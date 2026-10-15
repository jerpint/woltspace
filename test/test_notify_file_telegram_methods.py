"""Telegram carries a file by the method its name picks, and as a document when that is refused."""

import logging
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

import outbox
from server import notification_senders as senders
from server import notify
from server.config import DEN_REPLY_FOOTER
from server.notification_senders import (
    UPLOAD_TIMEOUT, TelegramRefused, telegram_send, telegram_send_audio,
    telegram_send_document, telegram_send_voice, telegram_sender,
)
from server.notification_types import Attachment, OutboundMessage, SendContext

# notify's direct Telegram path: the old adapter runtime (see conftest).
pytestmark = pytest.mark.usefixtures("telegram_adapter_runtime")

LINK = "https://lodge.test/tui?session=test-session"
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\n{LINK}"
DESTINATION = {"chat_id": "42"}
# Named here and not inside a test: test_shadow_wolt.py reads the Telegram host
# in a test body as a live call. Nothing below leaves the process.
API = "https://api.telegram.org/bott/"
OGG = b"OggS" + b"\x00" * 23
REFUSAL = "Bad Request: wrong file type"


def _context() -> SendContext:
    return SendContext({"TELEGRAM_BOT_TOKEN": "t"}, {})


def _file(tmp_path, name: str, kind: str = "file") -> Attachment:
    path = tmp_path / ("c" * 32)
    path.write_bytes(OGG)
    return Attachment(path, name, len(OGG), "application/octet-stream", kind)


def _transports() -> dict[str, AsyncMock]:
    return {name: AsyncMock() for name in ("text", "document", "voice", "audio")}


def _sender(transports):
    return telegram_sender(
        transports["text"], transports["document"], transports["voice"], transports["audio"],
    )


# --- which method a name picks ------------------------------------------------

@pytest.mark.parametrize("name, method", [
    ("note.ogg", "voice"), ("note.oga", "voice"), ("note.opus", "voice"),
    ("NOTE.OGG", "voice"), ("Note.OpUs", "voice"),
    ("song.mp3", "audio"), ("memo.m4a", "audio"), ("SONG.MP3", "audio"), ("Memo.M4a", "audio"),
    ("page.html", "document"), ("clip.wav", "document"), ("noext", "document"),
    ("ogg", "document"), ("x.ogg.txt", "document"), (".ogg", "document"),
])
@pytest.mark.asyncio
async def test_a_file_goes_out_by_the_method_its_name_picks(tmp_path, name, method):
    transports = _transports()
    attachment = _file(tmp_path, name)

    await _sender(transports)(
        OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
    )

    transports[method].assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    for other in set(transports) - {method}:
        transports[other].assert_not_awaited()


@pytest.mark.parametrize("name", ["note.ogg", "song.mp3"])
@pytest.mark.asyncio
async def test_a_sender_without_voice_and_audio_transports_sends_those_names_as_documents(
    tmp_path, name,
):
    text, document = AsyncMock(), AsyncMock()
    attachment = _file(tmp_path, name)

    await telegram_sender(text, document)(
        OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
    )

    document.assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    text.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_file_needs_the_document_transport_whatever_its_name(tmp_path):
    # The document transport is what a refused voice or audio upload falls back to.
    text, voice, audio = AsyncMock(), AsyncMock(), AsyncMock()

    with pytest.raises(RuntimeError, match="^telegram sender has no document transport$"):
        await telegram_sender(text, None, voice, audio)(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)),
            DESTINATION, _context(),
        )

    voice.assert_not_awaited()
    text.assert_not_awaited()


# --- the fallback ---------------------------------------------------------------

@pytest.mark.parametrize("name, method", [("note.ogg", "voice"), ("song.mp3", "audio")])
@pytest.mark.asyncio
async def test_a_refused_upload_is_sent_again_as_a_document_with_the_same_caption(
    tmp_path, name, method,
):
    transports = _transports()
    transports[method].side_effect = TelegramRefused(REFUSAL)
    attachment = _file(tmp_path, name)

    await _sender(transports)(
        OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
    )

    transports[method].assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    transports["document"].assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    transports["text"].assert_not_awaited()


