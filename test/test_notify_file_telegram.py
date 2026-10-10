"""Telegram carries a file as a document; its caption keeps the reply footer."""

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
    TELEGRAM_CAPTION_LIMIT, UPLOAD_TIMEOUT, telegram_send_document, telegram_sender,
)
from server.notification_types import Attachment, OutboundMessage, SendContext

LINK = "https://lodge.test/tui?session=test-session"
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\n{LINK}"
DESTINATION = {"chat_id": "42"}
# Named here and not inside a test: test_shadow_wolt.py reads the Telegram host
# in a test body as a live call. Nothing below leaves the process; the transport
# tests run httpx over a MockTransport.
SEND_DOCUMENT_URL = "https://api.telegram.org/bott/sendDocument"
RACCOON = "\U0001F99D"                                      # two UTF-16 units


def _context() -> SendContext:
    return SendContext({"TELEGRAM_BOT_TOKEN": "t"}, {})


def _units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _text_of(units: int) -> str:
    """Text that brings text + FOOTER to exactly `units` UTF-16 units, emoji included."""
    return RACCOON * 10 + "x" * (units - 20 - _units(FOOTER))


@pytest.fixture
def attachment(tmp_path) -> Attachment:
    path = tmp_path / ("a" * 32)
    path.write_bytes(b"<h1>hi</h1>")
    return Attachment(path, "a.html", 11, "text/html")


# --- the sender: caption or split -------------------------------------------

@pytest.mark.asyncio
async def test_short_text_travels_as_the_caption_with_the_reply_footer(attachment):
    text_transport, document_transport = AsyncMock(), AsyncMock()
    send = telegram_sender(text_transport, document_transport)

    await send(OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context())

    document_transport.assert_awaited_once_with("t", "42", attachment, "hi\n" + FOOTER)
    text_transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_caption_of_exactly_1024_utf16_units_is_one_message(attachment):
    text_transport, document_transport = AsyncMock(), AsyncMock()
    text = _text_of(1024)
    assert _units(text + FOOTER) == TELEGRAM_CAPTION_LIMIT == 1024
    assert len(text + FOOTER) == 1014                       # fewer characters than units

    await telegram_sender(text_transport, document_transport)(
        OutboundMessage(text, LINK, (attachment,)), DESTINATION, _context(),
    )

    document_transport.assert_awaited_once_with("t", "42", attachment, text + FOOTER)
    text_transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_caption_of_1025_utf16_units_splits(attachment):
    text_transport, document_transport = AsyncMock(), AsyncMock()
    text = _text_of(1025)
    assert _units(text + FOOTER) == 1025
    assert len(text + FOOTER) == 1015                       # counting characters would not split

    await telegram_sender(text_transport, document_transport)(
        OutboundMessage(text, LINK, (attachment,)), DESTINATION, _context(),
    )

    text_transport.assert_awaited_once_with("t", "42", text + FOOTER)
    document_transport.assert_awaited_once_with("t", "42", attachment, FOOTER.strip())


@pytest.mark.asyncio
async def test_split_sends_the_text_before_the_document(attachment):
    order = []
    text_transport = AsyncMock(side_effect=lambda *args: order.append("text"))
    document_transport = AsyncMock(side_effect=lambda *args: order.append("document"))

    await telegram_sender(text_transport, document_transport)(
        OutboundMessage(_text_of(1025), LINK, (attachment,)), DESTINATION, _context(),
    )

    assert order == ["text", "document"]


@pytest.mark.asyncio
@pytest.mark.parametrize("link", [LINK, "session=test-session"])
@pytest.mark.parametrize("split", [False, True])
async def test_split_caption_is_still_a_parseable_reply_footer(attachment, link, split):
    from bot.telegram_adapter import _parse_session_from_reply

    document_transport = AsyncMock()
    text = "x" * 2000 if split else "hi\n"

    await telegram_sender(AsyncMock(), document_transport)(
        OutboundMessage(text, link, (attachment,)), DESTINATION, _context(),
    )

    caption = document_transport.await_args.args[3]
    assert (caption == f"---{DEN_REPLY_FOOTER}\n{link}") is split
    assert _parse_session_from_reply(caption) == "test-session"


