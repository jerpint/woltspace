"""Telegram carries an attachment of kind "voice" as a voice note."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

from server import notification_senders as senders
from server import notify
from server.config import DEN_REPLY_FOOTER
from server.notification_senders import telegram_send_voice, telegram_sender
from server.notification_types import Attachment, OutboundMessage, SendContext

LINK = "https://lodge.test/tui?session=test-session"
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\n{LINK}"
DESTINATION = {"chat_id": "42"}
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


# --- the transport: the Telegram connector, over its socket ------------------

@pytest.mark.asyncio
async def test_send_voice_hands_the_connector_the_audio_as_a_voice_note(monkeypatch, voice):
    from connector_loopback import install_fake_broker
    connector = install_fake_broker(monkeypatch)

    await telegram_send_voice("", "42", voice, "hi")

    [frame] = connector.frames
    assert (frame["method"], frame["route"], frame["text"]) == ("voice", {"chat_id": "42"}, "hi")
    assert (frame["filename"], frame["content_type"], frame["bytes"]) == ("note.ogg", "audio/ogg", OGG)


@pytest.mark.asyncio
async def test_send_voice_raises_what_telegram_answers(monkeypatch, voice):
    from connector_loopback import install_fake_broker
    install_fake_broker(monkeypatch, answer=lambda frame: {
        "status": "refused", "error": "Bad Request: VOICE_MESSAGES_FORBIDDEN"})

    with pytest.raises(senders.TelegramRefused, match="^Bad Request: VOICE_MESSAGES_FORBIDDEN$"):
        await telegram_send_voice("", "42", voice, "hi")
