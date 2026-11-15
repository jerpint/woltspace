"""Additional round 2 B edges requested by the planner."""

from test_held_message_negative import CHAT, event, mail, pending

pytest_plugins = ["test_held_message_negative"]


def test_new_with_inbox_off_delivers_into_exactly_the_new_session(side, monkeypatch):
    import chat_lodge

    (side.root / "woltspace.json").write_text("{}")
    name, pasted, spawned = "alpha-new-grove-000003", [], []

    def spawn(wolt, chat):
        spawned.append((wolt, chat))
        return {"name": name}

    monkeypatch.setattr(side, "_spawn_session", spawn)
    monkeypatch.setattr(chat_lodge, "message_session",
                        lambda session, text: pasted.append((session, text)) or {"ok": True})
    event(side)
    event(side, "new", kind="command", body={"name": "new", "args": ["alpha"]})
    assert spawned == [("alpha", CHAT)]
    assert len(pasted) == 1 and pasted[0][0] == name and "first text" in pasted[0][1]
    assert side._load_chat_state(CHAT)["active_session"] == name
    assert not pending(side) and mail(side) == []


def test_failed_new_does_not_release_into_a_preexisting_session_of_same_wolt(side, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("engine unavailable")

    event(side)
    side._save_chat_state(CHAT, {"active_wolt": "alpha", "active_session": "alpha-old-grove-000004"})
    monkeypatch.setattr(side, "_spawn_session", fail)
    event(side, "new", kind="command", body={"name": "new", "args": ["alpha"]})
    assert pending(side) == [("first", "first text")]
    assert mail(side) == [] and side.wakes == []
    assert side._load_chat_state(CHAT)["active_session"] == "alpha-old-grove-000004"


def test_next_routed_reply_also_reports_an_expired_hold_once(side):
    import lodge_db

    event(side)
    with lodge_db.write(side.root) as conn:
        conn.execute("UPDATE pending_messages SET expires_at=0")
    reply = {"text": "reply", "reply_to": {"from_bot": True, "text":
             "---\n↩️ reply to this message to talk to this session directly\n"
             "https://lodge.invalid/tui?session=alpha-old-grove-000004"}}
    first = event(side, "reply1", body=reply)
    again = event(side, "reply2", body=reply)
    assert len(mail(side)) == 2  # expiry reporting must preserve explicit reply routing
    notices = [r.text.lower() for r in first.replies + again.replies
               if "not delivered" in r.text.lower() and
               ("expired" in r.text.lower() or "10 minutes" in r.text.lower())]
    assert len(notices) == 1
    assert not pending(side)
