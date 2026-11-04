"""The inbox flow end to end at its real seams: an owner's Telegram message through
ChatLodge into the Store, the post-commit wake under the agent's lifecycle lock, a
fresh session started with its native locator, a later message hinted to that
session's current attempt, then the agent's own fetch and ack through the real CLI.
Only tmux and the engine's native transport are fake."""

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

from inbox_support import limits, rows
from test_chat_lodge_inbox import ROOT  # noqa: F401 (puts container/ on the path for chat_lodge)
from test_inbox_api import _cli, client, server, store  # noqa: F401 (fixtures)

import inbox

import inbox_wake
import sessions
from session_runtime import RuntimeHandle

OWNER, CHAT = "111", "4242"


@pytest.fixture
def flow(tmp_path, monkeypatch, store, server):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")  # auto policy, whatever ran before
    import chat_lodge
    import connector_registry
    (tmp_path / ".space").mkdir(exist_ok=True)
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True, "native_hints": True}}))
    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(inbox, "LIMITS", store.limits)
    seen = types.SimpleNamespace(spawned=[], hints=[])

    def spawn(name, cwd, command):
        seen.spawned.append(name)
        return RuntimeHandle(woltspace_session_id=name, tmux_session_name=name, pane_id="%1")

    def live(wolt, registry=None):
        running = [r for r in sessions.SessionRegistry(tmp_path).records(wolt=wolt)
                   if r.get("status") == "running" and r.get("runtime") and not r.get("launching")]
        return running[-1]["name"] if running else None

    def deliver(record, hint):
        seen.hints.append((record["name"], record["attempt"], record["native"]["attempt"], hint))
        return "submitted", "ok"
    for module, name, value in [
        (sessions, "_tmux_spawn", spawn), (sessions, "harness_install_problem", lambda *a, **k: None),
        (sessions, "ensure_site", lambda wolt: None), (sessions, "get_tunnel_url", lambda: ""),
        (sessions, "resolve_active_session", live), (sessions, "_tmux_alive", lambda data: False),
        (inbox_wake, "_schedule", inbox_wake._wake),  # the wake inline, for a deterministic test
        (chat_lodge, "message_session", lambda *a, **k: pytest.fail("pasted into a session")),
        (chat_lodge, "start_claude_session", lambda **k: pytest.fail("started outside the wake")),
        (chat_lodge, "dog_available", lambda: False), (chat_lodge, "_bot_log", lambda *a: None),
    ]:
        monkeypatch.setattr(module, name, value)
    monkeypatch.setitem(sys.modules, "native_adapters", types.SimpleNamespace(
        prepare=lambda root, name, harness, attempt: {
            "kind": harness, "addr": str(root / ".space" / name / attempt), "attempt": attempt},
        retire=lambda locator: None, deliver=deliver))
    side = chat_lodge.ChatLodge(connector_registry.spec("telegram"), [OWNER])
    side._save_chat_state(CHAT, {"active_wolt": "pal", "active_session": ""})
    return types.SimpleNamespace(side=side, seen=seen, root=tmp_path, store=store, api=server)


def say(flow, event_id, text):
    class Out:
        said = []

        async def say(self, reply, photo=None):
            self.said.append(reply.text)
    out = Out()
    asyncio.run(flow.side.handle({"id": event_id, "sender": OWNER, "kind": "message",
                                  "route": {"chat_id": CHAT}, "body": {"text": text}}, out))
    return out.said


def test_telegram_to_a_woken_session_to_its_own_fetch_and_ack(flow, monkeypatch):
    import inbox_notice
    lines = []  # what the lodge's /notify would get: the owner's one line per message
    monkeypatch.setattr(inbox_notice, "_call", lambda method, path, body=None: (
        lines.append(body) if method == "POST" else None))
    assert say(flow, "e1", "first") == []  # no line at admission: the wake answers
    (name,) = flow.seen.spawned  # no session: exactly one fresh start, by the wake
    to_chat = {"adapter": "telegram", "chat_id": CHAT, "session": ""}
    assert lines == [{**to_chat, "message": f"🪵 {name} notified\n{name}"}]
    record = sessions.SessionRegistry(flow.root).get(name, check_alive=False)
    assert record["prompt"] == inbox_wake.START.format(sender=" from your human")[:500] and record["native"]["attempt"] == record["attempt"]
    assert "launching" not in record

    say(flow, "e2", "second")
    assert flow.seen.spawned == [name]  # the live session is hinted, never a second worker
    assert lines[1:] == [{**to_chat, "message": f"🪵 {name} notified\n{name}"}]  # its own one line
    # one pickup after the fresh session's publication, then one for the new mail
    assert len(flow.seen.hints) == 2
    (hinted, attempt, native_attempt, hint) = flow.seen.hints[-1]
    assert (hinted, attempt) == (name, native_attempt) and "woltspace inbox fetch" in hint
    assert "first" not in hint and "second" not in hint
    assert [s for s, in rows(flow.root, "SELECT state FROM inbox_messages")] == ["queued", "queued"]

    token = inbox.Store(flow.root, limits(name_chars=64)).grant(name, "pal")  # as run-session.sh does
    fetched = _cli(flow.api, "fetch", token=token)
    got = json.loads(fetched.stdout)
    assert fetched.returncode == 0 and "]: first\n" in got["body"]
    assert _cli(flow.api, "ack", str(got["seq"]), str(got["claim"]), token=token).returncode == 0
    assert [s for s, in rows(flow.root, "SELECT state FROM inbox_messages ORDER BY seq")] == [
        "acknowledged", "queued"]


def test_with_the_real_adapters_only_the_current_attempt_gets_the_hint(flow, monkeypatch):
    monkeypatch.delitem(sys.modules, "native_adapters")
    monkeypatch.setattr(inbox_wake, "log", types.SimpleNamespace(
        info=lambda *a: flow.seen.hints.append(a[-2:]), exception=print, warning=print))
    import native_adapters
    say(flow, "e1", "first")
    (name,) = flow.seen.spawned
    old = sessions.SessionRegistry(flow.root).get(name, check_alive=False)["native"]
    native_adapters.write_ready(old["addr"], {"attempt": old["attempt"], "kind": old["kind"],
                                               "armed_at": __import__("time").time()})
    say(flow, "e2", "second")  # the listener is armed but nobody claims: placed, still pending
    assert flow.seen.hints[-1] == ("pending", "placed")
    hint = (Path(old["addr"]) / native_adapters.HINT).read_text()
    assert "woltspace inbox fetch" in hint and "second" not in hint
    sessions.stop_session(name)  # retired before teardown: a late ready write is refused
    assert not native_adapters.write_ready(old["addr"], {"attempt": old["attempt"], "kind": old["kind"]})
    assert native_adapters.deliver({**old, "name": name, "status": "running", "attempt": old["attempt"],
                                    "native": old}, "x") == ("unavailable", "retired")
    assert [s for s, in rows(flow.root, "SELECT state FROM inbox_messages")] == ["queued", "queued"]
