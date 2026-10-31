"""Golden transcripts of how the Telegram adapter routes a message.

Text, voice, photo, video and document messages, in every chat state: no wolt
chosen, a wolt but no session, a live session, a revived one, a gone one with
a live sibling, a gone one with none, and a failing spawn. With and without
the dog, as a reply to a wolt's notification, and in group chats.

Each scenario records what the owner is told, what each session is sent,
which sessions start (with their opening prompt), what the dog is asked, the
chat's history and its state afterwards. The connector refactor (RFC #617)
moves this routing into the core; these transcripts say what must not change,
and any intended change shows up as a reviewed diff.

Regenerate with WOLTSPACE_UPDATE_GOLDEN=1.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import golden_store

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

GOLDEN = Path(__file__).resolve().parent / "golden" / "telegram_messages.json"
UPDATE = os.environ.get("WOLTSPACE_UPDATE_GOLDEN") == "1"

CHAT = 4242
BOT_ID = 777
BOT_NAME = "lodgebot"
TUNNEL = "https://lodge.example"
WOLTS3 = [{"name": "pal", "type": "raccoon"}, {"name": "bud", "type": "beaver"},
          {"name": "ots", "type": "otter"}, {"name": "rex", "type": "dog"}]
WOLTS1 = [{"name": "pal", "type": "raccoon"}, {"name": "rex", "type": "dog"}]

LIVE = "pal-live-grove-000001"
REVIVE = "pal-sleepy-grove-000002"
GONE_WITH_SIBLING = "pal-gone-grove-000003"
GONE = "bud-gone-grove-000004"
OTS_GONE = "ots-gone-grove-000005"
FOOTER = "\n\n---\n↩️ reply to this message to talk to this session directly\n" + TUNNEL + "/tui?session="

STATES = {
    "none": {},
    "wolt": {"active_wolt": "pal"},
    "live": {"active_wolt": "pal", "active_session": LIVE},
    "revive": {"active_wolt": "pal", "active_session": REVIVE},
    "sibling": {"active_wolt": "pal", "active_session": GONE_WITH_SIBLING},
    "gone": {"active_wolt": "bud", "active_session": GONE},
    "spawnfail": {"active_wolt": "ots", "active_session": OTS_GONE},
}
KINDS = ("text", "voice", "photo", "video", "document")


def _record(store, kind):
    async def record(*args, **kwargs):
        entry = {"kind": kind}
        if args:
            entry["text"] = args[0]
        for key in ("caption", "parse_mode"):
            if key in kwargs:
                entry[key] = kwargs[key]
        if kwargs.get("reply_markup") is not None:
            entry["buttons"] = [[[b.text, b.callback_data] for b in row]
                                for row in kwargs["reply_markup"].inline_keyboard]
        store.append(entry)
    return record


def _update(store, kind, text="hello there", chat_type="private", reply_to_session=None,
            reply_from_bot=False):
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = CHAT
    update.effective_chat.type = chat_type
    msg = update.message
    msg.text = text if kind == "text" else None
    msg.caption = "look" if kind in ("photo", "video", "document") else None
    msg.reply_text = AsyncMock(side_effect=_record(store, "reply"))
    msg.reply_photo = AsyncMock(side_effect=_record(store, "photo"))
    msg.reply_to_message = None
    if reply_to_session:
        r = MagicMock()
        r.text = "🦝 pal: done" + FOOTER + reply_to_session
        r.caption = None
        r.document = None
        r.audio = None
        r.voice = None
        r.from_user.id = BOT_ID
        msg.reply_to_message = r
    elif reply_from_bot:
        r = MagicMock()
        r.text = "plain"
        r.caption = None
        r.document = None
        r.audio = None
        r.voice = None
        r.from_user.id = BOT_ID
        msg.reply_to_message = r
    msg.voice = None
    msg.audio = None
    msg.photo = None
    msg.video = None
    msg.animation = None
    msg.video_note = None
    msg.document = None
    if kind == "voice":
        msg.voice = MagicMock(file_id="f1", file_unique_id="u1")
    elif kind == "photo":
        msg.photo = [MagicMock(file_id="f1", file_unique_id="u1")]
    elif kind == "video":
        msg.video = MagicMock(file_id="f1", file_unique_id="u1", mime_type="video/mp4", file_name="clip.mp4")
    elif kind == "document":
        msg.document = MagicMock(file_id="f1", file_unique_id="u1", file_name="note.txt", mime_type="text/plain")
    return update


HANDLERS = {"text": "handle_message", "voice": "handle_voice", "photo": "handle_photo",
            "video": "handle_video", "document": "handle_document"}


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    import chat_lodge
    from connector_loopback import telegram

    wolts_dir = tmp_path / "wolts"
    for name, creature in (("pal", "raccoon"), ("bud", "beaver"), ("ots", "otter"), ("rex", "dog")):
        (wolts_dir / name / "wolt").mkdir(parents=True)
        (wolts_dir / name / "wolt" / "wolt.json").write_text(json.dumps({"type": creature}))
    connector, side = telegram(monkeypatch, wolts_dir)
    monkeypatch.setattr(side, "history_dir", tmp_path / "history")
    monkeypatch.setattr(connector, "is_allowed", lambda update: True)
    monkeypatch.setattr(chat_lodge, "get_tunnel_url", lambda: TUNNEL)
    monkeypatch.setattr(side, "_shown", lambda name: name)
    monkeypatch.setattr(connector, "_download_bytes", AsyncMock(return_value=b"bytes!"))
    monkeypatch.setattr(connector, "_save_upload", lambda name, data: Path("/uploads") / name)
    monkeypatch.setattr(side, "_inside_uploads", lambda path: True)
    monkeypatch.setattr(side, "_read_upload", lambda path: b"bytes!")
    monkeypatch.setattr(chat_lodge, "transcribe_audio", lambda path: "spoken words")
    monkeypatch.setattr(chat_lodge, "notify_reply_instruction",
                        lambda *a: f"<reply via notify {' '.join(map(str, a))}>")
    monkeypatch.setattr(side, "_start_chat_prompt", lambda wolt: f"/start-chat telegram {wolt}")
    monkeypatch.setattr(chat_lodge.random, "choice", lambda seq: seq[0])
    monkeypatch.setattr(chat_lodge, "_bot_log", lambda event, data: None)

    log = {"said": [], "sent": [], "spawned": [], "dog": []}
    world = {"dog": False, "wolts": WOLTS3}
    monkeypatch.setattr(chat_lodge, "dog_available", lambda: world["dog"])
    monkeypatch.setattr(chat_lodge, "list_wolts", lambda: [dict(w) for w in world["wolts"]])

    def message_session(name, msg):
        log["sent"].append([name, msg])
        if name == LIVE:
            return {"ok": True, "url": f"{TUNNEL}/tui?session={LIVE}"}
        if name == REVIVE:
            return {"ok": True, "status": "revived", "url": ""}
        return {"ok": False}
    monkeypatch.setattr(chat_lodge, "message_session", message_session)
    monkeypatch.setattr(chat_lodge, "resolve_active_session",
                        lambda wolt: LIVE if wolt == "pal" else None)

    def start(prompt, wolt=None, routing=None, **_):
        log["spawned"].append({"wolt": wolt, "prompt": prompt, "routing": routing})
        if wolt == "ots":
            raise RuntimeError("engine not logged in")
        return {"name": f"{wolt}-new-grove-000009", "url": ""}
    monkeypatch.setattr(chat_lodge, "start_claude_session", start)

    def get_response(message, conversation_history=None, routing=None, user_content=None):
        log["dog"].append({"message": message, "image": user_content is not None, "routing": routing})
        if "spawn" in message:
            return {"type": "session", "text": "on it",
                    "session": {"name": "bud-dog-grove-000010", "wolt": "bud", "creature": "beaver", "url": ""},
                    "history_messages": [{"role": "assistant", "content": "on it"}]}
        return {"type": "text", "text": "woof", "history_messages": [{"role": "assistant", "content": "woof"}]}
    monkeypatch.setattr(chat_lodge, "get_response", get_response)
    monkeypatch.setattr(side, "format_response", lambda result: result.get("text", ""))
    return (connector, side), log, world, tmp_path


def _history(tmp_path):
    path = tmp_path / "history" / f"{CHAT}.jsonl"
    if not path.exists():
        return []
    return [{k: v for k, v in json.loads(line).items() if k != "ts"} for line in path.read_text().splitlines()]


def _run(lodge, kind, state, dog, wolts=WOLTS3, text="hello there", chat_type="private",
         reply_to_session=None, reply_from_bot=False):
    (adapter, side), log, world, tmp_path = lodge
    world["dog"] = dog
    world["wolts"] = wolts
    if state:
        side._save_chat_state(CHAT, dict(state))
    said = []
    update = _update(said, kind, text=text, chat_type=chat_type,
                     reply_to_session=reply_to_session, reply_from_bot=reply_from_bot)
    context = MagicMock()
    context.bot.username = BOT_NAME
    context.bot.id = BOT_ID
    asyncio.run(getattr(adapter, HANDLERS[kind])(update, context))
    final = side._load_chat_state(CHAT)
    return {
        "said": said,
        "sent": log["sent"],
        "spawned": log["spawned"],
        "dog": log["dog"],
        "history": _history(tmp_path),
        "chat_state": {k: final.get(k) for k in ("active_wolt", "active_session")},
    }


SCENARIOS = []
for kind in KINDS:
    for state in STATES:
        for dog in (False, True):
            SCENARIOS.append((f"{kind}-{state}-{'dog' if dog else 'nodog'}", kind, state, dog, {}))
    for dog in (False, True):
        tag = "dog" if dog else "nodog"
        SCENARIOS.append((f"{kind}-none-onewolt-{tag}", kind, "none", dog, {"wolts": WOLTS1}))
        SCENARIOS.append((f"{kind}-reply-live-while-bud-{tag}", kind, "gone", dog, {"reply_to_session": LIVE}))
        SCENARIOS.append((f"{kind}-reply-gone-{tag}", kind, "live", dog, {"reply_to_session": GONE}))
        SCENARIOS.append((f"{kind}-reply-unknown-wolt-{tag}", kind, "live", dog,
                          {"reply_to_session": "zed-old-grove-000011"}))
SCENARIOS += [
    ("text-group-quiet", "text", "none", False, {"chat_type": "group"}),
    ("text-group-active", "text", "live", False, {"chat_type": "group"}),
    ("text-group-mention-dog", "text", "none", True, {"chat_type": "group", "text": f"@{BOT_NAME} hi"}),
    ("text-group-mention-nodog", "text", "wolt", False, {"chat_type": "group", "text": f"@{BOT_NAME} hi"}),
    ("text-group-reply-to-bot", "text", "wolt", False, {"chat_type": "supergroup", "reply_from_bot": True}),
    ("text-mention-dog-private", "text", "live", True, {"text": f"@{BOT_NAME} help"}),
    ("text-dog-spawns", "text", "none", True, {"text": "please spawn bud"}),
]


def _golden() -> dict:
    return golden_store.load(GOLDEN)


@pytest.mark.parametrize("sid,kind,state,dog,extra", SCENARIOS, ids=[s[0] for s in SCENARIOS])
def test_message_routing_matches_golden(lodge, sid, kind, state, dog, extra):
    result = _run(lodge, kind, STATES[state], dog, **extra)
    if UPDATE:
        data = _golden()
        data[sid] = result
        golden_store.save(GOLDEN, data)
        return
    expected = _golden().get(sid)
    assert expected is not None, f"no golden transcript for {sid}; run with WOLTSPACE_UPDATE_GOLDEN=1"
    assert result == expected


@pytest.mark.parametrize("kind", KINDS)
def test_a_footer_in_someone_elses_message_is_not_followed(lodge, kind):
    """In a group anyone can paste a session footer into their own message.
    Only this bot's notifications route a reply to the session they name."""
    (adapter, side), log, world, tmp_path = lodge
    side._save_chat_state(CHAT, dict(STATES["live"]))
    said = []
    update = _update(said, kind, chat_type="group", reply_to_session=GONE)
    update.message.reply_to_message.from_user.id = 31337  # another member, not the bot
    context = MagicMock()
    context.bot.username = BOT_NAME
    context.bot.id = BOT_ID
    asyncio.run(getattr(adapter, HANDLERS[kind])(update, context))
    assert [name for name, _msg in log["sent"]] == [LIVE]
    assert log["spawned"] == []
    assert side._load_chat_state(CHAT)["active_session"] == LIVE


@pytest.mark.parametrize("kind", KINDS)
def test_a_stranger_media_message_reaches_nothing(lodge, monkeypatch, kind):
    (adapter, side), log, world, tmp_path = lodge
    monkeypatch.setattr(adapter, "is_allowed", lambda update: False)
    side._save_chat_state(CHAT, dict(STATES["live"]))
    said = []
    update = _update(said, kind, reply_to_session=LIVE)
    context = MagicMock()
    context.bot.username = BOT_NAME
    context.bot.id = BOT_ID
    asyncio.run(getattr(adapter, HANDLERS[kind])(update, context))
    assert said == [] and log["sent"] == [] and log["spawned"] == [] and log["dog"] == []
