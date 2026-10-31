"""Owner messages from a chat connector into the lodge inbox (RFC #615).

ChatLodge.handle on a real Store in a dummy root, with sessions, transcription
and the runtime's wake faked at their edges. With the inbox on, an owner's
message is queued for its wolt at human priority and then the wake is called;
with it off, delivery is today's.
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

from inbox_support import rows, wolt

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib"):
    sys.path.insert(0, str(ROOT / part))

import inbox  # noqa: E402

OWNER, CHAT, LIVE = "111", "4242", "pal-live-grove-000001"


class Out:
    def __init__(self):
        self.said = []

    async def say(self, reply, photo=None):
        self.said.append(reply.text)


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    # Imported here: bot.core makes its log folder under the data root on import.
    import chat_lodge
    import connector_registry
    wolt(tmp_path, "pal")
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    side = chat_lodge.ChatLodge(connector_registry.spec("telegram"), [OWNER])
    calls = {"pasted": [], "spawned": [], "woken": []}

    def notify_admitted(root, status):
        # What a reader would see at wake time: the row must already be committed.
        calls["woken"].append((status, rows(root, "SELECT state FROM inbox_messages WHERE key=?", status.key)))

    for name, value in [
        ("message_session", lambda name, text: calls["pasted"].append((name, text)) or {"ok": True, "url": ""}),
        ("start_claude_session", lambda **kw: calls["spawned"].append(kw) or {"name": LIVE}),
        ("transcribe_audio", lambda path: f"heard {Path(path).name}"),
        ("notify_admitted", notify_admitted), ("dog_available", lambda: False),
        ("get_tunnel_url", lambda: ""), ("_bot_log", lambda *a: None),
    ]:
        monkeypatch.setattr(chat_lodge, name, value)
    side._save_chat_state(CHAT, {"active_wolt": "pal", "active_session": LIVE})
    side.root, side.calls = tmp_path, calls
    return side


def handle(side, event_id="e1", sender=OWNER, kind="message", body=None):
    out = Out()
    event = {"id": event_id, "sender": sender, "kind": kind, "route": {"chat_id": CHAT},
             "body": body or {"text": "hello pal"}}
    asyncio.run(side.handle(event, out))
    return out.said


def queued(side):
    return rows(side.root, "SELECT sender, key, priority, body, state FROM inbox_messages ORDER BY seq")


def test_an_owner_message_is_queued_at_human_priority_instead_of_pasted(lodge):
    said = handle(lodge)
    [(sender, key, priority, body, state)] = queued(lodge)
    assert (sender, key, priority, state) == ("provider:connector:telegram", "telegram:e1", inbox.HUMAN, "queued")
    assert body.decode().startswith(f"[telegram message from human, chat_id={CHAT}]: hello pal\n")
    assert "WOLTSPACE_NOTIFY_" in body.decode()  # the session replies on today's path
    assert said == []  # the owner hears once, when the wake has decided
    assert lodge.calls["pasted"] == lodge.calls["spawned"] == []


def test_a_reply_to_a_notification_is_queued_with_its_quote(lodge):
    notification = ("🦫 pal: shall I ship it?\n\n---\n"
                    "↩️ reply to this message to talk to this session directly\n"
                    "https://lodge.invalid/tui?session=pal-other-marsh-abc123")
    handle(lodge, body={"text": "yes ship it", "reply_to": {"from_bot": True, "text": notification}})
    [(_, _, priority, body, _)] = queued(lodge)
    assert priority == inbox.HUMAN
    assert "]: [replying to: 🦫 pal: shall I ship it?\n]\nyes ship it\n" in body.decode()
    assert "↩️" not in body.decode()
    assert lodge.calls["pasted"] == lodge.calls["spawned"] == []


def test_the_next_plain_message_goes_to_the_wolt_last_written_to(lodge):
    wolt(lodge.root, "nunu")
    lodge._save_chat_state(CHAT, {"active_wolt": "nunu", "active_session": "nunu-old-grove-000002"})
    notification = ("🦫 pal: done?\n\n---\n↩️ reply to this message to talk to this session directly\n"
                    "https://lodge.invalid/tui?session=pal-other-marsh-abc123")
    handle(lodge, body={"text": "yes", "reply_to": {"from_bot": True, "text": notification}})
    assert lodge._load_chat_state(CHAT) == {"active_wolt": "pal"}
    handle(lodge, event_id="e2", body={"text": "and one more"})
    assert [r[1] for r in rows(lodge.root, "SELECT seq, b.wolt FROM inbox_messages m JOIN inbox_mailboxes b"
                                           " ON b.id = m.mailbox ORDER BY seq")] == ["pal", "pal"]


def test_a_non_owner_queues_nothing_and_wakes_nobody(lodge):
    assert handle(lodge, sender="999") == []
    assert not (lodge.root / ".space" / "lodge.db").exists() or queued(lodge) == []
    assert lodge.calls["woken"] == lodge.calls["pasted"] == []


def test_a_resent_event_is_the_message_already_queued(lodge):
    assert handle(lodge) == handle(lodge) == []
    [(seq, key)] = rows(lodge.root, "SELECT seq, key FROM inbox_messages")
    first, again = (s for s, _ in lodge.calls["woken"])
    assert first == again and (first.seq, first.key) == (seq, "telegram:e1")
    handle(lodge, event_id="e2")
    assert [r[1] for r in queued(lodge)] == ["telegram:e1", "telegram:e2"]


def test_a_resent_event_for_another_wolt_is_a_conflict_not_a_wake(lodge):
    handle(lodge)
    wolt(lodge.root, "nunu")
    lodge._save_chat_state(CHAT, {"active_wolt": "nunu", "active_session": "nunu-live-grove-000002"})
    assert handle(lodge) == ["🪵 not queued for nunu: conflict"]
    assert [r[1] for r in queued(lodge)] == ["telegram:e1"]
    assert len(lodge.calls["woken"]) == 1
    assert lodge.calls["pasted"] == lodge.calls["spawned"] == []


def test_a_voice_transcript_is_queued(lodge):
    uploads = lodge.uploads_dir
    uploads.mkdir(parents=True)
    (uploads / "note.ogg").write_bytes(b"dummy")
    handle(lodge, kind="attachment", body={"media": "voice", "path": str(uploads / "note.ogg")})
    [(_, _, priority, body, _)] = queued(lodge)
    assert priority == inbox.HUMAN
    assert "]: [voice message] heard note.ogg\n" in body.decode()


def test_with_the_inbox_off_the_message_is_pasted_as_before(lodge):
    (lodge.root / "woltspace.json").unlink()
    assert handle(lodge) == [f"🪵 sent to {LIVE}\n{LIVE}"]
    [(name, text)] = lodge.calls["pasted"]
    assert name == LIVE and text.startswith(f"[telegram message from human, chat_id={CHAT}]: hello pal\n")
    assert not (lodge.root / ".space" / "lodge.db").exists() or queued(lodge) == []
    assert lodge.calls["woken"] == []


def test_the_wake_comes_after_the_commit_and_never_for_refused_mail(lodge):
    handle(lodge)
    [(status, seen)] = lodge.calls["woken"]
    assert (status.to, status.state, seen) == ("pal", "queued", [("queued",)])
    inbox.Store(lodge.root, inbox.LIMITS).set_state("pal", "archived")
    assert handle(lodge, event_id="e2") == ["🪵 not queued for pal: mailbox_archived"]
    assert len(lodge.calls["woken"]) == 1 and len(queued(lodge)) == 1
    assert lodge.calls["pasted"] == lodge.calls["spawned"] == []


def test_the_session_the_owner_meant_is_the_wake_s_first_choice_and_the_chat_keeps_it(lodge):
    handle(lodge)  # a plain message: the chat's own session
    notification = ("🦫 pal: done?\n\n---\n↩️ reply to this message to talk to this session directly\n"
                    "https://lodge.invalid/tui?session=pal-other-marsh-abc123")
    handle(lodge, event_id="e2", body={"text": "yes", "reply_to": {"from_bot": True, "text": notification}})
    chat = {"adapter": "telegram", "chat": CHAT}
    (first, _), (second, _) = lodge.calls["woken"]
    wake = inbox.Store(lodge.root, inbox.LIMITS).wake_facts("pal")["wake"]  # recorded with each message
    assert wake["prefer"]["session"] == "pal-other-marsh-abc123" and wake["prefer"]["seq"] == second.seq
    assert wake["chats"] == {str(first.seq): chat, str(second.seq): chat}
    assert lodge._load_chat_state(CHAT) == {"active_wolt": "pal", "active_session": LIVE}  # same wolt: kept