@pytest.mark.asyncio
async def test_file_failure_after_text_says_what_was_delivered(attachment):
    original = RuntimeError("Bad Request: file too big")
    text_transport, document_transport = AsyncMock(), AsyncMock(side_effect=original)

    with pytest.raises(RuntimeError) as error:
        await telegram_sender(text_transport, document_transport)(
            OutboundMessage(_text_of(1025), LINK, (attachment,)), DESTINATION, _context(),
        )

    assert str(error.value) == (
        "the text was delivered but the file was not: Bad Request: file too big"
    )
    assert error.value.__cause__ is original
    text_transport.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_failure_with_a_blank_error_names_its_type(attachment):
    # httpx timeouts usually carry no message, and an upload is where one is likeliest.
    with pytest.raises(RuntimeError) as error:
        await telegram_sender(AsyncMock(), AsyncMock(side_effect=httpx.WriteTimeout("")))(
            OutboundMessage(_text_of(1025), LINK, (attachment,)), DESTINATION, _context(),
        )

    assert str(error.value) == "the text was delivered but the file was not: WriteTimeout"


@pytest.mark.asyncio
async def test_file_failure_as_one_message_is_the_provider_error_itself(attachment):
    original = RuntimeError("Bad Request: file too big")

    with pytest.raises(RuntimeError) as error:
        await telegram_sender(AsyncMock(), AsyncMock(side_effect=original))(
            OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
        )

    assert error.value is original                          # nothing was delivered


@pytest.mark.asyncio
async def test_text_failure_in_a_split_sends_no_document(attachment):
    original = RuntimeError("Bad Request: message is too long")
    document_transport = AsyncMock()

    with pytest.raises(RuntimeError) as error:
        await telegram_sender(AsyncMock(side_effect=original), document_transport)(
            OutboundMessage(_text_of(1025), LINK, (attachment,)), DESTINATION, _context(),
        )

    assert error.value is original
    document_transport.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text, caption, text_sent", [
    ("hi\n", "hi\n", None),
    ("x" * 1024, "x" * 1024, None),
    ("x" * 1025, "", "x" * 1025),
])
async def test_no_session_link_means_no_footer(attachment, text, caption, text_sent):
    text_transport, document_transport = AsyncMock(), AsyncMock()

    await telegram_sender(text_transport, document_transport)(
        OutboundMessage(text, None, (attachment,)), DESTINATION, _context(),
    )

    document_transport.assert_awaited_once_with("t", "42", attachment, caption)
    if text_sent is None:
        text_transport.assert_not_awaited()
    else:
        text_transport.assert_awaited_once_with("t", "42", text_sent)


@pytest.mark.asyncio
async def test_text_only_message_never_touches_the_document_transport():
    text_transport, document_transport = AsyncMock(), AsyncMock()

    await telegram_sender(text_transport, document_transport)(
        OutboundMessage("x" * 5000, LINK), DESTINATION, _context(),
    )

    text_transport.assert_awaited_once_with("t", "42", "x" * 5000 + FOOTER)
    document_transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_attachment_without_a_document_transport_raises(attachment):
    text_transport = AsyncMock()

    with pytest.raises(RuntimeError) as error:
        await telegram_sender(text_transport)(
            OutboundMessage("hi\n", LINK, (attachment,)), DESTINATION, _context(),
        )

    assert str(error.value) == "telegram sender has no document transport"
    text_transport.assert_not_awaited()


@pytest.mark.asyncio
async def test_more_than_one_attachment_is_refused_not_dropped(attachment):
    text_transport, document_transport = AsyncMock(), AsyncMock()

    with pytest.raises(RuntimeError) as error:
        await telegram_sender(text_transport, document_transport)(
            OutboundMessage("hi\n", LINK, (attachment, attachment)), DESTINATION, _context(),
        )

    assert str(error.value) == "telegram sender carries one file per message"
    text_transport.assert_not_awaited()
    document_transport.assert_not_awaited()


# --- the transport: a real httpx client over a mock transport ----------------

