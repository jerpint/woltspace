"""A Telegram message that spawns a session is in that session's opening prompt.

Pasting it into the new pane a second after the spawn lost it: the agent was
not reading input yet (#555). Every handler path that spawns for an incoming
message now starts the session with start-chat followed by the message, and
pastes nothing. Messages to a running session still go through
message_session.
"""

import json
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

CHAT = 123
WOLT = "nunu"
OLD = "nunu-old-grove-abc123"
NEW = "nunu-new-grove-def456"
FOOTER = (
    "\n\n---\n↩️ reply to this message to talk to this session directly\n"
    f"https://abc.trycloudflare.com/tui?session={OLD}"
)


def _reply_instructions(text: str) -> int:
    """How many notify reply instructions a prompt carries (each has its own delimiter)."""
    return len(re.findall(r"<<'WOLTSPACE_NOTIFY_[A-F0-9]{16}'", text))


# id, handler, dog available, chat state before, replying to OLD's notification,
# a fragment of the message the session must read
SPAWN_SITES = [
    ("text-reply-to-expired", "message", False, {}, True, "hello"),
    ("text-expired", "message", False, {"active_wolt": WOLT, "active_session": OLD}, False, "hello"),
    ("text-no-session", "message", False, {"active_wolt": WOLT}, False, "hello"),
    ("voice-reply-to-expired", "voice", True, {}, True, "[voice message] hello"),
    ("voice-expired", "voice", True, {"active_wolt": WOLT, "active_session": OLD}, False, "[voice message] hello"),
    ("voice-no-session", "voice", True, {"active_wolt": WOLT}, False, "[voice message] hello"),
    ("voice-no-dog-no-session", "voice", False, {"active_wolt": WOLT}, False, "[voice message] hello"),
    ("photo-expired", "photo", True, {"active_wolt": WOLT, "active_session": OLD}, False, "[image received] photo_u1.jpg"),
    ("photo-no-session", "photo", True, {"active_wolt": WOLT}, False, "[image received] photo_u1.jpg"),
    ("video-expired", "video", False, {"active_wolt": WOLT, "active_session": OLD}, False, "[video received] clip.mp4"),
    ("video-no-session", "video", False, {"active_wolt": WOLT}, False, "[video received] clip.mp4"),
    ("document-expired", "document", True, {"active_wolt": WOLT, "active_session": OLD}, False, "[file received] note.txt"),
    ("document-no-session", "document", True, {"active_wolt": WOLT}, False, "[file received] note.txt"),
]

# id, handler, dog available
RUNNING_SITES = [
    ("text", "message", False),
    ("voice", "voice", True),
    ("voice-no-dog", "voice", False),
    ("photo", "photo", True),
    ("video", "video", False),
    ("document", "document", True),
]


def _update(kind: str, replying: bool):
    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_chat.id = CHAT
    update.effective_chat.type = "private"
    msg = update.message
    msg.text = "hello"
    msg.caption = "see this"
    msg.reply_text = AsyncMock()
    msg.reply_to_message = None
    if replying:
        reply_to = MagicMock()
        reply_to.text = "🦦 nunu: done" + FOOTER
        reply_to.caption = None
        msg.reply_to_message = reply_to
    if kind == "voice":
        msg.voice.file_id = "f1"
        msg.voice.file_unique_id = "u1"
    elif kind == "photo":
        msg.photo = [MagicMock(file_id="f1", file_unique_id="u1")]
    elif kind == "video":
        msg.video.file_id = "f1"
        msg.video.mime_type = "video/mp4"
        msg.video.file_name = "clip.mp4"
    elif kind == "document":
        msg.document.file_id = "f1"
        msg.document.file_name = "note.txt"
        msg.document.mime_type = "text/plain"
    return update