@pytest.mark.asyncio
async def test_when_the_document_is_refused_too_the_error_is_the_document_s(tmp_path):
    transports = _transports()
    transports["voice"].side_effect = TelegramRefused(REFUSAL)
    of_the_document = TelegramRefused("Bad Request: file is too big")
    transports["document"].side_effect = of_the_document

    with pytest.raises(RuntimeError) as error:
        await _sender(transports)(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)),
            DESTINATION, _context(),
        )

    assert error.value is of_the_document
    transports["voice"].assert_awaited_once()


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("no route to host"), httpx.WriteTimeout(""), RuntimeError("not a refusal"),
])
@pytest.mark.asyncio
async def test_a_failure_that_is_not_telegram_s_refusal_sends_no_document(tmp_path, failure):
    # A request that may have arrived is not sent a second time under another method.
    transports = _transports()
    transports["voice"].side_effect = failure

    with pytest.raises(type(failure)) as error:
        await _sender(transports)(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)),
            DESTINATION, _context(),
        )

    assert error.value is failure
    transports["document"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_voice_note_that_was_asked_for_has_no_fallback_when_refused(tmp_path):
    # --voice and --speak asked for a voice note: the wolt is told it could not
    # be one, and decides what to send instead.
    transports = _transports()
    refusal = TelegramRefused("Bad Request: VOICE_MESSAGES_FORBIDDEN")
    transports["voice"].side_effect = refusal

    with pytest.raises(RuntimeError) as error:
        await _sender(transports)(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg", "voice"),)),
            DESTINATION, _context(),
        )

    assert error.value is refusal
    transports["document"].assert_not_awaited()
    transports["audio"].assert_not_awaited()


@pytest.mark.asyncio
async def test_an_mp3_asked_for_as_a_voice_note_still_uses_the_voice_transport(tmp_path):
    transports = _transports()
    attachment = _file(tmp_path, "song.mp3", "voice")

    await _sender(transports)(
        OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
    )

    transports["voice"].assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    transports["audio"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_fallback_is_logged_once_with_the_method_and_telegram_s_words(tmp_path, caplog):
    transports = _transports()
    transports["audio"].side_effect = TelegramRefused(REFUSAL)

    with caplog.at_level(logging.WARNING, logger="server.notification_senders"):
        await _sender(transports)(
            OutboundMessage("the secret plan\n", LINK, (_file(tmp_path, "song.mp3"),)),
            DESTINATION, _context(),
        )

    (record,) = caplog.records
    assert record.levelno == logging.WARNING
    assert "sendAudio" in record.getMessage() and REFUSAL in record.getMessage()
    assert "the secret plan" not in record.getMessage()


@pytest.mark.asyncio
async def test_no_fallback_means_no_log(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="server.notification_senders"):
        await _sender(_transports())(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)),
            DESTINATION, _context(),
        )

    assert caplog.records == []


@pytest.mark.asyncio
async def test_text_too_long_for_a_caption_goes_first_and_the_fallback_keeps_the_footer(tmp_path):
    calls = []
    transports = _transports()
    transports["text"].side_effect = lambda *args: calls.append("text")
    transports["document"].side_effect = lambda *args: calls.append("document")

    async def refuse(*args):
        calls.append("voice")
        raise TelegramRefused(REFUSAL)

    transports["voice"].side_effect = refuse
    attachment, long_text = _file(tmp_path, "note.ogg"), "x" * 2000

    await _sender(transports)(
        OutboundMessage(long_text, LINK, (attachment,)), DESTINATION, _context(),
    )

    assert calls == ["text", "voice", "document"]
    transports["text"].assert_awaited_once_with("t", "42", long_text + FOOTER)
    transports["voice"].assert_awaited_once_with("t", "42", attachment, FOOTER.strip())
    transports["document"].assert_awaited_once_with("t", "42", attachment, FOOTER.strip())


@pytest.mark.asyncio
async def test_after_the_text_a_double_refusal_says_what_was_delivered_with_the_document_s_error(
    tmp_path,
):
    transports = _transports()
    transports["audio"].side_effect = TelegramRefused(REFUSAL)
    transports["document"].side_effect = TelegramRefused("Bad Request: file is too big")

    with pytest.raises(RuntimeError) as error:
        await _sender(transports)(
            OutboundMessage("x" * 2000, LINK, (_file(tmp_path, "song.mp3"),)),
            DESTINATION, _context(),
        )

    assert str(error.value) == (
        "the text was delivered but the file was not: Bad Request: file is too big"
    )
    transports["text"].assert_awaited_once()
    transports["audio"].assert_awaited_once()


