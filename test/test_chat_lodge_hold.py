"""Hold-while-picking (held_messages): a message sent with no wolt chosen waits for the pick.

ChatLodge.handle on a real lodge DB in a dummy root, sessions and the wake faked at their
edges. The boundary cases (other senders and chats, retries, refused admission, restore)
are in test_held_message_negative.py.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from test.inbox_support import rows, wolt

ROOT = Path(__file__).resolve().parents[1]
for part in ("container", "container/lib"):
    sys.path.insert(0, str(ROOT / part))

import held_messages  # noqa: E402
import lodge_db  # noqa: E402
import lodge_messages  # noqa: E402

OWNER, CHAT = "111", "4242"


class Out:
    def __init__(self):
        self.replies = []

    async def say(self, reply, photo=None):
        self.replies.append(reply)

    async def ack(self, ack):
        pass

    async def edit(self, reply):
        pass

    @property
    def texts(self):
        return [r.text for r in self.replies]


@pytest.fixture
def side(tmp_path, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    import chat_lodge
    import connector_registry
    for name in ("alpha", "beta"):
        wolt(tmp_path, name)
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    calls: dict[str, Any] = {"spawned": [], "dog": [], "dog_on": False}
    for name, value in [
        ("start_claude_session", lambda **kw: calls["spawned"].append(kw) or {"name": f"{kw['wolt']}-a-b-000001"}),
        ("message_session", lambda *a: pytest.fail("nothing is pasted here")),
        ("dog_available", lambda: calls["dog_on"]), ("notify_admitted", lambda *a: None),
        ("get_tunnel_url", lambda: ""), ("_bot_log", lambda *a: None),
        # bot.core reads the wolts dir it was first imported with
        ("list_wolts", lambda: [{"name": "alpha", "type": "beaver"}, {"name": "beta", "type": "beaver"}]),
    ]:
        monkeypatch.setattr(chat_lodge, name, value)
    lodge: Any = chat_lodge.ChatLodge(connector_registry.spec("telegram"), [OWNER])
    monkeypatch.setattr(lodge, "_handle_dog", lambda route, out, text: calls["dog"].append(text) or _done())
    lodge.root, lodge.calls = tmp_path, calls
    return lodge


async def _done():
    return None


def handle(side, kind="message", body=None, key="m1"):
    out = Out()
    asyncio.run(side.handle({"id": key, "sender": OWNER, "kind": kind, "route": {"chat_id": CHAT},
                             "body": body or {"text": "hello"}}, out))
    return out


def click(side, wolt="alpha", key="m1"):
    return handle(side, "selection", {"action": f"hold:{wolt}:{held_messages.generation(key)}"}, f"c-{key}")


def queued_to(side):
    return rows(side.root, "SELECT b.wolt, m.key FROM inbox_messages m JOIN inbox_mailboxes b"
                           " ON b.id = m.mailbox ORDER BY seq")


def test_with_no_wolt_chosen_the_message_is_held_and_its_picker_carries_it(side):
    out = handle(side)
    picker, said = out.replies
    assert [c.action for row in picker.choices for c in row] == [
        f"hold:{name}:{held_messages.generation('m1')}" for name in ("alpha", "beta")]
    assert said.text == lodge_messages.HELD
    assert rows(side.root, "SELECT event_id, text, state FROM pending_messages") == [("m1", "hello", "pending")]
    assert queued_to(side) == [] and side.calls["spawned"] == []


def test_the_chosen_wolt_gets_it_whatever_the_chat_switched_to_meanwhile(side):
    handle(side)
    side._save_chat_state(CHAT, {"active_wolt": "beta"})
    side.calls["dog_on"] = True
    click(side)
    assert queued_to(side) == [("alpha", "telegram:m1")]
    assert side._load_chat_state(CHAT)["active_wolt"] == "alpha"
    assert side.calls["dog"] == []


@pytest.mark.parametrize("case", ["active wolt", "dog", "one wolt", "reply target"])
def test_routing_that_needs_no_choice_holds_nothing(side, monkeypatch, case):
    body = {"text": "hello"}
    if case == "active wolt":
        side._save_chat_state(CHAT, {"active_wolt": "beta"})
    elif case == "dog":
        side.calls["dog_on"] = True
    elif case == "one wolt":
        import chat_lodge
        monkeypatch.setattr(chat_lodge, "list_wolts", lambda: [{"name": "alpha", "type": "beaver"}])
    else:
        body["reply_to"] = {"from_bot": True, "text": "---\n↩️ reply to this message to talk to this session"
                            " directly\nhttps://lodge.invalid/tui?session=beta-a-b-000002"}
    handle(side, body=body)
    assert rows(side.root, "SELECT count(*) FROM pending_messages") == [(0,)]
    assert queued_to(side) or side.calls["dog"]


def test_with_the_inbox_off_one_pick_starts_one_session_and_a_failure_says_resend(side, monkeypatch):
    import chat_lodge
    (side.root / "woltspace.json").write_text("{}")
    handle(side)
    click(side)
    click(side)
    assert [(s["wolt"], s["prompt"].endswith("]: hello")) for s in side.calls["spawned"]] == [("alpha", True)]
    assert rows(side.root, "SELECT state, text FROM pending_messages") == [("claimed", None)]

    def fails(**kw):
        raise RuntimeError("no engine")
    monkeypatch.setattr(chat_lodge, "start_claude_session", fails)
    side._save_chat_state(CHAT, {})
    handle(side, key="m2")
    out = click(side, key="m2")
    assert out.texts[-1] == "🪵 your held message was not delivered: send it again."


def test_a_pick_after_ten_minutes_says_it_expired_and_delivers_nothing(side):
    handle(side)
    with lodge_db.write(side.root) as conn:
        conn.execute("UPDATE pending_messages SET expires_at = expires_at - ?", (held_messages.TTL_SECONDS,))
    out = click(side)
    assert "waited over 10 minutes" in out.texts[-1]
    assert queued_to(side) == []
    assert rows(side.root, "SELECT state, text FROM pending_messages") == [("expired", None)]


def test_command_release_keeps_the_newly_started_session(side):
    (side.root / "woltspace.json").write_text("{}")
    handle(side)
    handle(side, "command", {"name": "wolt", "args": ["alpha"]}, "command")
    [spawned] = side.calls["spawned"]
    assert spawned["wolt"] == "alpha"
    assert side._load_chat_state(CHAT)["active_session"] == "alpha-a-b-000001"


def test_a_plain_wolt_pick_or_new_with_a_name_releases_the_held_message_too(side):
    handle(side)
    handle(side, "selection", {"action": "wolt:beta"}, "pick")  # the /wolt picker's own button
    handle(side, key="m2")  # nothing waits any more: straight to beta
    side._save_chat_state(CHAT, {})
    handle(side, key="m3")
    handle(side, "command", {"name": "new", "args": ["alpha"]}, "new")
    assert queued_to(side) == [("beta", "telegram:m1"), ("beta", "telegram:m2"), ("alpha", "telegram:m3")]
