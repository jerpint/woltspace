"""Integrated acceptance negatives; reuse builder ingress/runtime/server fixtures."""
import json
import sys

import inbox
from inbox_support import limits, rows
from test_inbox_api import _cli
from test_inbox_flow import client, flow, say, server, store  # noqa: F401
import sessions


def token_for(flow):
    name, = flow.seen.spawned
    return inbox.Store(flow.root, limits(name_chars=64)).grant(name, "pal")


def test_human_priority_and_target_survive_real_cli_pickup(flow):
    flow.store.admit("fixture", "agent-first", "pal", "agent older")
    flow.store.admit("fixture", "other-target", "nunu", "other mailbox", human=True)
    say(flow, "owner", "owner later")
    token = token_for(flow)
    got = json.loads(_cli(flow.api, "fetch", token=token).stdout)
    assert "]: owner later\n" in got["body"] and got["priority"] == inbox.HUMAN
    assert rows(flow.root, "SELECT state FROM inbox_messages WHERE key='telegram:owner'") == [("claimed",)]
    assert rows(flow.root, "SELECT state FROM inbox_messages WHERE key IN ('agent-first','other-target')") == [
        ("queued",), ("queued",)]
    assert _cli(flow.api, "ack", str(got["seq"]), str(got["claim"]), token=token).returncode == 0
    next_mail = json.loads(_cli(flow.api, "fetch", token=token).stdout)
    assert next_mail["body"] == "agent older" and next_mail["priority"] == inbox.AGENT
    assert rows(flow.root, "SELECT state FROM inbox_messages WHERE key='other-target'") == [("queued",)]


def test_unknown_native_submission_keeps_custody_until_cli_receipt(flow, monkeypatch):
    say(flow, "first", "first mail")
    seen = []

    def unknown(record, hint):
        seen.append((record["attempt"], record["native"]["attempt"], hint))
        return "unknown", "reply lost"

    monkeypatch.setattr(sys.modules["native_adapters"], "deliver", unknown)
    say(flow, "second", "second mail")
    assert len(flow.seen.spawned) == 1 and len(seen) == 1
    assert seen[0][0] == seen[0][1] and "second mail" not in seen[0][2]
    assert rows(flow.root, "SELECT state FROM inbox_messages ORDER BY seq") == [("queued",), ("queued",)]
    token = token_for(flow)
    first = json.loads(_cli(flow.api, "fetch", token=token).stdout)
    repeated = json.loads(_cli(flow.api, "fetch", token=token).stdout)
    assert (first["seq"], first["claim"], first["body"]) == (
        repeated["seq"], repeated["claim"], repeated["body"])
    assert rows(flow.root, "SELECT state FROM inbox_messages ORDER BY seq") == [("claimed",), ("queued",)]
    assert _cli(flow.api, "ack", str(first["seq"]), str(first["claim"]), token=token).returncode == 0


def test_true_resend_and_changed_body_conflict_never_spawn_again(flow):
    say(flow, "same", "original")
    say(flow, "same", "original")
    hints_before = len(flow.seen.hints)
    answer = say(flow, "same", "changed")
    assert answer == ["🪵 not queued for pal: conflict"]
    assert len(flow.seen.spawned) == 1 and len(flow.seen.hints) == hints_before
    messages = rows(flow.root, "SELECT key, state, body FROM inbox_messages")
    assert len(messages) == 1 and messages[0][:2] == ("telegram:same", "queued")
    assert b"]: original\n" in messages[0][2]
