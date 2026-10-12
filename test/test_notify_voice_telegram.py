"""Telegram carries an attachment of kind "voice" as a voice note."""

import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

from server import notification_senders as senders
from server import notify
from server.config import DEN_REPLY_FOOTER
from server.notification_senders import UPLOAD_TIMEOUT, telegram_send_voice, telegram_sender
from server.notification_types import Attachment, OutboundMessage, SendContext

LINK = "https://lodge.test/tui?session=test-session"
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\n{LINK}"
DESTINATION = {"chat_id": "42"}
# Named here and not inside a test: test_shadow_wolt.py reads the Telegram host
# in a test body as a live call. Nothing below leaves the process.
SEND_VOICE_URL = "https://api.telegram.org/bott/sendVoice"
OGG = b"OggS" + b"\x00" * 23


def _context() -> SendContext:
    return SendContext({"TELEGRAM_BOT_TOKEN": "t"}, {})


@pytest.fixture
def voice(tmp_path) -> Attachment:
    path = tmp_path / ("b" * 32)
    path.write_bytes(OGG)
    return Attachment(path, "note.ogg", len(OGG), "audio/ogg", "voice")


@pytest.fixture
def document(tmp_path) -> Attachment:
    path = tmp_path / ("a" * 32)
    path.write_bytes(b"<h1>hi</h1>")
    return Attachment(path, "a.html", 11, "text/html")


# --- the sender: which transport, and the caption rule -----------------------

@pytest.mark.asyncio
async def test_a_voice_attachment_uses_the_voice_transport(voice):
    text, send_document, send_voice = AsyncMock(), AsyncMock(), AsyncMock()
    send = telegram_sender(text, send_document, send_voice)

    await send(OutboundMessage("hi\n", LINK, (voice,)), DESTINATION, _context())

    send_voice.assert_awaited_once_with("t", "42", voice, "hi\n" + FOOTER)
    send_document.assert_not_awaited()
    text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_attachment_still_uses_the_document_transport(document):
    text, send_document, send_voice = AsyncMock(), AsyncMock(), AsyncMock()
    send = telegram_sender(text, send_document, send_voice)

    await send(OutboundMessage("hi\n", LINK, (document,)), DESTINATION, _context())

    send_document.assert_awaited_once_with("t", "42", document, "hi\n" + FOOTER)
    send_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_too_long_for_a_caption_goes_first_and_the_voice_note_keeps_the_footer(voice):
    calls = []
    text = AsyncMock(side_effect=lambda *args: calls.append("text"))
    send_voice = AsyncMock(side_effect=lambda *args: calls.append("voice"))
    send = telegram_sender(text, AsyncMock(), send_voice)
    long_text = "x" * 2000

    await send(OutboundMessage(long_text, LINK, (voice,)), DESTINATION, _context())

    assert calls == ["text", "voice"]
    text.assert_awaited_once_with("t", "42", long_text + FOOTER)
    send_voice.assert_awaited_once_with("t", "42", voice, FOOTER.strip())


@pytest.mark.asyncio
async def test_voice_failure_after_text_says_what_was_delivered(voice):
    send_voice = AsyncMock(side_effect=RuntimeError("Bad Request: VOICE_MESSAGES_FORBIDDEN"))
    send = telegram_sender(AsyncMock(), AsyncMock(), send_voice)

    with pytest.raises(RuntimeError) as error:
        await send(OutboundMessage("x" * 2000, LINK, (voice,)), DESTINATION, _context())

    assert str(error.value) == (
        "the text was delivered but the voice note was not: "
        "Bad Request: VOICE_MESSAGES_FORBIDDEN"
    )


@pytest.mark.asyncio
async def test_voice_failure_as_one_message_is_the_provider_error_itself(voice):
    send_voice = AsyncMock(side_effect=RuntimeError("Bad Request: VOICE_MESSAGES_FORBIDDEN"))
    send = telegram_sender(AsyncMock(), AsyncMock(), send_voice)

    with pytest.raises(RuntimeError, match="^Bad Request: VOICE_MESSAGES_FORBIDDEN$"):
        await send(OutboundMessage("hi", LINK, (voice,)), DESTINATION, _context())


