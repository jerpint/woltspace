"""Telegram carries a file by the method its name picks, and as a document when that is refused."""

import logging
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
    TelegramRefused, telegram_send, telegram_send_audio,
    telegram_send_document, telegram_send_voice, telegram_sender,
)
from server.notification_types import Attachment, OutboundMessage, SendContext

LINK = "https://lodge.test/tui?session=test-session"
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\n{LINK}"
DESTINATION = {"chat_id": "42"}
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


# --- the transports: the Telegram connector, over its socket -------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("transport, method", [
    (telegram_send_voice, "voice"), (telegram_send_audio, "audio"), (telegram_send_document, "document"),
])
async def test_each_transport_asks_the_connector_for_its_method(monkeypatch, tmp_path, transport, method):
    from connector_loopback import install_fake_broker
    connector = install_fake_broker(monkeypatch)

    await transport("", "42", _file(tmp_path, "song.mp3"), "")

    [frame] = connector.frames
    assert (frame["method"], frame["filename"], frame["text"], frame["bytes"]) == (method, "song.mp3", "", OGG)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [telegram_send_voice, telegram_send_audio, telegram_send_document])
async def test_an_answer_that_is_not_ok_is_a_refusal(monkeypatch, tmp_path, transport):
    from connector_loopback import install_fake_broker
    install_fake_broker(monkeypatch, answer=lambda frame: {"status": "refused", "error": REFUSAL})

    with pytest.raises(TelegramRefused, match=f"^{REFUSAL}$"):
        await transport("", "42", _file(tmp_path, "note.ogg"), "hi")


@pytest.mark.asyncio
async def test_a_connection_that_fails_is_not_a_refusal(monkeypatch, tmp_path):
    from connector_loopback import install_fake_broker
    install_fake_broker(monkeypatch, answer=lambda frame: {"status": "failed", "error": "no route to host"})

    with pytest.raises(RuntimeError) as error:
        await telegram_send_voice("", "42", _file(tmp_path, "note.ogg"), "hi")

    assert not isinstance(error.value, TelegramRefused)


@pytest.mark.asyncio
@pytest.mark.parametrize("name, method", [("note.ogg", "voice"), ("song.mp3", "audio")])
async def test_through_the_connector_a_refused_upload_is_followed_by_the_same_bytes_as_a_document(
    monkeypatch, tmp_path, name, method,
):
    from connector_loopback import install_fake_broker
    connector = install_fake_broker(monkeypatch, answer=lambda frame: (
        {"status": "refused", "error": REFUSAL} if frame["method"] == method
        else {"status": "delivered"}))
    send = telegram_sender(
        telegram_send, telegram_send_document, telegram_send_voice, telegram_send_audio,
    )

    await send(OutboundMessage("hi\n", LINK, (_file(tmp_path, name),)), DESTINATION, _context())

    first, second = connector.frames
    assert (first["method"], second["method"]) == (method, "document")
    assert first["text"] == second["text"] == "hi\n" + FOOTER
    assert first["bytes"] == second["bytes"] == OGG
    assert second["filename"] == name


@pytest.mark.asyncio
async def test_through_the_connector_a_failed_send_is_not_followed_by_a_document(monkeypatch, tmp_path):
    from connector_loopback import install_fake_broker
    connector = install_fake_broker(monkeypatch, answer=lambda frame: {"status": "ambiguous", "error": "timed out"})
    send = telegram_sender(
        telegram_send, telegram_send_document, telegram_send_voice, telegram_send_audio,
    )

    with pytest.raises(RuntimeError, match="ambiguous"):
        await send(OutboundMessage("hi\n", LINK, (_file(tmp_path, "note.ogg"),)), DESTINATION, _context())

    assert [frame["method"] for frame in connector.frames] == ["voice"]


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
    # Telegram sends through its connector: here it is connected while the
    # lodge has a bot token, so the old "no token" cases are "not connected".
    monkeypatch.setattr(notify, "_connector_up",
                        lambda name: name != "telegram" or bool(env.get("TELEGRAM_BOT_TOKEN")))
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
    assert (token, chat_id, caption) == ("", "101", "hi\n" + FOOTER)  # the token stays with the connector
    assert (attachment.filename, attachment.kind) == (name, "file")
    for other in set(transports) - {transport}:
        transports[other].assert_not_awaited()
    # Whatever carried it, the lodge's answer for a file has no kind.
    assert [set(entry) for entry in result["attachments"]] == [{"name", "content_type", "size"}]
    assert not (tmp_path / "outbox" / entry).exists()