# --- the transports: a real httpx client over a mock transport ----------------

class _Telegram:
    """The Bot API behind a real httpx.AsyncClient on a MockTransport.

    `answers` maps a method name to the response it gets, or to an exception
    the connection raises; every other method is answered ok. httpx's own
    multipart encoder runs and nothing reaches the network.
    """

    def __init__(self, monkeypatch, answers=None):
        self.requests, self.post_kwargs = [], []
        answers = answers or {}
        telegram = self

        def handler(request):
            telegram.requests.append(request)
            answer = answers.get(request.url.path.rsplit("/", 1)[-1])
            if isinstance(answer, Exception):
                raise answer
            return answer or httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})

        class Client(httpx.AsyncClient):
            def __init__(self, **kwargs):
                super().__init__(transport=httpx.MockTransport(handler), **kwargs)

            async def post(self, url, **kwargs):
                telegram.post_kwargs.append(kwargs)
                return await super().post(url, **kwargs)

        monkeypatch.setattr(senders.httpx, "AsyncClient", Client)

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def parts(self, index: int = 0) -> dict[str, tuple[str, bytes]]:
        """One request's multipart body as {field: (part headers, part body)}."""
        request = self.requests[index]
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
async def test_send_audio_posts_the_file_in_the_audio_field(monkeypatch, tmp_path):
    telegram = _Telegram(monkeypatch)
    song = Attachment(_file(tmp_path, "song.mp3").path, "song.mp3", len(OGG), "audio/mpeg")

    result = await telegram_send_audio("t", "42", song, "hi")

    assert result == {"ok": True, "result": {"message_id": 7}}
    assert telegram.urls == [API + "sendAudio"]
    parts = telegram.parts()
    assert set(parts) == {"chat_id", "caption", "audio"}
    assert parts["chat_id"][1] == b"42"
    assert parts["caption"][1] == b"hi"
    head, body = parts["audio"]
    assert 'filename="song.mp3"' in head and "Content-Type: audio/mpeg" in head
    assert body == OGG
    assert telegram.post_kwargs[0]["timeout"] is UPLOAD_TIMEOUT


@pytest.mark.asyncio
async def test_send_audio_leaves_out_an_empty_caption(monkeypatch, tmp_path):
    telegram = _Telegram(monkeypatch)

    await telegram_send_audio("t", "42", _file(tmp_path, "song.mp3"), "")

    assert set(telegram.parts()) == {"chat_id", "audio"}


@pytest.mark.parametrize("upload, method", [
    (telegram_send_voice, "sendVoice"), (telegram_send_audio, "sendAudio"),
    (telegram_send_document, "sendDocument"),
])
@pytest.mark.parametrize("answer, words", [
    (httpx.Response(400, json={"ok": False, "description": REFUSAL}), REFUSAL),
    (httpx.Response(400, json={"ok": False}), "telegram error"),
    (httpx.Response(413, text="<html>too large</html>"), "telegram answered HTTP 413"),
])
@pytest.mark.asyncio
async def test_an_answer_that_is_not_ok_is_a_refusal(
    monkeypatch, tmp_path, upload, method, answer, words,
):
    _Telegram(monkeypatch, {method: answer})

    with pytest.raises(TelegramRefused) as error:
        await upload("t", "42", _file(tmp_path, "note.ogg"), "hi")

    assert str(error.value) == words
    assert isinstance(error.value, RuntimeError)       # what every older caller catches


@pytest.mark.asyncio
async def test_a_connection_that_fails_is_not_a_refusal(monkeypatch, tmp_path):
    _Telegram(monkeypatch, {"sendVoice": httpx.ConnectError("no route to host")})

    with pytest.raises(httpx.ConnectError):
        await telegram_send_voice("t", "42", _file(tmp_path, "note.ogg"), "hi")