@pytest.fixture
def lodge(monkeypatch, tmp_path):
    """The adapter with its chat state and history on disk, sessions faked.

    message_session finds OLD gone and delivers to anything else, so the
    untouched spawn-then-paste code runs to the end instead of crashing.
    """
    from bot import telegram_adapter as adapter

    monkeypatch.setattr(adapter, "_WOLTS_DIR", tmp_path / "wolts")
    monkeypatch.setattr(adapter, "CHATS_DIR", tmp_path / "chats")
    monkeypatch.setattr(adapter, "HISTORY_DIR", tmp_path / "history")
    monkeypatch.setattr(adapter, "is_allowed", lambda update: True)
    monkeypatch.setattr(adapter, "get_tunnel_url", lambda: "")
    monkeypatch.setattr(adapter, "resolve_active_session", lambda wolt: None)
    monkeypatch.setattr(adapter, "_download_bytes", AsyncMock(return_value=b"content"))
    monkeypatch.setattr(adapter, "_save_upload", lambda name, data: tmp_path / "uploads" / name)
    monkeypatch.setattr(adapter, "transcribe_audio", lambda path: "hello")
    monkeypatch.setattr(adapter, "get_response", MagicMock(side_effect=AssertionError("no model call")))
    events = []
    monkeypatch.setattr(adapter, "_bot_log", lambda event, data: events.append((event, data)))
    start = MagicMock(return_value={"name": NEW, "url": ""})
    monkeypatch.setattr(adapter, "start_claude_session", start)
    deliver = MagicMock(side_effect=lambda name, msg: {"ok": name != OLD, "url": ""})
    monkeypatch.setattr(adapter, "message_session", deliver)

    def history():
        path = adapter._history_file(CHAT)
        if not path.exists():
            return []
        return [(e["role"], e["content"]) for e in map(json.loads, path.read_text().splitlines())]

    def setup(dog: bool, state: dict):
        monkeypatch.setattr(adapter, "dog_available", lambda: dog)
        if state:
            adapter._save_chat_state(CHAT, dict(state))

    return adapter, start, deliver, events, history, setup


async def _handle(adapter, kind: str, update):
    context = MagicMock()
    context.bot.username = "testbot"
    await getattr(adapter, "handle_" + kind)(update, context)


def _replies(update) -> list[str]:
    return [c.args[0] for c in update.message.reply_text.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,dog,state,replying,fragment",
                         [s[1:] for s in SPAWN_SITES], ids=[s[0] for s in SPAWN_SITES])
async def test_spawning_message_is_in_the_opening_prompt(lodge, kind, dog, state, replying, fragment):
    adapter, start, deliver, events, history, setup = lodge
    setup(dog, state)
    update = _update(kind, replying)

    await _handle(adapter, kind, update)

    start.assert_called_once()
    assert start.call_args.kwargs["wolt"] == WOLT
    entries = history()
    # The message as the session reads it is what the chat history records.
    assert entries[-2:] == [
        ("user", entries[-2][1]),
        ("assistant", f"[delivered to session {NEW}]"),
    ]
    text = entries[-2][1]
    assert fragment in text
    # No reply instruction here: the spawn's Telegram context carries the one.
    assert start.call_args.kwargs["prompt"] == (
        f"{adapter._start_chat_prompt(WOLT)}\n\n"
        f"[telegram message from human, chat_id={CHAT}]: {text}"
    )
    # Nothing is pasted into the new session; only the dead one was tried.
    expired = bool(state.get("active_session")) or replying
    assert [c.args[0] for c in deliver.call_args_list] == ([OLD] if expired else [])
    assert ("telegram_v2_spawn_with_message",
            {"session": NEW, "wolt": WOLT, "text": text[:200]}) in events
    saved = adapter._load_chat_state(CHAT)
    assert (saved["active_wolt"], saved["active_session"]) == (WOLT, NEW)
    replies = _replies(update)
    assert not [r for r in replies if r.startswith("🪵 sent to")]
    expected = (f"🪵 session expired — new one for {WOLT}\n{NEW}" if replying
                else f"🪵 new session for {WOLT}\n{NEW}")
    assert replies[-1] == expected


