"""Golden transcripts of the Telegram adapter's commands and buttons.

The connector refactor (RFC #617) moves command semantics into the core and
leaves Telegram to render them. These tests pin what the owner sees today, for
every command, button and refusal, so that move can be checked to be
behavior-identical. Each scenario runs a real handler against a faked lodge and
records every reply, answer and edit, with its buttons.

The expected transcripts live in test/golden/telegram_commands.json. After an
intended change, regenerate with WOLTSPACE_UPDATE_GOLDEN=1 and review the diff.
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

GOLDEN = Path(__file__).resolve().parent / "golden" / "telegram_commands.json"
UPDATE = os.environ.get("WOLTSPACE_UPDATE_GOLDEN") == "1"

OWNER = 111
STRANGER = 999
CHAT = -100
TUNNEL = "https://lodge.example"
NOW = 1_800_000_000

WOLTS = [
    {"name": "pal", "type": "raccoon"},
    {"name": "bud", "type": "beaver"},
    {"name": "ots", "type": "otter"},
    {"name": "rex", "type": "dog"},
    {"name": "howl", "type": "wolf"},
]
SESSIONS = [
    {"name": "pal-maple-a1b2c3", "alive": True, "status": "running", "last_activity": NOW},
    {"name": "bud-birch-b2c3d4", "alive": True, "status": "running", "last_activity": NOW - 60},
    {"name": "ots-cedar-c3d4e5", "alive": False, "status": "stopped", "last_activity": NOW - 3600},
]
APPS = [
    {"name": "blog", "emoji": "📝", "running": True, "tunnel_url": "https://blog.example"},
    {"name": "wiki", "emoji": None, "running": True, "tunnel_url": None},
    {"name": "game", "emoji": "🎮", "running": False},
]


def _markup(markup):
    if markup is None:
        return None
    return [[[b.text, b.url, b.callback_data] for b in row] for row in markup.inline_keyboard]


def _call(kind, args, kwargs):
    entry = {"kind": kind}
    if args:
        entry["text"] = args[0]
    for key in ("text", "parse_mode", "show_alert"):
        if key in kwargs:
            entry[key] = kwargs[key]
    if "reply_markup" in kwargs:
        entry["buttons"] = _markup(kwargs["reply_markup"])
    return entry


class Transcript:
    def __init__(self):
        self.calls = []

    def recorder(self, kind):
        async def record(*args, **kwargs):
            self.calls.append(_call(kind, args, kwargs))
        return record


def _update(transcript, user=OWNER, text=None, callback=None):
    update = MagicMock()
    update.effective_user.id = user
    update.effective_chat.id = CHAT
    update.effective_chat.type = "private"
    update.message.text = text
    update.message.reply_text = AsyncMock(side_effect=transcript.recorder("reply"))
    if callback is None:
        update.callback_query = None
    else:
        query = MagicMock()
        query.data = callback
        query.message.chat.id = CHAT
        query.answer = AsyncMock(side_effect=transcript.recorder("answer"))
        query.edit_message_text = AsyncMock(side_effect=transcript.recorder("edit"))
        update.callback_query = query
    return update


def _context(args=None):
    context = MagicMock()
    context.args = list(args or [])
    return context


HANDLER_COMMANDS = {
    "handle_start": "start", "handle_help": "help", "handle_setwolt": "wolt", "handle_new": "new",
    "handle_sessions": "sessions", "handle_holds": "holds", "handle_idle": "idle",
    "handle_kill": "kill", "handle_apps": "apps",
}


class _Adapter:
    """The Telegram connector's handlers by their old names, and the lodge side's state."""

    def __init__(self, connector, side):
        self.connector, self.side = connector, side

    def handler(self, name):
        if name == "handle_callback":
            return self.connector.handle_callback
        return self.connector.command_handler(HANDLER_COMMANDS[name])

    def _save_chat_state(self, chat, state):
        self.side._save_chat_state(chat, state)

    def _load_chat_state(self, chat):
        return self.side._load_chat_state(chat)


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    """The connector and the lodge side on a temporary lodge: real chat state, faked sessions and apps."""
    import chat_lodge
    from connector_loopback import telegram

    wolts_dir = tmp_path / "wolts"
    for w in WOLTS:
        (wolts_dir / w["name"] / "wolt").mkdir(parents=True)
        (wolts_dir / w["name"] / "wolt" / "wolt.json").write_text(json.dumps({"creature": w["type"]}))
    connector, lodge_side = telegram(monkeypatch, wolts_dir, owners=(str(OWNER),))
    monkeypatch.setattr(lodge_side, "history_dir", tmp_path / "history")
    monkeypatch.setattr(connector, "ALLOWED_USERS", {OWNER})
    monkeypatch.setattr(chat_lodge, "list_wolts", lambda: [dict(w) for w in WOLTS])
    monkeypatch.setattr(chat_lodge, "list_sessions", lambda: [dict(s) for s in SESSIONS])
    monkeypatch.setattr(chat_lodge, "get_tunnel_url", lambda: TUNNEL)
    monkeypatch.setattr(chat_lodge, "dog_available", lambda: False)
    monkeypatch.setattr(lodge_side, "_shown", lambda name: name)
    monkeypatch.setattr(lodge_side, "_session_emoji", lambda name: "🦫")

    side = {"killed": [], "spawned": [], "app_actions": []}

    def kill(name):
        side["killed"].append(name)
        return name != "ghost-session"
    monkeypatch.setattr(chat_lodge, "kill_session", kill)

    def spawn(wolt, chat_id, prompt=""):
        side["spawned"].append(wolt)
        if wolt == "ots":
            raise RuntimeError("engine not logged in")
        return {"name": f"{wolt}-fresh-000001", "url": ""}
    monkeypatch.setattr(lodge_side, "_spawn_session", spawn)

    apps_state = {"fail": False}

    async def fetch_apps():
        if apps_state["fail"]:
            raise RuntimeError("lodge unreachable")
        return [dict(a) for a in APPS]
    monkeypatch.setattr(lodge_side, "_fetch_apps", fetch_apps)

    async def app_action(name, action):
        side["app_actions"].append((name, action))
        return name != "game" or action != "start"
    monkeypatch.setattr(lodge_side, "_app_action", app_action)

    return _Adapter(connector, lodge_side), side, apps_state


def _state(adapter):
    state = adapter._load_chat_state(CHAT)
    return {k: state.get(k) for k in ("active_wolt", "active_session")}


# Scenarios: (id, setup, handler name, command args, callback data, user).
# `setup` seeds the chat state before the handler runs.
SCENARIOS = [
    ("start", {}, "handle_start", [], None, OWNER),
    ("help-empty", {}, "handle_help", [], None, OWNER),
    ("help-active", {"active_wolt": "pal", "active_session": "pal-maple-a1b2c3"}, "handle_help", [], None, OWNER),
    ("wolt-picker-none-active", {}, "handle_setwolt", [], None, OWNER),
    ("wolt-picker-pal-active", {"active_wolt": "pal"}, "handle_setwolt", [], None, OWNER),
    ("wolt-set", {}, "handle_setwolt", ["bud"], None, OWNER),
    ("wolt-unknown", {}, "handle_setwolt", ["nobody"], None, OWNER),
    ("new-no-active", {}, "handle_new", [], None, OWNER),
    ("new-active", {"active_wolt": "pal"}, "handle_new", [], None, OWNER),
    ("new-named", {}, "handle_new", ["bud"], None, OWNER),
    ("new-unknown", {}, "handle_new", ["nobody"], None, OWNER),
    ("new-spawn-fails", {}, "handle_new", ["ots"], None, OWNER),
    ("sessions", {}, "handle_sessions", [], None, OWNER),
    ("kill-usage", {}, "handle_kill", [], None, OWNER),
    ("kill-active", {"active_wolt": "pal", "active_session": "pal-maple-a1b2c3"}, "handle_kill", ["pal-maple-a1b2c3"], None, OWNER),
    ("kill-fails", {}, "handle_kill", ["ghost-session"], None, OWNER),
    ("apps", {}, "handle_apps", [], None, OWNER),
    ("cb-wolt", {"active_wolt": "pal"}, "handle_callback", [], "wolt:bud", OWNER),
    ("cb-wolt-gone", {}, "handle_callback", [], "wolt:nobody", OWNER),
    ("cb-astart-ok", {}, "handle_callback", [], "astart:wiki", OWNER),
    ("cb-astart-fails", {}, "handle_callback", [], "astart:game", OWNER),
    ("cb-astop-ask", {}, "handle_callback", [], "astop:blog", OWNER),
    ("cb-astop-confirm", {}, "handle_callback", [], "astop-confirm:blog", OWNER),
    ("cb-astop-cancel", {}, "handle_callback", [], "astop-cancel", OWNER),
    ("cb-sstop-ask", {}, "handle_callback", [], "sstop:bud-birch-b2c3d4", OWNER),
    ("cb-sstop-confirm", {"active_wolt": "bud", "active_session": "bud-birch-b2c3d4"}, "handle_callback", [], "sstop-confirm:bud-birch-b2c3d4", OWNER),
    ("cb-sstop-fails", {}, "handle_callback", [], "sstop-confirm:ghost-session", OWNER),
    ("cb-sstop-cancel", {}, "handle_callback", [], "sstop-cancel", OWNER),
    ("cb-noop", {}, "handle_callback", [], "noop:wiki", OWNER),
    ("cb-unknown", {}, "handle_callback", [], "mystery:1", OWNER),
]

# Every owner command, run by someone else: nothing is said, nothing happens.
STRANGER_SCENARIOS = [
    (f"stranger-{name}", {"active_wolt": "pal", "active_session": "pal-maple-a1b2c3"}, name, args, cb, STRANGER)
    for name, args, cb in [
        ("handle_start", [], None),
        ("handle_help", [], None),
        ("handle_setwolt", ["bud"], None),
        ("handle_new", ["bud"], None),
        ("handle_sessions", [], None),
        ("handle_kill", ["pal-maple-a1b2c3"], None),
        ("handle_apps", [], None),
        ("handle_holds", [], None),
        ("handle_idle", [], None),
        ("handle_callback", [], "sstop-confirm:pal-maple-a1b2c3"),
        ("handle_callback", [], "astop-confirm:blog"),
        ("handle_callback", [], "wolt:bud"),
    ]
]


def _run(lodge, setup, handler, args, callback, user):
    adapter, side, _apps = lodge
    if setup:
        adapter._save_chat_state(CHAT, dict(setup))
    transcript = Transcript()
    update = _update(transcript, user=user, callback=callback)
    asyncio.run(adapter.handler(handler)(update, _context(args)))
    return {
        "calls": transcript.calls,
        "chat_state": _state(adapter),
        "killed": side["killed"],
        "spawned": side["spawned"],
        "app_actions": [list(a) for a in side["app_actions"]],
    }


def _golden() -> dict:
    return json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}


_RESULTS: dict = {}


@pytest.mark.parametrize("sid,setup,handler,args,callback,user",
                         SCENARIOS + STRANGER_SCENARIOS, ids=lambda v: v if isinstance(v, str) else None)
def test_transcript_matches_golden(lodge, sid, setup, handler, args, callback, user):
    result = _run(lodge, setup, handler, args, callback, user)
    if UPDATE:
        _RESULTS[sid] = result
        data = _golden()
        data[sid] = result
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=True) + "\n")
        return
    expected = _golden().get(sid)
    assert expected is not None, f"no golden transcript for {sid}; run with WOLTSPACE_UPDATE_GOLDEN=1"
    assert result == expected


@pytest.mark.parametrize("sid,setup,handler,args,callback,user", STRANGER_SCENARIOS,
                         ids=lambda v: v if isinstance(v, str) else None)
def test_a_stranger_gets_no_answer_and_changes_nothing(lodge, sid, setup, handler, args, callback, user):
    result = _run(lodge, setup, handler, args, callback, user)
    assert result["calls"] == []
    assert result["killed"] == result["spawned"] == result["app_actions"] == []
    assert result["chat_state"] == {"active_wolt": "pal", "active_session": "pal-maple-a1b2c3"}


def test_apps_failure_is_reported(lodge):
    adapter, _side, apps = lodge
    apps["fail"] = True
    result = _run(lodge, {}, "handle_apps", [], None, OWNER)
    assert result["calls"] == [{"kind": "reply", "text": "couldn't fetch apps: lodge unreachable"}]


def test_the_command_menu_is_pinned():
    """The '/' menu Telegram shows, in order: the lodge's registry, sent in its welcome."""
    import lodge_commands
    assert [(c.name, c.description) for c in lodge_commands.COMMANDS] == [
        ("sessions", "list active sessions"),
        ("holds", "list sessions kept up by a hold"),
        ("idle", "how long idle sessions stay up"),
        ("wolt", "switch or show the active wolt"),
        ("new", "start a fresh session"),
        ("apps", "list apps with public links"),
        ("kill", "stop a session by slug"),
        ("help", "show commands and messaging cheatsheet"),
        ("start", "onboard and show current state"),
    ]