@pytest.mark.parametrize("name, method, field", [
    ("note.ogg", "sendVoice", "voice"), ("song.mp3", "sendAudio", "audio"),
])
@pytest.mark.asyncio
async def test_on_the_wire_a_refused_upload_is_followed_by_the_same_bytes_as_a_document(
    monkeypatch, tmp_path, name, method, field,
):
    telegram = _Telegram(monkeypatch, {
        method: httpx.Response(400, json={"ok": False, "description": REFUSAL}),
    })
    send = telegram_sender(
        telegram_send, telegram_send_document, telegram_send_voice, telegram_send_audio,
    )

    await send(
        OutboundMessage("hi\n", LINK, (_file(tmp_path, name),)), DESTINATION, _context(),
    )

    assert telegram.urls == [API + method, API + "sendDocument"]
    first, second = telegram.parts(0), telegram.parts(1)
    assert set(first) == {"chat_id", "caption", field}
    assert set(second) == {"chat_id", "caption", "document"}
    assert first["caption"][1] == second["caption"][1] == ("hi\n" + FOOTER).encode()
    assert first[field][1] == second["document"][1] == OGG
    assert f'filename="{name}"' in second["document"][0]


@pytest.mark.asyncio
async def test_on_the_wire_a_failed_connection_is_not_followed_by_a_document(monkeypatch, tmp_path):
    telegram = _Telegram(monkeypatch, {"sendVoice": httpx.ConnectError("no route to host")})
    send = telegram_sender(
        telegram_send, telegram_send_document, telegram_send_voice, telegram_send_audio,
    )

    with pytest.raises(httpx.ConnectError):
        await send(
            OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)),
            DESTINATION, _context(),
        )

    assert telegram.urls == [API + "sendVoice"]


# --- registration and the whole path ------------------------------------------

@pytest.mark.asyncio
async def test_registered_telegram_sender_has_an_audio_transport(monkeypatch, tmp_path):
    send_audio = AsyncMock()
    monkeypatch.setattr(notify, "telegram_send_audio", send_audio)
    song = _file(tmp_path, "song.mp3")

    await notify.SENDERS["telegram"].send(
        OutboundMessage("hi", None, (song,)), DESTINATION, _context(),
    )

    send_audio.assert_awaited_once_with("t", "42", song, "hi")


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    env = {"TELEGRAM_BOT_TOKEN": "telegram-token", "TELEGRAM_ALLOWED_USERS": "101"}
    (tmp_path / "tunnel.json").write_text('{"url": "https://lodge.test"}')
    monkeypatch.setattr(notify, "SPACE_PLATFORM_DIR", tmp_path)
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setattr(notify, "read_session_registry", lambda name: None)
    monkeypatch.setattr(notify, "append_chat_history", Mock())
    monkeypatch.setattr(notify, "SessionRegistry", lambda root: Mock())
    transports = {
        name: AsyncMock() for name in (
            "telegram_send", "telegram_send_document", "telegram_send_voice",
            "telegram_send_audio",
        )
    }
    for name, transport in transports.items():
        monkeypatch.setattr(notify, name, transport)
    return tmp_path, transports


@pytest.mark.parametrize("name, transport", [
    ("note.ogg", "telegram_send_voice"), ("song.mp3", "telegram_send_audio"),
    ("page.html", "telegram_send_document"),
])
@pytest.mark.asyncio
async def test_end_to_end_a_file_reaches_the_transport_its_name_picks(lodge, name, transport):
    tmp_path, transports = lodge
    source = tmp_path / name
    source.write_bytes(OGG)
    entry = outbox.stage(source, tmp_path / "outbox")

    result = await notify.send_notification(
        "test-session", "hi\n", None, [{"id": entry, "name": name}],
    )

    token, chat_id, attachment, caption = transports[transport].await_args.args
    assert (token, chat_id, caption) == ("telegram-token", "101", "hi\n" + FOOTER)
    assert (attachment.filename, attachment.kind) == (name, "file")
    for other in set(transports) - {transport}:
        transports[other].assert_not_awaited()
    # Whatever carried it, the lodge's answer for a file has no kind.
    assert [set(entry) for entry in result["attachments"]] == [{"name", "content_type", "size"}]
    assert not (tmp_path / "outbox" / entry).exists()