@pytest.mark.asyncio
async def test_voice_attachment_without_a_voice_transport_raises(voice):
    text, send_document = AsyncMock(), AsyncMock()
    send = telegram_sender(text, send_document)

    with pytest.raises(RuntimeError, match="telegram sender has no voice transport"):
        await send(OutboundMessage("hi", LINK, (voice,)), DESTINATION, _context())

    text.assert_not_awaited()
    send_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_registered_telegram_sender_has_a_voice_transport(monkeypatch, voice):
    send_voice = AsyncMock()
    monkeypatch.setattr(notify, "telegram_send_voice", send_voice)

    await notify.SENDERS["telegram"].send(
        OutboundMessage("hi", None, (voice,)), DESTINATION, _context(),
    )

    send_voice.assert_awaited_once_with("t", "42", voice, "hi")


# --- the transport: a real httpx client over a mock transport ----------------

class _Telegram:
    """sendVoice behind a real httpx.AsyncClient on a MockTransport.

    httpx's own multipart encoder runs, so a body an async client cannot send
    fails here the way it would against Telegram, and nothing reaches the network.
    """

    def __init__(self, monkeypatch, answer=None):
        self.requests, self.post_kwargs = [], []
        answer = answer or httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        telegram = self

        def handler(request):
            telegram.requests.append(request)
            return answer

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

            async def post(self, url, **kwargs):
                telegram.post_kwargs.append(kwargs)
                return await super().post(url, **kwargs)

        monkeypatch.setattr(senders.httpx, "AsyncClient", Client)

    @property
    def parts(self) -> dict[str, tuple[str, bytes]]:
        """The one request's multipart body as {field: (part headers, part body)}."""
        (request,) = self.requests
        content_type = request.headers["content-type"]
        assert content_type.startswith("multipart/form-data; boundary=")
        boundary = content_type.split("boundary=", 1)[1].encode()
        parts = {}
        for chunk in request.content.split(b"--" + boundary)[1:-1]:
            head, _, body = chunk[2:-2].partition(b"\r\n\r\n")      # exactly one CRLF each side
            head = head.decode()
            parts[re.search(r'name="([^"]+)"', head).group(1)] = (head, body)
        return parts


@pytest.mark.asyncio
async def test_send_voice_posts_the_audio_in_the_voice_field(monkeypatch, voice):
    telegram = _Telegram(monkeypatch)

    result = await telegram_send_voice("t", "42", voice, "hi")

    assert result == {"ok": True, "result": {"message_id": 7}}
    assert [str(request.url) for request in telegram.requests] == [SEND_VOICE_URL]
    parts = telegram.parts
    assert set(parts) == {"chat_id", "caption", "voice"}
    assert parts["chat_id"][1] == b"42"
    assert parts["caption"][1] == b"hi"
    head, body = parts["voice"]
    assert 'filename="note.ogg"' in head and "Content-Type: audio/ogg" in head
    assert body == OGG
    assert telegram.post_kwargs[0]["timeout"] is UPLOAD_TIMEOUT


@pytest.mark.asyncio
async def test_send_voice_leaves_out_an_empty_caption(monkeypatch, voice):
    telegram = _Telegram(monkeypatch)

    await telegram_send_voice("t", "42", voice, "")

    assert set(telegram.parts) == {"chat_id", "voice"}


@pytest.mark.asyncio
async def test_send_voice_raises_what_telegram_answers(monkeypatch, voice):
    _Telegram(monkeypatch, httpx.Response(
        400, json={"ok": False, "description": "Bad Request: VOICE_MESSAGES_FORBIDDEN"},
    ))

    with pytest.raises(RuntimeError, match="^Bad Request: VOICE_MESSAGES_FORBIDDEN$"):
        await telegram_send_voice("t", "42", voice, "hi")


@pytest.mark.asyncio
async def test_send_voice_names_the_status_when_the_answer_is_not_json(monkeypatch, voice):
    _Telegram(monkeypatch, httpx.Response(413, text="<html>too large</html>"))

    with pytest.raises(RuntimeError, match="^telegram answered HTTP 413$"):
        await telegram_send_voice("t", "42", voice, "hi")
