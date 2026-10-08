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
    from bot import telegram_adapter as tg

    get_file = AsyncMock(side_effect=[TimedOut("slow"), _file_returning(b"ogg")])
    with patch("bot.telegram_adapter.asyncio.sleep", new_callable=AsyncMock):
        data = await tg._download_bytes(_context(get_file), "voice-file", "voice message")
    assert data == b"ogg"
    assert get_file.await_count == 2


@pytest.mark.asyncio
async def test_download_gives_up_after_the_last_attempt():
    from bot import telegram_adapter as tg

    get_file = AsyncMock(side_effect=TimedOut("slow"))
    with patch("bot.telegram_adapter.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(TimedOut):
            await tg._download_bytes(_context(get_file), "voice-file", "voice message")
    assert get_file.await_count == tg.DOWNLOAD_ATTEMPTS


@pytest.mark.asyncio
async def test_lost_voice_message_is_reported_not_routed():
    from bot import telegram_adapter as tg

    update = _voice_update()
    context = _context(AsyncMock(side_effect=TimedOut("slow")))
    with patch("bot.telegram_adapter.is_allowed", return_value=True), \
         patch("bot.telegram_adapter.asyncio.sleep", new_callable=AsyncMock), \
         patch("bot.telegram_adapter.transcribe_audio") as transcribe, \
         patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock) as route:
        await tg.handle_voice(update, context)

    transcribe.assert_not_called()
    route.assert_not_called()
    replies = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("couldn't get that voice message" in text for text in replies)


@pytest.mark.asyncio
async def test_failed_ack_does_not_stop_the_voice_message():
    from bot import telegram_adapter as tg

    update = _voice_update()
    update.message.reply_text = AsyncMock(side_effect=TimedOut("slow"))
    context = _context(AsyncMock(return_value=_file_returning(b"ogg")))
    with patch("bot.telegram_adapter.is_allowed", return_value=True), \
         patch("bot.telegram_adapter.asyncio.sleep", new_callable=AsyncMock), \
         patch("bot.telegram_adapter._save_upload"), \
         patch("bot.telegram_adapter.transcribe_audio", return_value="hello there") as transcribe, \
         patch("bot.telegram_adapter._load_chat_state", return_value={"active_wolt": "uxwolt", "active_session": "uxwolt-test-123-abc"}), \
         patch("bot.telegram_adapter._is_session_alive", return_value=True), \
         patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock, return_value=True) as route:
        await tg.handle_voice(update, context)

    transcribe.assert_called_once()
    route.assert_called_once()
    assert "hello there" in route.call_args[0][3]
