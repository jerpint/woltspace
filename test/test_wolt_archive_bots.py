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
LIVE = "bob-swift-marsh-abc123"


@pytest.fixture
def telegram(monkeypatch, tmp_path):
    from bot import telegram_adapter as adapter

    wolts = tmp_path / "wolts"
    for name in ("bob", "kit"):
        (wolts / name / "wolt").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name, "type": "beaver"}))
    monkeypatch.setattr(adapter, "_WOLTS_DIR", wolts)
    monkeypatch.setattr(adapter, "CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr(adapter, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(adapter, "list_wolts", lambda: [
        {"name": n, "type": "beaver", "dir": str(wolts / n)} for n in ("bob", "kit")])
    for name, value in [("is_allowed", lambda update: True), ("get_tunnel_url", lambda: ""),
                        ("dog_available", lambda: False), ("transcribe_audio", lambda path: "hi"),
                        ("_download_bytes", AsyncMock(return_value=b"ogg")),
                        ("_save_upload", lambda name, data: tmp_path / name)]:
        monkeypatch.setattr(adapter, name, value)
    start = MagicMock(return_value={"name": "pal-new-reed-654321", "url": ""})
    monkeypatch.setattr(adapter, "start_claude_session", start)
    deliver = MagicMock(side_effect=lambda name, msg: {"ok": name == LIVE, "url": ""})
    monkeypatch.setattr(adapter, "message_session", deliver)
    return adapter, wolts, start, deliver


def _update(kind="message", reply_to_gone=False):
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = CHAT
    update.effective_chat.type = "private"
    update.message.text = "hi"
    update.message.reply_text = AsyncMock()
    update.message.reply_to_message = None
    update.message.voice.file_id = "f1"
    update.message.voice.file_unique_id = "u1"
    if reply_to_gone:
        update.message.reply_to_message = MagicMock(caption=None, text=(
            "🦫 pal: done\n\n---\n↩️ reply to this message to talk to this session directly\n"
            f"https://x.trycloudflare.com/tui?session={GONE}"))
    return update


async def _handle(adapter, update, kind="message"):
    context = MagicMock()
    context.bot.username = "testbot"
    await getattr(adapter, f"handle_{kind}")(update, context)


@pytest.mark.asyncio
async def test_chat_pointing_at_a_gone_wolt_goes_to_the_picker(telegram):
    adapter, wolts, start, deliver = telegram
    adapter._save_chat_state(CHAT, {"active_wolt": "pal", "active_session": GONE})
    update = _update()

    await _handle(adapter, update)

    start.assert_not_called()
    deliver.assert_not_called()
    replies = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert replies[-1] == adapter._PICK_THEN_RESEND
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
    adapter._save_chat_state(CHAT, {"active_wolt": "bob", "active_session": LIVE})

    await _handle(adapter, _update(kind, reply_to_gone=True), kind)

    assert [c.args[0] for c in deliver.call_args_list] == [LIVE]
    start.assert_not_called()
    assert adapter._load_chat_state(CHAT)["active_wolt"] == "bob"
    assert not (wolts / "pal").exists()


def test_slack_thread_owned_by_a_gone_wolt_is_unowned(monkeypatch, tmp_path):
    from bot import slack_adapter as slack

    (tmp_path / "bob" / "wolt").mkdir(parents=True)
    (tmp_path / "bob" / "wolt" / "wolt.json").write_text("{}")
    monkeypatch.setattr(slack, "_WOLTS_DIR", tmp_path)
    monkeypatch.setattr(slack, "_thread_sessions", {
        slack._thread_key("C1", "1.1"): {"session": GONE, "wolt": "pal", "creature": "beaver"},
        slack._thread_key("C1", "2.2"): {"session": LIVE, "wolt": "bob", "creature": "beaver"},
    })

    assert slack._get_session_owner("C1", "1.1") is None
    assert slack._get_session_owner("C1", "2.2")["session"] == LIVE
