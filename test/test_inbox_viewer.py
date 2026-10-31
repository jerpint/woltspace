"""The owner's read-only inbox viewer: Store.messages, GET /inbox/messages and the
two mounts (the site shell's right pill, the lodge Inbox page). Real SQL on a dummy root."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from inbox_support import limits, reader, rows, session, wolt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import inbox  # noqa: E402
import server.app as server_app  # noqa: E402
from server import inbox_routes  # noqa: E402

FIELDS = {"seq", "sender", "sender_wolt", "human", "at", "state", "text"}
TRICKY = "<img src=x onerror=alert(1)> & </script>\nline two \U0001F9AB"


@pytest.fixture
def store(tmp_path, monkeypatch):
    """nunu gets: a human message (claimed: being read), pal's tricky one, one from a
    wolt since archived, and one acked (gone). pal gets one held message."""
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    for name in ("nunu", "zed"):
        wolt(tmp_path, name)
    store = inbox.Store(tmp_path, limits())
    pal, nunu, zed = reader(store, "s1"), reader(store, "s2", "nunu"), reader(store, "s3", "zed")
    store.admit("connector:t", "h1", "nunu", "acked away")
    got = store.fetch(nunu)
    store.ack(nunu, got.seq, got.claim)
    store.admit("connector:t", "h2", "nunu", "from a person", human=True)
    store.send(pal, "nunu", "a1", TRICKY)
    store.send(zed, "nunu", "z1", "from zed")
    store.set_state("zed", "archived")
    store.fetch(nunu)  # claims the human one: being read
    store.send(nunu, "pal", "a2", "for pal")
    old = reader(store, "s4")
    store.fetch(old)
    session(tmp_path, "s4", "pal", status="stopped")  # its reader is gone: held
    return store


def _client(store, on=True):
    if on:
        (store.root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    return TestClient(FastAPI(routes=inbox_routes.router.routes), base_url="http://localhost:7777")


def test_every_retained_message_newest_first_with_only_the_viewer_fields(store):
    page = store.messages()
    assert page["next"] is None and all(set(m) == FIELDS for m in page["messages"])
    got = [(m["sender"], m["sender_wolt"], m["human"], m["state"], m["text"]) for m in page["messages"]]
    assert got == [("nunu", "nunu", False, "held", "for pal"),
                   ("zed", None, False, "queued", "from zed"),  # a wolt no longer open: no link
                   ("pal", "pal", False, "queued", TRICKY),
                   ("connector:t", None, True, "claimed", "from a person")]
    assert [m["text"] for m in store.messages("nunu")["messages"]] == ["from zed", TRICKY, "from a person"]
    assert store.messages("nobody") == {"messages": [], "next": None}


@pytest.mark.parametrize("size", [1, 2, 3])
def test_pages_reach_every_message_once(store, size):
    seen, before = [], None
    while True:
        page = store.messages(before=before, limit=size)
        assert len(page["messages"]) <= size
        seen += [m["seq"] for m in page["messages"]]
        if page["next"] is None:
            break
        before = page["next"]
    assert seen == [m["seq"] for m in store.messages()["messages"]]
    assert store.messages(limit=10**6)["next"] is None  # the cap still answers


def test_the_route_is_owner_only_never_cached_and_leaks_no_key(store):
    client = _client(store, on=False)
    assert (client.get("/inbox/messages").status_code, client.get("/inbox/messages").json()) == \
        (404, {"error": "inbox_disabled"})
    client = _client(store)
    agent = client.get("/inbox/messages", headers={inbox_routes.TOKEN_HEADER: "anything"})
    assert (agent.status_code, agent.json()) == (403, {"error": "owner_only"})
    ok = client.get("/inbox/messages?wolt=nunu&limit=2")
    assert ok.status_code == 200 and ok.headers["cache-control"] == "no-store"
    assert set(ok.json()) == {"ok", "now", "messages", "next"}
    assert ok.json()["messages"] == store.messages("nunu", limit=2)["messages"]
    rest = client.get(f"/inbox/messages?wolt=nunu&before={ok.json()['next']}").json()
    assert [m["text"] for m in rest["messages"]] == ["from a person"]
    keys = [k for (k,) in rows(store.root, "SELECT key FROM inbox_messages")]
    body = client.get("/inbox/messages").text
    assert not any(f'"{k}"' in body for k in keys) and '"claim"' not in body and '"reader"' not in body


@pytest.mark.parametrize("query", ["limit=abc", "limit=%C2%B2", "before=-1", "limit=0", "x=1",
                                   "wolt=a&wolt=b", "wolt=" + "x" * (inbox.LIMITS.name_chars + 1)])
def test_bad_queries_get_a_fixed_code_and_no_echo(store, query):
    r = _client(store).get("/inbox/messages?" + query)
    assert (r.status_code, r.json()) == (400, {"error": "invalid"})


def test_viewing_changes_nothing(store):
    client = _client(store)
    tables = ("inbox_mailboxes", "inbox_messages", "inbox_readers", "inbox_events", "inbox_usage")
    before = {t: rows(store.root, f"SELECT * FROM {t}") for t in tables}
    for q in ("", "?wolt=nunu", "?wolt=pal&limit=1", "?before=2"):
        assert client.get("/inbox/messages" + q).status_code == 200
    store.messages()
    assert {t: rows(store.root, f"SELECT * FROM {t}") for t in tables} == before


def test_the_inbox_page_mounts_the_same_viewer(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    page = TestClient(server_app.app, base_url="http://localhost:7777").get("/inbox")
    assert page.status_code == 200 and 'id="inbox-messages"' in page.text
    assert page.text.index("/static/wolt-shell/inbox-viewer.js") < page.text.index("/static/inbox.js")


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_viewer_renders_text_only_and_drops_stale_answers():
    """test/inbox-viewer.test.mjs: the viewer's list on the repo's fake DOM."""
    out = subprocess.run(["node", "--test", str(ROOT / "test" / "inbox-viewer.test.mjs")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