@pytest.mark.asyncio
async def test_assembled_opening_prompt_has_one_start_chat_and_one_reply_instruction(lodge):
    """Start-chat leads, so the spawn's own prompt assembly doesn't add one, and
    the reply instruction comes once, from the spawn's Telegram context."""
    import sessions

    adapter, start, deliver, events, history, setup = lodge
    setup(False, {"active_wolt": WOLT})
    await _handle(adapter, "message", _update("message", False))

    prompt = start.call_args.kwargs["prompt"]
    assembled = sessions._assemble_spawn_prompt(
        {"wolt": WOLT, "adapter": "telegram", "chat_id": CHAT},
        prompt, sessions.wolt_harness(WOLT),
    )
    assert assembled.count("start-chat") == 1
    assert _reply_instructions(assembled) == 1
    assert "[telegram message from human" in assembled


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,dog,state,replying,fragment",
                         [s[1:] for s in SPAWN_SITES], ids=[s[0] for s in SPAWN_SITES])
async def test_failed_spawn_is_reported_and_records_no_delivery(lodge, kind, dog, state, replying, fragment):
    adapter, start, deliver, events, history, setup = lodge
    start.side_effect = RuntimeError("boom")
    setup(dog, state)
    update = _update(kind, replying)

    await _handle(adapter, kind, update)

    replies = _replies(update)
    assert replies[-1].startswith("couldn't start session")
    assert replies[-1].endswith(": boom")
    assert NEW not in [c.args[0] for c in deliver.call_args_list]
    assert not [c for _, c in history() if c.startswith("[delivered to session")]
    assert not [e for e, _ in events if e == "telegram_v2_spawn_with_message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,dog", [s[1:] for s in RUNNING_SITES], ids=[s[0] for s in RUNNING_SITES])
async def test_message_to_a_running_session_is_still_pasted(lodge, kind, dog):
    adapter, start, deliver, events, history, setup = lodge
    deliver.side_effect = lambda name, msg: {"ok": True, "url": ""}
    setup(dog, {"active_wolt": WOLT, "active_session": OLD})
    update = _update(kind, False)

    await _handle(adapter, kind, update)

    start.assert_not_called()
    assert [c.args[0] for c in deliver.call_args_list] == [OLD]
    pasted = deliver.call_args.args[1]
    assert pasted.startswith(f"[telegram message from human, chat_id={CHAT}]: ")
    assert _reply_instructions(pasted) == 1
    assert f"🪵 sent to {OLD}\n{OLD}" in _replies(update)
    assert history()[-1] == ("assistant", f"[delivered to session {OLD}]")


def _iwcl(tmp_path):
    path = tmp_path / "wolts" / ".space" / "logs" / "iwcl.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.asyncio
async def test_a_spawning_message_is_a_human_spawn_line_in_the_iwcl_log(lodge, tmp_path, monkeypatch):
    monkeypatch.delenv("WOLTSPACE_IWCL_LOG", raising=False)
    adapter, start, deliver, events, history, setup = lodge
    setup(False, {"active_wolt": WOLT})
    update = _update("message", False)
    update.effective_user.username = "jerpint"

    await _handle(adapter, "message", update)

    [line] = _iwcl(tmp_path)
    assert (line["event"], line["status"], line["via"]) == ("spawn", "started", "telegram")
    assert (line["from_human"], line["from_wolt"]) == ("jerpint", "")
    assert (line["to_wolt"], line["to_session"], line["text"]) == (WOLT, NEW, "hello")


@pytest.mark.asyncio
async def test_a_pasted_message_is_a_human_message_line_in_the_iwcl_log(lodge, tmp_path, monkeypatch):
    monkeypatch.delenv("WOLTSPACE_IWCL_LOG", raising=False)
    adapter, start, deliver, events, history, setup = lodge
    deliver.side_effect = lambda name, msg: {"ok": True, "url": ""}
    setup(False, {"active_wolt": WOLT, "active_session": OLD})
    update = _update("message", False)
    update.effective_user.username = None
    update.effective_user.first_name = "Jeremy"

    await _handle(adapter, "message", update)

    [line] = _iwcl(tmp_path)
    assert (line["event"], line["status"], line["from_human"]) == ("message", "delivered", "Jeremy")
    assert (line["to_session"], line["text"]) == (OLD, "hello")
