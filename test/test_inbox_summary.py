"""The lodge's Inbox page: Store.summary, GET /inbox/summary and the page itself.
Counts only, read-only. Real SQL on a dummy root."""

import json
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

BODY = "DUMMY_BODY_MARKER"
FIELDS = {"wolt", "state", "queued_human", "queued_agent", "claimed", "held", "oldest_queued_at"}


@pytest.fixture
def store(tmp_path, monkeypatch):
    """pal: one agent message claimed, one held; nunu: two human, one agent queued, one acked;
    zed: archived with one queued; gone: closed after its mail was acked."""
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    for name in ("nunu", "zed", "gone"):
        wolt(tmp_path, name)
    store = inbox.Store(tmp_path, limits())
    pal, nunu = reader(store, "s1"), reader(store, "s2", "nunu")
    store.admit("connector:t", "h1", "nunu", BODY, human=True)
    store.admit("connector:t", "h2", "nunu", BODY, human=True)
    store.send(pal, "nunu", "a1", BODY)
    store.send(pal, "nunu", "a2", BODY)
    got = store.fetch(nunu)  # the first human message, acknowledged: counted nowhere
    store.ack(nunu, got.seq, got.claim)
    store.send(nunu, "pal", "a3", BODY)
    store.send(nunu, "pal", "a4", BODY)
    store.fetch(pal)
    old = reader(store, "s3")  # a second session of pal claims, then stops: held
    store.fetch(old)
    session(tmp_path, "s3", "pal", status="stopped")
    store.admit("connector:t", "z1", "zed", BODY)
    store.set_state("zed", "archived")
    gone = reader(store, "s4", "gone")  # a mailbox that held mail, then closed for good
    store.admit("connector:t", "g1", "gone", BODY)
    got = store.fetch(gone)
    store.ack(gone, got.seq, got.claim)
    store.set_state("gone", "closed")
    return store


def test_the_summary_counts_each_open_or_archived_mailbox_human_first(store):
    summary = store.summary()
    assert all(set(w) == FIELDS for w in summary)
    counts = [(w["wolt"], w["state"], w["queued_human"], w["queued_agent"], w["claimed"], w["held"]) for w in summary]
    assert counts == [("nunu", "open", 1, 2, 0, 0), ("zed", "archived", 0, 1, 0, 0), ("pal", "open", 0, 0, 1, 1)]
    [(oldest,)] = rows(store.root, "SELECT min(m.created_at) FROM inbox_messages m JOIN inbox_mailboxes b"
                                   " ON b.id = m.mailbox WHERE b.wolt = 'nunu' AND m.state = 'queued'")
    assert summary[0]["oldest_queued_at"] == oldest and summary[2]["oldest_queued_at"] is None


def test_the_route_serves_counts_only_and_only_while_the_inbox_is_on(store):
    client = TestClient(FastAPI(routes=inbox_routes.router.routes), base_url="http://localhost:7777")
    off = client.get("/inbox/summary")
    assert (off.status_code, off.json()) == (404, {"error": "inbox_disabled"})
    (store.root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    on = client.get("/inbox/summary")
    assert on.status_code == 200 and set(on.json()) == {"ok", "now", "wolts"}
    assert on.json()["wolts"] == store.summary()
    assert BODY not in on.text and "connector:t" not in on.text and "h2" not in on.text


def test_viewing_changes_nothing(store):
    (store.root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    tables = ("inbox_mailboxes", "inbox_messages", "inbox_readers", "inbox_events", "inbox_usage")
    before = {t: rows(store.root, f"SELECT * FROM {t}") for t in tables}
    client = TestClient(FastAPI(routes=inbox_routes.router.routes), base_url="http://localhost:7777")
    for _ in range(3):
        assert client.get("/inbox/summary").status_code == 200
    assert {t: rows(store.root, f"SELECT * FROM {t}") for t in tables} == before


def test_the_page_is_in_the_lodge_nav(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    page = client.get("/inbox")
    assert page.status_code == 200 and "/static/inbox.js" in page.text
    assert 'class="sidebar-nav-item active" id="nav-inbox"' in page.text
    assert 'id="nav-inbox" href="/inbox"' in client.get("/connectors").text  # shown on every page
