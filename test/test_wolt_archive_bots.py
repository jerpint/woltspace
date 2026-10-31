"""A chat or thread pointing at a wolt whose folder is gone lets go of it.

Archived, or removed by hand: Telegram drops the chosen wolt and goes to the
picker, a reply to that wolt's old message is routed like any message, and a
Slack thread it owned is unowned. Nothing is spawned for the gone wolt.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

CHAT = 123
GONE = "pal-quiet-reed-123456"
LIVE = "wolt-a-swift-marsh-abc123"


@pytest.fixture
def telegram(monkeypatch, tmp_path):
    # bot.core makes its log dir under the wolts dir at import: keep it here.
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    import chat_lodge
    from connector_loopback import Pair, telegram as wire_telegram

    wolts = tmp_path / "wolts"
    for name in ("wolt-a", "kit"):
        (wolts / name / "wolt").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name, "type": "beaver"}))
    connector, side = wire_telegram(monkeypatch, wolts, owners=("12345",))
    start = MagicMock(return_value={"name": "pal-new-reed-654321", "url": ""})
    deliver = MagicMock(side_effect=lambda name, msg: {"ok": name == LIVE, "url": ""})
    monkeypatch.setattr(side, "history_dir", tmp_path / "history")
    monkeypatch.setattr(side, "_inside_uploads", lambda path: True)
    for name, value in [
            ("list_wolts", lambda: [{"name": n, "type": "beaver", "dir": str(wolts / n)} for n in ("wolt-a", "kit")]),
            ("get_tunnel_url", lambda: ""), ("dog_available", lambda: False),
            ("transcribe_audio", lambda path: "hi"),
            ("start_claude_session", start), ("message_session", deliver)]:
        monkeypatch.setattr(chat_lodge, name, value)
    for name, value in [
            ("is_allowed", lambda update: True), ("_download_bytes", AsyncMock(return_value=b"ogg")),
            ("_save_upload", lambda name, data: tmp_path / name)]:
        monkeypatch.setattr(connector, name, value)
    adapter = Pair(connector, side)
    return adapter, wolts, start, deliver


async def _handle(adapter, kind="message", reply_to_gone=False):
    update = MagicMock()
    update.effective_user.id, update.effective_chat.id, update.effective_chat.type = 12345, CHAT, "private"
    update.message.text, update.message.reply_text, update.message.reply_to_message = "hi", AsyncMock(), None
    update.message.voice.file_id, update.message.voice.file_unique_id = "f1", "u1"
    if reply_to_gone:
        update.message.reply_to_message = MagicMock(caption=None, text=(
            "🦫 pal: done\n\n---\n↩️ reply to this message to talk to this session directly\n"
            f"https://x.trycloudflare.com/tui?session={GONE}"))
    context = MagicMock()
    context.bot.username = "testbot"
    context.bot.id = 4242
    if reply_to_gone:
        update.message.reply_to_message.from_user.id = 4242  # the bot's own notification
    await getattr(adapter, f"handle_{kind}")(update, context)
    return update


@pytest.mark.asyncio
async def test_chat_pointing_at_a_gone_wolt_goes_to_the_picker(telegram):
    adapter, wolts, start, deliver = telegram
    adapter._save_chat_state(CHAT, {"active_wolt": "pal", "active_session": GONE})

    update = await _handle(adapter)

    start.assert_not_called()
    deliver.assert_not_called()
    replies = [c.args[0] for c in update.message.reply_text.await_args_list]
    import lodge_messages
    assert replies[-1] == lodge_messages.PICK_THEN_RESEND
    assert "active_wolt" not in adapter._load_chat_state(CHAT)
    assert not (wolts / "pal").exists()


def test_a_wolt_chosen_by_its_json_name_stays_chosen(telegram):
    adapter, wolts, start, deliver = telegram
    (wolts / "nunu-dir" / "wolt").mkdir(parents=True)
    (wolts / "nunu-dir" / "wolt" / "wolt.json").write_text(json.dumps({"name": "nunu"}))
    adapter._save_chat_state(CHAT, {"active_wolt": "nunu", "active_session": LIVE})

    assert adapter._load_chat_state(CHAT)["active_wolt"] == "nunu"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["message", "voice"])
async def test_reply_to_an_archived_wolts_message_is_routed_normally(telegram, kind):
    adapter, wolts, start, deliver = telegram
    adapter._save_chat_state(CHAT, {"active_wolt": "wolt-a", "active_session": LIVE})

    await _handle(adapter, kind, reply_to_gone=True)

    assert [c.args[0] for c in deliver.call_args_list] == [LIVE]
    start.assert_not_called()
    assert adapter._load_chat_state(CHAT)["active_wolt"] == "wolt-a"
    assert not (wolts / "pal").exists()


def test_slack_thread_owned_by_a_gone_wolt_is_unowned(monkeypatch, tmp_path):
    # bot.core makes its log dir under the wolts dir at import: keep it here.
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    from bot import slack_adapter as slack

    (tmp_path / "wolt-a" / "wolt").mkdir(parents=True)
    (tmp_path / "wolt-a" / "wolt" / "wolt.json").write_text("{}")
    monkeypatch.setattr(slack, "_WOLTS_DIR", tmp_path)
    slack._set_session_owner("C1", "1.1", GONE, "pal", "beaver")
    slack._set_session_owner("C1", "2.2", LIVE, "wolt-a", "beaver")

    assert slack._get_session_owner("C1", "1.1") is None
    assert slack._get_session_owner("C1", "2.2")["session"] == LIVE
