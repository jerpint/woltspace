"""Telegram media downloads retry, and a lost message is never silent.

A voice note whose download timed out used to vanish: the timeout was
swallowed by the global error handler and the person was never told.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import TimedOut

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "connectors" / "telegram"))


def _context(get_file):
    context = MagicMock()
    context.bot.get_file = get_file
    return context


def _file_returning(data: bytes):
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(data))
    return file


def _voice_update():
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = 99999
    update.effective_chat.type = "private"
    update.message.voice.file_id = "voice-file"
    update.message.voice.file_unique_id = "voice-unique"
    update.message.reply_to_message = None
    update.message.reply_text = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_download_retries_a_timeout_then_succeeds():
    import telegram_connector as tg

    get_file = AsyncMock(side_effect=[TimedOut("slow"), _file_returning(b"ogg")])
    with patch.object(tg.asyncio, "sleep", new_callable=AsyncMock):
        data = await tg._download_bytes(_context(get_file), "voice-file", "voice message")
    assert data == b"ogg"
    assert get_file.await_count == 2


@pytest.mark.asyncio
async def test_download_gives_up_after_the_last_attempt():
    import telegram_connector as tg

    get_file = AsyncMock(side_effect=TimedOut("slow"))
    with patch.object(tg.asyncio, "sleep", new_callable=AsyncMock):
        with pytest.raises(TimedOut):
            await tg._download_bytes(_context(get_file), "voice-file", "voice message")
    assert get_file.await_count == tg.DOWNLOAD_ATTEMPTS


@pytest.mark.asyncio
async def test_lost_voice_message_is_reported_not_routed():
    import telegram_connector as tg

    update = _voice_update()
    context = _context(AsyncMock(side_effect=TimedOut("slow")))
    client = MagicMock()
    client.event = AsyncMock()
    with patch.object(tg, "is_allowed", return_value=True), \
         patch.object(tg.asyncio, "sleep", new_callable=AsyncMock), \
         patch.object(tg, "CLIENT", client):
        await tg.handle_voice(update, context)

    # Nothing reaches the lodge: no transcription, no routing.
    client.event.assert_not_called()
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("couldn't get that voice message" in text for text in replies)


@pytest.mark.asyncio
async def test_failed_ack_does_not_stop_the_voice_message(monkeypatch, tmp_path):
    import chat_lodge
    from connector_loopback import telegram

    tg, side = telegram(monkeypatch, tmp_path / "wolts", owners=("12345",))
    update = _voice_update()
    update.message.reply_text = AsyncMock(side_effect=TimedOut("slow"))
    context = _context(AsyncMock(return_value=_file_returning(b"ogg")))
    transcribe = MagicMock(return_value="hello there")
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(tg, "is_allowed", lambda update: True)
    monkeypatch.setattr(tg.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(chat_lodge, "transcribe_audio", transcribe)
    monkeypatch.setattr(chat_lodge, "_bot_log", lambda *a: None)
    monkeypatch.setattr(side, "_load_chat_state",
                        lambda chat: {"active_wolt": "uxwolt", "active_session": "uxwolt-test-123-abc"})
    monkeypatch.setattr(side, "_route_to_session", route)

    await tg.handle_voice(update, context)

    transcribe.assert_called_once()
    route.assert_called_once()
    assert "hello there" in route.call_args[0][4]
