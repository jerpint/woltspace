"""Round 2 held-message behavior: expiry, fresh-session choice and consumed picker."""


import inbox
import lodge_db

from test.inbox_support import session
from test.test_held_message_negative import CHAT, event, mail, pending, pick

pytest_plugins = ["test.test_held_message_negative"]


def test_next_routed_message_reports_expired_hold_once_even_with_active_wolt(side):
    event(side)
    with lodge_db.write(side.root) as conn:
        conn.execute("UPDATE pending_messages SET expires_at=0")
    side._save_chat_state(CHAT, {"active_wolt": "alpha"})
    first = event(side, "next", body={"text": "next text"})
    again = event(side, "later", body={"text": "later text"})
    notices = [r.text.lower() for r in first.replies + again.replies
               if "not delivered" in r.text.lower() and
               ("expired" in r.text.lower() or "10 minutes" in r.text.lower())]
    assert len(notices) == 1
    assert not pending(side)


def test_new_starts_a_session_before_admitting_the_held_message_and_prefers_it(side, monkeypatch):
    name = "alpha-new-grove-000003"
    started = []

    def spawn(wolt, chat):
        assert pending(side) == [("first", "first text")]
        assert mail(side) == []
        started.append((wolt, chat))
        session(side.root, name, wolt="alpha")
        return {"name": name}

    monkeypatch.setattr(side, "_spawn_session", spawn)
    event(side)
    event(side, "new", kind="command", body={"name": "new", "args": ["alpha"]})
    assert started == [("alpha", CHAT)]
    assert [key for key, _ in mail(side)] == ["telegram:first"]
    facts = inbox.Store(side.root, inbox.LIMITS).wake_facts("alpha")
    assert facts is not None
    wake = facts["wake"]
    assert wake["prefer"]["session"] == name
    assert side._load_chat_state(CHAT)["active_session"] == name


def test_new_failure_keeps_the_held_text_and_admits_nothing(side, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("engine unavailable")

    monkeypatch.setattr(side, "_spawn_session", fail)
    event(side)
    out = event(side, "new", kind="command", body={"name": "new", "args": ["alpha"]})
    assert mail(side) == [] and side.wakes == []
    assert pending(side) == [("first", "first text")]
    assert "couldn't" in out.text.lower() or "failed" in out.text.lower()


def test_a_consumed_hold_picker_still_switches_but_never_redelivers(side):
    event(side)
    pick(side)
    side._save_chat_state(CHAT, {"active_wolt": "beta"})
    out = pick(side)
    assert side._load_chat_state(CHAT)["active_wolt"] == "alpha"
    assert [key for key, _ in mail(side)] == ["telegram:first"]
    notice = (out.text + " ".join(ack.text or "" for ack in out.acks)).lower()
    assert "already" in notice or "gone" in notice