class _Telegram:
    """sendDocument behind a real httpx.AsyncClient on a MockTransport.

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
async def test_send_document_posts_multipart_with_the_upload_timeout(monkeypatch, attachment):
    telegram = _Telegram(monkeypatch)

    result = await telegram_send_document("t", "42", attachment, "hi")

    assert result == {"ok": True, "result": {"message_id": 7}}
    assert [str(request.url) for request in telegram.requests] == [SEND_DOCUMENT_URL]
    parts = telegram.parts
    assert set(parts) == {"chat_id", "caption", "document"}
    assert parts["chat_id"][1] == b"42"
    assert parts["caption"][1] == b"hi"
    head, body = parts["document"]
    assert 'filename="a.html"' in head and "Content-Type: text/html" in head
    assert body == b"<h1>hi</h1>"
    assert telegram.post_kwargs[0]["timeout"] is UPLOAD_TIMEOUT


@pytest.mark.asyncio
async def test_send_document_leaves_out_an_empty_caption(monkeypatch, attachment):
    telegram = _Telegram(monkeypatch)

    await telegram_send_document("t", "42", attachment, "")

    assert set(telegram.parts) == {"chat_id", "document"}


@pytest.mark.asyncio
async def test_send_document_carries_an_odd_filename_and_a_long_caption(monkeypatch, tmp_path):
    telegram = _Telegram(monkeypatch)
    path = tmp_path / ("b" * 32)
    path.write_bytes(bytes(range(256)) * 4)                 # every byte value, CRLF included
    name = f"résumé {RACCOON} weekly update.pdf"
    caption = f"{RACCOON} wolt: done\n" + FOOTER

    await telegram_send_document("t", "42", Attachment(path, name, 1024, "application/pdf"), caption)

    parts = telegram.parts
    assert parts["caption"][1] == caption.encode()
    head, body = parts["document"]
    assert f'filename="{name}"' in head and "Content-Type: application/pdf" in head
    assert body == bytes(range(256)) * 4


@pytest.mark.asyncio
@pytest.mark.parametrize("answer, message", [
    ({"ok": False, "description": "nope"}, "nope"),
    ({"ok": False}, "telegram error"),
])
async def test_send_document_raises_what_telegram_answers(monkeypatch, attachment, answer, message):
    _Telegram(monkeypatch, httpx.Response(400, json=answer))

    with pytest.raises(RuntimeError) as error:
        await telegram_send_document("t", "42", attachment, "hi")

    assert str(error.value) == message


@pytest.mark.asyncio
@pytest.mark.parametrize("status, body", [
    (413, "<html><h1>413 Request Entity Too Large</h1></html>"),
    (502, ""),
])
async def test_send_document_names_the_status_when_the_answer_is_not_json(
    monkeypatch, attachment, status, body,
):
    _Telegram(monkeypatch, httpx.Response(status, text=body))

    with pytest.raises(RuntimeError) as error:
        await telegram_send_document("t", "42", attachment, "hi")

    assert str(error.value) == f"telegram answered HTTP {status}"


# --- registration and the whole path ----------------------------------------

def test_registered_telegram_sender_declares_fifty_megabytes():
    assert notify.SENDERS["telegram"].max_attachment_bytes == 50 * 1024 * 1024
    assert notify.SENDERS["telegram"].secrets == ("TELEGRAM_BOT_TOKEN",)


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
    source = tmp_path / "a.html"
    source.write_bytes(b"<h1>hi</h1>")
    return tmp_path / "outbox", outbox.stage(source, tmp_path / "outbox")


@pytest.mark.asyncio
async def test_end_to_end_through_send_notification(lodge, monkeypatch):
    box, entry = lodge
    text_transport, document_transport = AsyncMock(), AsyncMock()
    monkeypatch.setattr(notify, "telegram_send", text_transport)
    monkeypatch.setattr(notify, "telegram_send_document", document_transport)

    result = await notify.send_notification(
        "test-session", "hi\n", None, [{"id": entry, "name": "a.html"}],
    )

    token, chat_id, attachment, caption = document_transport.await_args.args
    assert (token, chat_id) == ("telegram-token", "101")
    assert attachment == Attachment(box / entry, "a.html", 11, "text/html")
    assert caption == "hi\n" + FOOTER
    document_transport.assert_awaited_once()
    text_transport.assert_not_awaited()
    assert result["attachments"] == [{"name": "a.html", "content_type": "text/html", "size": 11}]
    assert not (box / entry).exists()


@pytest.mark.asyncio
async def test_end_to_end_the_bytes_are_on_the_wire_before_the_entry_is_removed(lodge, monkeypatch):
    box, entry = lodge
    telegram = _Telegram(monkeypatch)

    await notify.send_notification("test-session", "hi\n", None, [{"id": entry, "name": "a.html"}])

    parts = telegram.parts
    assert parts["chat_id"][1] == b"101"
    assert parts["caption"][1] == ("hi\n" + FOOTER).encode()
    assert parts["document"][1] == b"<h1>hi</h1>"
    assert not (box / entry).exists()
