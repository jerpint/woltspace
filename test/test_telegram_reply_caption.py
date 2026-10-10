"""A reply to a file a wolt sent reaches the session that sent it.

A document carries the reply footer in its caption, not in its text, so a
reply to one used to fall through to the chat's active session.
"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from server.config import DEN_REPLY_FOOTER

SESSION = "nunu-swift-marsh-a1b2c3"
# Exactly what the Telegram sender appends to a notification.
FOOTER = f"\n\n---{DEN_REPLY_FOOTER}\nhttps://abc.trycloudflare.com/tui?session={SESSION}"
FILE_NAME = "weekly-update.html"


def _update(reply_to, *, text="looks good"):
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = 99999
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.reply_to_message = reply_to
    update.message.reply_text = AsyncMock()
    return update


def _file_message(caption):
    reply_to = MagicMock()
    reply_to.text = None
    reply_to.caption = caption
    reply_to.document.file_name = FILE_NAME
    return reply_to


async def _routed_by_handle_message(update):
    from bot.telegram_adapter import handle_message

    context = MagicMock()
    context.bot.username = "testbot"
    with patch("bot.telegram_adapter.is_allowed", return_value=True), \
         patch("bot.telegram_adapter._is_session_alive", return_value=True), \
         patch("bot.telegram_adapter._load_chat_state", return_value={}), \
         patch("bot.telegram_adapter._save_chat_state"), \
         patch("bot.telegram_adapter._notify_switch", new_callable=AsyncMock), \
         patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock, return_value=SESSION) as route:
        await handle_message(update, context)
    route.assert_called_once()
    return route.call_args[0][1:4]


@pytest.mark.asyncio
async def test_reply_to_a_file_with_a_caption_routes_to_the_session():
    update = _update(_file_message("🦦 nunu: the weekly update" + FOOTER))
    session, wolt, message = await _routed_by_handle_message(update)
    assert (session, wolt) == (SESSION, "nunu")
    assert message.startswith(f"[replying to file {FILE_NAME}: 🦦 nunu: the weekly update")
    assert message.endswith("]\nlooks good")
    assert DEN_REPLY_FOOTER.strip() not in message


@pytest.mark.asyncio
async def test_reply_to_a_file_whose_caption_is_only_the_footer():
    update = _update(_file_message(FOOTER.strip()))
    session, wolt, message = await _routed_by_handle_message(update)
    assert (session, wolt) == (SESSION, "nunu")
    assert message == f"[replying to file {FILE_NAME}]\nlooks good"


@pytest.mark.asyncio
async def test_reply_to_a_text_message_is_quoted_as_before():
    reply_to = MagicMock()
    reply_to.text = "🦦 nunu: here's your playlist!" + FOOTER
    session, wolt, message = await _routed_by_handle_message(_update(reply_to, text="love this track"))
    assert (session, wolt) == (SESSION, "nunu")
    assert message == "[replying to: 🦦 nunu: here's your playlist!\n]\nlove this track"


@pytest.mark.asyncio
async def test_voice_reply_to_a_file_routes_to_the_session():
    from bot import telegram_adapter as tg

    update = _update(_file_message("🦦 nunu: the weekly update" + FOOTER))
    update.message.voice.file_id = "voice-file"
    update.message.voice.file_unique_id = "voice-unique"
    file = MagicMock()
    file.download_as_bytearray = AsyncMock(return_value=bytearray(b"ogg"))
    context = MagicMock()
    context.bot.get_file = AsyncMock(return_value=file)
    with patch("bot.telegram_adapter.is_allowed", return_value=True), \
         patch("bot.telegram_adapter.asyncio.sleep", new_callable=AsyncMock), \
         patch("bot.telegram_adapter._save_upload"), \
         patch("bot.telegram_adapter.transcribe_audio", return_value="hello there"), \
         patch("bot.telegram_adapter._load_chat_state", return_value={"active_wolt": "uxwolt", "active_session": "uxwolt-test-123-abc"}), \
         patch("bot.telegram_adapter._save_chat_state"), \
         patch("bot.telegram_adapter._notify_switch", new_callable=AsyncMock), \
         patch("bot.telegram_adapter._is_session_alive", return_value=True), \
         patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock, return_value=SESSION) as route:
        await tg.handle_voice(update, context)

    route.assert_called_once()
    session, wolt, message = route.call_args[0][1:4]
    assert (session, wolt) == (SESSION, "nunu")
    assert message.startswith(f"[replying to file {FILE_NAME}: 🦦 nunu: the weekly update")
    assert message.endswith("]\n[voice message] hello there")


@pytest.mark.asyncio
@pytest.mark.parametrize("text, routed", [
    ("🦦 nunu: the weekly update", f"[replying to file {FILE_NAME}: 🦦 nunu: the weekly update\n]\nok"),
    ("🦦 nunu: " + "long " * 300, f"[replying to file {FILE_NAME}]\nok"),
], ids=["text-fits-the-caption", "text-went-first"])
async def test_a_caption_the_telegram_sender_wrote_routes_the_reply(text, routed, tmp_path):
    from bot.telegram_adapter import _parse_session_from_reply, _quote_reply, _reply_source
    from server.notification_senders import telegram_sender
    from server.notification_types import Attachment, OutboundMessage, SendContext

    document = AsyncMock()
    send = telegram_sender(AsyncMock(), document)
    await send(
        OutboundMessage(text, f"https://abc.trycloudflare.com/tui?session={SESSION}", (
            Attachment(tmp_path / "entry", FILE_NAME, 11, "text/html"),
        )),
        {"chat_id": "42"}, SendContext({"TELEGRAM_BOT_TOKEN": "t"}, {}),
    )
    caption = document.await_args.args[3]

    body, name = _reply_source(
        SimpleNamespace(text=None, caption=caption, document=SimpleNamespace(file_name=FILE_NAME)),
    )
    assert _parse_session_from_reply(body) == SESSION
    assert _quote_reply(body, name, "ok") == routed


@pytest.mark.parametrize("reply_to, expected", [
    (SimpleNamespace(text="done" + FOOTER, caption=None, document=None), ("done" + FOOTER, None)),
    (SimpleNamespace(text=None, caption="done" + FOOTER, document=SimpleNamespace(file_name="a.html")),
     ("done" + FOOTER, "a.html")),
    (SimpleNamespace(text="", caption="the caption", document=SimpleNamespace(file_name="")),
     ("the caption", None)),
    (SimpleNamespace(text=None, caption=None, document=SimpleNamespace(file_name=None)), ("", None)),
])
def test_reply_source_reads_text_then_caption_and_the_file_name(reply_to, expected):
    from bot.telegram_adapter import _reply_source

    assert _reply_source(reply_to) == expected


def test_reply_source_ignores_attributes_that_are_not_strings():
    from bot.telegram_adapter import _reply_source

    assert _reply_source(MagicMock()) == ("", None)
    with_text = MagicMock()
    with_text.text = "done" + FOOTER
    assert _reply_source(with_text) == ("done" + FOOTER, None)


@pytest.mark.parametrize("body, file_name, expected", [
    ("done" + FOOTER, "a.html", "[replying to file a.html: done\n]\nok"),
    (FOOTER.strip(), "a.html", "[replying to file a.html]\nok"),
    ("done" + FOOTER, None, "[replying to: done\n]\nok"),
    (FOOTER.strip(), None, "ok"),
    ("  " + FOOTER, "a.html", "[replying to file a.html]\nok"),
    ("  " + FOOTER, None, "ok"),
    ("x" * 300 + FOOTER, "a.html", "[replying to file a.html: " + "x" * 200 + "]\nok"),
    ("x" * 300 + FOOTER, None, "[replying to: " + "x" * 200 + "]\nok"),
    ("no footer here", None, "[replying to: no footer here]\nok"),
], ids=[
    "file-and-text", "file-footer-only", "text", "footer-only", "file-blank-text",
    "blank-text", "file-long-text", "long-text", "no-footer",
])
def test_quote_reply_names_the_file_and_drops_the_footer(body, file_name, expected):
    from bot.telegram_adapter import _quote_reply

    assert _quote_reply(body, file_name, "ok") == expected