def test_every_menu_command_has_a_handler(monkeypatch):
    """The connector wires a CommandHandler for each command the lodge's welcome lists, and nothing else."""
    import lodge_commands
    from connector_loopback import ROOT as _root  # noqa: F401  (puts the connector on sys.path)
    import telegram_connector

    menu = [(c.name, c.description) for c in lodge_commands.COMMANDS]
    app = telegram_connector.build_app("123:abc", menu)
    registered = sorted(name for group in app.handlers.values() for h in group
                        if h.__class__.__name__ == "CommandHandler" for name in h.commands)
    assert registered == sorted(name for name, _ in menu)


def test_a_custom_creature_wolt_shows_its_own_emoji_and_stays_pickable(lodge, monkeypatch):
    """A custom creature (#633) is a rodent of its size: /wolt lists it, with its own emoji."""
    import chat_lodge

    wolts_dir = Path(os.environ["WOLTSPACE_WOLTS_DIR"])
    (wolts_dir / "woltspace.json").write_text(json.dumps(
        {"creatures": {"custom": {"star": {"emoji": "⭐", "size": "raccoon"}}}}))
    (wolts_dir / "nova" / "wolt").mkdir(parents=True)
    (wolts_dir / "nova" / "wolt" / "wolt.json").write_text(json.dumps({"type": "star"}))
    wolts = WOLTS + [{"name": "nova", "type": "star"}]
    monkeypatch.setattr(chat_lodge, "list_wolts", lambda: [dict(w) for w in wolts])

    picker = _run(lodge, {}, "handle_setwolt", [], None, OWNER)["calls"][0]
    buttons = [b for row in picker["buttons"] for b in row]
    assert ["⭐ nova", None, "wolt:nova"] in buttons
    assert not any(b[2] in ("wolt:rex", "wolt:howl") for b in buttons)   # dog and wolf stay out

    active = _run(lodge, {"active_wolt": "nova"}, "handle_setwolt", [], None, OWNER)["calls"][0]
    assert active["text"].startswith("active: ⭐ nova")
