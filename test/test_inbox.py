"""inbox: the lodge inbox's store, durable admission to acknowledged receipt."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from inbox_support import acked, failing, limits, lose_confirmation, reader, rows, session, state, usage, wolt

import inbox
import lodge_db


@pytest.fixture
def store(tmp_path):
    for name in ("pal", "nunu"):
        wolt(tmp_path, name)
    return inbox.Store(tmp_path, limits())


def test_a_receipt_is_the_committed_row_and_survives_reopening(tmp_path, store):
    receipt = store.admit("ops", "k1", "pal", "hello")
    assert (receipt.sender, receipt.key, receipt.to, receipt.state) == ("ops", "k1", "pal", "queued")
    assert rows(tmp_path, "SELECT body, size, digest FROM inbox_messages") == [
        (b"hello", 5, hashlib.sha256(b"hello").digest())]
    assert rows(tmp_path, "SELECT seq, state FROM inbox_events") == [(receipt.seq, "queued")]
    assert usage(tmp_path) == {"": (1, 5, 1), "1": (1, 5, 1)}
    token = reader(inbox.Store(tmp_path, limits()), "s1")
    assert inbox.Store(tmp_path, limits()).fetch(token).body == "hello"


@pytest.mark.parametrize("purged", [False, True])
def test_a_retry_returns_the_original_and_a_changed_one_conflicts(tmp_path, store, purged):
    first = store.admit("ops", "k1", "pal", "héllo")
    if purged:
        acked(store, reader(store, "s1"))
        first = store.admit("ops", "k1", "pal", "héllo")
        assert first.state == "acknowledged"
    before = (rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path))
    assert store.admit("ops", "k1", "pal", "héllo") == first
    for changed in (("nunu", "héllo"), ("pal", "hello"), ("pal", "héllo ")):
        with pytest.raises(inbox.Conflict) as conflict:
            store.admit("ops", "k1", *changed)
        assert str(conflict.value) == "conflict"
    with pytest.raises(inbox.Conflict):
        store.admit("ops", "k1", "pal", "héllo", human=True)
    assert (rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path)) == before
    assert store.admit("other", "k1", "pal", "héllo").seq != first.seq  # another sender's key
    assert len(rows(tmp_path, "SELECT * FROM inbox_messages")) == 2


def test_a_live_body_is_compared_byte_for_byte_not_only_by_digest(store, monkeypatch):
    store.admit("ops", "k1", "pal", "one")
    forced = hashlib.sha256(b"one").digest()

    class Same:
        def __init__(self, data):
            pass

        def digest(self):
            return forced

    monkeypatch.setattr(inbox.hashlib, "sha256", Same)
    with pytest.raises(inbox.Conflict):
        store.admit("ops", "k1", "pal", "two")  # same length, same (forced) digest


@pytest.mark.parametrize("bad", [
    dict(key=""), dict(key="k" * 41), dict(to=""), dict(sender="s" * 21),
    dict(key=7), dict(body=b"bytes"), dict(body="\udcff"), dict(body="é" * (16 * 1024) + "x"),
])
def test_malformed_input_is_refused_before_the_store_is_touched(tmp_path, store, bad):
    args = {**dict(sender="ops", key="k1", to="pal", body="hi"), **bad}
    with pytest.raises(inbox.Invalid):
        store.admit(**args)
    assert not lodge_db.path(tmp_path).exists()


def test_the_32_kib_ceiling_counts_utf8_bytes(tmp_path, store):
    store = inbox.Store(tmp_path, limits(pending_bytes=40_000, mailbox_bytes=40_000))
    assert store.admit("ops", "k1", "pal", "é" * (16 * 1024)).state == "queued"


def test_limits_have_no_defaults_and_must_be_positive():
    with pytest.raises(TypeError):
        inbox.Limits(pending=1)
    for bad in (dict(pending=0), dict(events=True), dict(page=51)):
        with pytest.raises(inbox.Invalid):
            limits(**bad)


@pytest.mark.parametrize("code, tight, body", [
    ("keys", dict(keys=2), "x"),
    ("pending", dict(pending=2), "x"),
    ("pending_bytes", dict(pending_bytes=5), "xxx"),
    ("mailbox_pending", dict(mailbox_pending=1), "x"),
    ("mailbox_bytes", dict(mailbox_bytes=3), "xx"),
])
def test_each_limit_refuses_new_mail_but_never_a_retry(tmp_path, store, code, tight, body):
    store = inbox.Store(tmp_path, limits(**tight))
    store.admit("ops", "a", "pal", "xxx" if "bytes" in code else "x")
    if code in ("keys", "pending"):
        store.admit("ops", "b", "nunu", "x")
    before = (rows(tmp_path, "SELECT * FROM inbox_messages"), usage(tmp_path))
    with pytest.raises(inbox.Refused) as refused:
        store.admit("ops", "new", "pal", body)
    assert refused.value.code == code
    assert store.admit("ops", "a", "pal", "xxx" if "bytes" in code else "x").key == "a"
    with pytest.raises(inbox.Conflict):
        store.admit("ops", "a", "pal", "changed")
    assert (rows(tmp_path, "SELECT * FROM inbox_messages"), usage(tmp_path)) == before


def test_acknowledgement_frees_pending_room_but_keeps_the_key(tmp_path, store):
    store = inbox.Store(tmp_path, limits(keys=2, pending=1))
    token = reader(store, "s1")
    store.admit("ops", "a", "pal", "x")
    acked(store, token)
    store.admit("ops", "b", "pal", "x")
    acked(store, token)
    assert usage(tmp_path)[""] == (0, 0, 2)
    with pytest.raises(inbox.Refused, match="keys"):
        store.admit("ops", "c", "pal", "x")
    assert store.admit("ops", "a", "pal", "x").state == "acknowledged"


def test_shrunk_limits_keep_every_body_and_key(tmp_path, store):
    for key in ("a", "b", "c"):
        store.admit("ops", key, "pal", "xx")
    small = inbox.Store(tmp_path, limits(pending=1, pending_bytes=1, keys=1))
    for key in ("a", "b", "c"):
        assert small.admit("ops", key, "pal", "xx").state == "queued"
    with pytest.raises(inbox.Refused):
        small.admit("ops", "d", "pal", "x")
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages WHERE body IS NOT NULL") == [(3,)]


_CHILD = """
import json, sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import inbox, inbox_support
root, go, op, arg, body = Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], sys.argv[6], sys.argv[7]
store = inbox.Store(root, inbox_support.limits(pending=3))
while not go.exists():
    time.sleep(0.001)
try:
    if op == "admit":
        print(json.dumps(["ok", store.admit("ops", arg, "pal", body).seq]))
    else:
        got = store.fetch(arg)
        print(json.dumps(["ok", got and got.seq]))
except inbox.InboxError as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""


def _race(tmp_path, op, args, bodies):
    lib, here = Path(inbox.__file__).parent, Path(__file__).parent
    go = tmp_path / "go"
    go.unlink(missing_ok=True)
    procs = [subprocess.Popen([sys.executable, "-I", "-c", _CHILD, str(lib), str(here), str(tmp_path),
                               str(go), op, arg, body], stdout=subprocess.PIPE, text=True)
             for arg, body in zip(args, bodies)]
    go.touch()
    return [json.loads(p.communicate(timeout=60)[0]) for p in procs]


def test_sender_processes_racing_agree_on_one_message_and_never_overfill(tmp_path, store):
    store = inbox.Store(tmp_path, limits(pending=3))
    store.admit("ops", "a", "pal", "x")
    same = _race(tmp_path, "admit", ["k"] * 6, ["same"] * 6)
    assert len({tuple(r) for r in same}) == 1 and same[0][0] == "ok"
    mixed = _race(tmp_path, "admit", ["d"] * 3 + ["e"] * 3, ["one"] * 3 + ["two"] * 3)
    assert sorted(r[0] for r in mixed) == ["Refused"] * 3 + ["ok"] * 3
    assert rows(tmp_path, "SELECT count(*), count(DISTINCT key) FROM inbox_messages") == [(3, 3)]
    assert rows(tmp_path, "SELECT count(*) FROM inbox_events") == [(3,)]
    assert usage(tmp_path)[""] == (3, 1 + 4 + 3, 3)


def test_readers_fetching_at_once_get_distinct_claims(tmp_path, store):
    tokens = [reader(store, f"s{n}") for n in range(4)]
    for key in "abc":
        store.admit("ops", key, "pal", key)
    got = _race(tmp_path, "fetch", tokens, [""] * 4)
    assert sorted(got, key=str) == [["ok", 1], ["ok", 2], ["ok", 3], ["ok", None]]
    assert rows(tmp_path, "SELECT count(DISTINCT reader) FROM inbox_messages WHERE state='claimed'") == [(3,)]


def test_an_ack_its_event_and_the_purge_commit_or_roll_back_together(tmp_path, store):
    token = reader(store, "s1")
    store.admit("ops", "k1", "pal", "hello")
    got = store.fetch(token)
    tables = ("inbox_messages", "inbox_readers", "inbox_events", "inbox_usage")
    before = [rows(tmp_path, f"SELECT * FROM {t}") for t in tables]
    with failing(tmp_path, "inbox_events"):
        with pytest.raises(inbox.Unavailable):
            store.ack(token, got.seq, got.claim)
    assert [rows(tmp_path, f"SELECT * FROM {t}") for t in tables] == before
    assert store.ack(token, got.seq, got.claim).state == "acknowledged"
    assert state(tmp_path, "k1") == [("acknowledged", 1, 1)]
    assert usage(tmp_path)["1"] == (0, 0, 1)
    again = [rows(tmp_path, f"SELECT * FROM {t}") for t in tables]
    assert store.ack(token, got.seq, got.claim).state == "acknowledged"  # a repeat changes nothing
    assert [rows(tmp_path, f"SELECT * FROM {t}") for t in tables] == again
    assert rows(tmp_path, "SELECT acks FROM inbox_readers") == [(1,)]


@pytest.mark.parametrize("table", ["inbox_events", "inbox_usage", "inbox_messages"])
def test_a_failed_admission_leaves_nothing_behind(tmp_path, store, table):
    store.admit("ops", "a", "pal", "x")
    tables = ("inbox_messages", "inbox_events", "inbox_usage", "inbox_mailboxes")
    before = [rows(tmp_path, f"SELECT * FROM {t}") for t in tables]
    with failing(tmp_path, table, "UPDATE" if table == "inbox_usage" else "INSERT"):
        with pytest.raises(inbox.Unavailable):
            store.admit("ops", "b", "nunu", "y")
    assert [rows(tmp_path, f"SELECT * FROM {t}") for t in tables] == before


def test_every_store_write_is_full_and_ordinary_opens_stay_normal(tmp_path, store, monkeypatch):
    token = reader(store, "s1")
    seen, real = [], lodge_db.connect

    def connect(root, **kw):
        conn = real(root, **kw)
        seen.append((kw.get("readonly", False), conn.execute("PRAGMA synchronous").fetchone()[0]))
        return conn

    monkeypatch.setattr(lodge_db, "connect", connect)
    store.admit("ops", "k1", "pal", "x")
    got = store.fetch(token)
    store.release(token, got.seq, got.claim)
    acked(store, token)
    store.status(token)
    with lodge_db.write(tmp_path):
        pass
    assert seen == [(False, 2)] * 5 + [(True, 1), (False, 1)]


def test_a_read_never_creates_a_store_and_says_unavailable(tmp_path, store):
    with pytest.raises(inbox.Unavailable):
        store.status("t" * 43)
    with pytest.raises(inbox.Unavailable):
        store.events()
    assert not lodge_db.path(tmp_path).exists()


def test_a_failed_read_is_unavailable_never_missing(tmp_path, store, monkeypatch):
    token = reader(store, "s1")
    store.admit("ops", "k1", "pal", "x")
    assert store.status(token, "nope") is None

    def broken(root, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(lodge_db, "connect", broken)
    for read in (lambda: store.status(token), lambda: store.status(token, "nope"), store.events):
        with pytest.raises(inbox.Unavailable):
            read()


def test_a_locked_or_unwritable_store_is_unavailable(tmp_path, store):
    store = inbox.Store(tmp_path, limits(busy_ms=50))
    store.admit("ops", "a", "pal", "x")
    holder = sqlite3.connect(lodge_db.path(tmp_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(inbox.Unavailable, match="store_busy"):
        store.admit("ops", "b", "pal", "x")
    assert time.monotonic() - started < 2  # the supplied bound, not lodge_db's 5 s
    holder.execute("ROLLBACK")
    holder.close()
    os.chmod(lodge_db.path(tmp_path), 0o400)
    try:
        with pytest.raises(inbox.Unavailable):
            store.admit("ops", "b", "pal", "x")
    finally:
        os.chmod(lodge_db.path(tmp_path), 0o600)
    assert rows(tmp_path, "SELECT key FROM inbox_messages") == [("a",)]


def test_a_lost_admission_confirmation_is_recovered_by_the_same_key(tmp_path, store, monkeypatch):
    lose_confirmation(monkeypatch, 1)
    with pytest.raises(inbox.Unavailable):
        store.admit("ops", "k1", "pal", "x")
    again = store.admit("ops", "k1", "pal", "x")
    assert again.state == "queued"
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages") == [(1,)]
    assert rows(tmp_path, "SELECT count(*) FROM inbox_events") == [(1,)]


def test_human_mail_is_fetched_first_then_the_oldest(store):
    token = reader(store, "s1")
    for sender, key in (("ops", "a"), ("ops", "b")):
        store.admit(sender, key, "pal", key)
    store.admit("owner", "h", "pal", "h", human=True)
    order = []
    while (got := store.fetch(token)) is not None:
        order.append((got.body, got.priority, got.sender))
        store.ack(token, got.seq, got.claim)
    assert order == [("h", inbox.HUMAN, "owner"), ("a", inbox.AGENT, "ops"), ("b", inbox.AGENT, "ops")]


def test_a_lost_fetch_reply_returns_the_same_claim_marked_repeated(store):
    token = reader(store, "s1")
    for key in "ab":
        store.admit("ops", key, "pal", key)
    first = store.fetch(token)  # its reply lost: the caller asks again
    again = store.fetch(token)
    assert (again.seq, again.claim, again.body, first.repeated, again.repeated) == (
        first.seq, first.claim, "a", False, True)
    other = reader(store, "s2")  # another session of the same wolt
    assert store.fetch(other).body == "b"
    with pytest.raises(inbox.Refused, match="stale_claim"):
        store.ack(other, first.seq, first.claim)
    store.ack(token, first.seq, first.claim)
    assert store.fetch(token) is None


def test_release_requeues_for_any_reader_with_a_new_claim(store):
    token, other = reader(store, "s1"), reader(store, "s2")
    store.admit("ops", "a", "pal", "a")
    got = store.fetch(token)
    assert store.release(token, got.seq, got.claim).state == "queued"
    with pytest.raises(inbox.Refused, match="stale_claim"):
        store.release(token, got.seq, got.claim)
    again = store.fetch(other)
    assert (again.seq, again.claim, again.repeated) == (got.seq, got.claim + 1, False)
    with pytest.raises(inbox.Refused, match="stale_claim"):
        store.ack(token, got.seq, got.claim)


def test_a_relaunched_or_stopped_session_holds_its_claim_while_other_mail_moves(tmp_path, store):
    old = reader(store, "s1")
    store.admit("ops", "a", "pal", "a")
    claimed = store.fetch(old)
    new = store.grant("s1", "pal")  # a relaunch revokes the session's earlier reader
    for call in (lambda: store.fetch(old), lambda: store.ack(old, claimed.seq, claimed.claim)):
        with pytest.raises(inbox.Refused, match="reader_invalid"):
            call()
    assert store.status(new) == {"queued": 0, "claimed": 0, "held": 1, "acknowledged": 0}
    store.admit("owner", "h", "pal", "h", human=True)
    assert store.fetch(new).body == "h"  # held mail is not reassigned
    stopped, moved = reader(store, "s3"), reader(store, "s4")
    session(tmp_path, "s3", status="stopped")
    session(tmp_path, "s4", wolt="nunu")  # the row now names another wolt
    for token in (stopped, moved):
        with pytest.raises(inbox.Refused, match="reader_invalid"):
            store.fetch(token)


def test_a_status_read_is_one_snapshot_with_its_reader_check(tmp_path, store, monkeypatch):
    token = reader(store, "s1")
    real = inbox.Store._reader

    def checked_then_changed(conn, hashed):
        found = real(conn, hashed)
        store.admit("ops", "late", "pal", "x")  # another writer commits right after the check
        session(tmp_path, "s1", status="stopped")
        return found

    monkeypatch.setattr(inbox.Store, "_reader", staticmethod(checked_then_changed))
    assert store.status(token)["queued"] == 0  # what the checked snapshot held, nothing later


def test_a_reader_sends_as_its_own_wolt_and_reads_only_its_mailbox(store):
    pal, nunu = reader(store, "s1"), reader(store, "s2", "nunu")
    sent = store.send(pal, "nunu", "k1", "hi")
    assert (sent.sender, sent.to, sent.priority) == ("pal", "nunu", inbox.AGENT)
    assert store.fetch(pal) is None
    assert store.fetch(nunu).sender == "pal"
    assert store.status(pal, "k1").state == "claimed"
    with pytest.raises(inbox.Refused, match="no_mailbox"):
        store.send(pal, "nobody", "k2", "hi")
    for bad in ("", "short", None, "t" * 43):
        with pytest.raises(inbox.Refused, match="reader_invalid"):
            store.send(bad, "nunu", "k3", "hi")
    for key in (7, "", "k" * 41):
        with pytest.raises(inbox.Invalid):
            store.status(pal, key)


def test_archive_blocks_mail_and_claims_and_close_waits_for_custody(tmp_path, store):
    token = reader(store, "s1")
    store.admit("ops", "a", "pal", "a")
    store.set_state("pal", "archived")
    with pytest.raises(inbox.Refused, match="mailbox_archived"):
        store.admit("ops", "b", "pal", "b")
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.fetch(token)
    assert store.admit("ops", "a", "pal", "a").state == "queued"  # a retry still answers
    with pytest.raises(inbox.Refused, match="mailbox_custody"):
        store.set_state("pal", "closed")
    store.set_state("pal", "open")
    acked(store, token)
    store.set_state("pal", "closed")
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.fetch(token)
    fresh = reader(store, "s2")  # the name again: a new mailbox, none of the old mail or keys
    assert rows(tmp_path, "SELECT id, state FROM inbox_mailboxes") == [(1, "closed"), (2, "open")]
    assert store.status(fresh) == {"queued": 0, "claimed": 0, "held": 0, "acknowledged": 0}
    with pytest.raises(inbox.Conflict):
        store.admit("ops", "a", "pal", "a")  # its key is bound to the closed mailbox


def test_a_recreated_sender_starts_its_own_key_namespace(tmp_path, store):
    old = reader(store, "s1")
    first = store.send(old, "nunu", "k1", "one")
    store.set_state("pal", "closed")  # nothing waits in pal's own mailbox
    new = reader(store, "s2")  # the name again: a new mailbox, a new sender id
    second = store.send(new, "nunu", "k1", "two")  # the same key is new mail
    assert (second.seq != first.seq, second.state, store.status(new, "k1")) == (True, "queued", second)
    assert rows(tmp_path, "SELECT sender, sender_name, key, body FROM inbox_messages ORDER BY seq") == [
        ("mailbox:1", "pal", "k1", b"one"), ("mailbox:3", "pal", "k1", b"two")]
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        store.send(old, "nunu", "k2", "x")


def test_acknowledged_metadata_goes_30_days_after_the_first_ack_and_nothing_else(tmp_path, store, monkeypatch):
    token = reader(store, "s1")
    for key in "abcd":
        store.admit("ops", key, "pal", key)
    clock = [time.time()]
    monkeypatch.setattr(inbox.time, "time", lambda: clock[0])
    got = store.fetch(token)
    store.ack(token, got.seq, got.claim)
    clock[0] += inbox.RETAIN_SECONDS - 10
    store.ack(token, got.seq, got.claim)  # a repeat does not extend it
    held = store.fetch(token)
    store.grant("s1", "pal")
    clock[0] += 20
    store.admit("ops", "e", "pal", "e")
    assert [r[0] for r in rows(tmp_path, "SELECT key FROM inbox_messages ORDER BY seq")] == list("bcde")
    assert state(tmp_path, held.body) == [("claimed", 1, 0)]
    assert usage(tmp_path)[""] == (4, 4, 4)
    assert store.admit("ops", "a", "pal", "new").state == "queued"  # the key is free again


def test_after_its_30_days_a_receipt_is_unknown_even_to_its_reader(tmp_path, store, monkeypatch):
    token = reader(store, "s1")
    clock = [time.time()]
    monkeypatch.setattr(inbox.time, "time", lambda: clock[0])
    store.send(token, "pal", "k", "x")
    got = store.fetch(token)
    store.ack(token, got.seq, got.claim)
    clock[0] += inbox.RETAIN_SECONDS + 1
    assert (store.status(token, "k"), store.status(token)["acknowledged"]) == (None, 0)
    with pytest.raises(inbox.Refused, match="stale_claim"):
        store.ack(token, got.seq, got.claim)  # never an invented success


def test_the_event_ring_drops_its_oldest_shows_the_gap_and_keeps_the_facts(tmp_path, store):
    store = inbox.Store(tmp_path, limits(events=3, page=2))
    for key in "abcd":
        store.admit("ops", key, "pal", "x")
    page = store.events()
    assert ([e.cursor for e in page.events], page.next, page.gap, page.oldest, page.newest) == (
        [2, 3], 3, True, 2, 4)
    page = store.events(page.next, limit=50)
    assert ([e.cursor for e in page.events], page.gap) == ([4], False)
    with store._write() as conn:  # the same observation again is counted, not added
        store._event(conn, 4, "queued", None)
    assert rows(tmp_path, "SELECT state, repeats FROM inbox_events ORDER BY cursor DESC LIMIT 1") == [
        ("queued", 2)]


def test_the_migration_is_idempotent_and_its_tables_start_empty(tmp_path):
    for _ in range(2):
        lodge_db.connect(tmp_path).close()
    assert 5 in lodge_db.applied_migrations(lodge_db.connect(tmp_path))
    for table in ("inbox_mailboxes", "inbox_readers", "inbox_messages", "inbox_events", "inbox_usage"):
        assert rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_importing_the_inbox_does_nothing(tmp_path):
    lib = Path(inbox.__file__).parent
    code = "import sys, threading; sys.path.insert(0, sys.argv[1]); import inbox; print(threading.active_count())"
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "WOLTSPACE_WOLTS_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-I", "-c", code, str(lib)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert out.split() == ["1"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("config, on", [
    (None, False), ("{}", False), ('{"inbox": {"enabled": "yes"}}', False), ("[1]", False),
    ("not json", False), ('{"inbox": {"enabled": true}}', True),
])
def test_the_inbox_is_off_unless_woltspace_json_enables_it(tmp_path, config, on):
    if config is not None:
        (tmp_path / "woltspace.json").write_text(config)
    assert inbox.enabled(tmp_path) is on


@pytest.mark.parametrize("kwargs", [
    {"limit": -1}, {"limit": 0}, {"limit": True}, {"limit": 1.5}, {"limit": "2"},
    {"after": -1}, {"after": True}, {"after": 1.5}, {"after": "0"},
    {"after": 2**100},
])
def test_event_page_rejects_invalid_bounds_before_read(store, monkeypatch, kwargs):
    def unexpected_read():
        pytest.fail("malformed page opened storage")
    monkeypatch.setattr(store, "_read", unexpected_read)
    with pytest.raises(inbox.Invalid):
        store.events(**kwargs)


def test_negative_page_limit_cannot_return_the_whole_ring(tmp_path, store):
    store = inbox.Store(tmp_path, limits(page=1))
    for key in ("a", "b", "c"):
        store.admit("ops", key, "pal", "x")
    with pytest.raises(inbox.Invalid):
        store.events(limit=-1)
    assert len(store.events().events) == 1
    assert len(store.events(limit=50).events) == 1  # positive requests clamp safely


@pytest.mark.parametrize("field", ["key", "to", "sender"])
def test_unencodable_identity_is_invalid_before_write(store, monkeypatch, field):
    args = dict(sender="ops", key="a", to="pal", body="x")
    args[field] = "\udcff"
    def unexpected_write():
        pytest.fail("unencodable identity opened storage")
    monkeypatch.setattr(store, "_write", unexpected_write)
    with pytest.raises(inbox.Invalid):
        store.admit(**args)


def test_event_page_and_gap_share_one_committed_snapshot(tmp_path, store, monkeypatch):
    store = inbox.Store(tmp_path, limits(events=3, page=1))
    for key in "abc":
        store.admit("ops", key, "pal", "x")
    real_connect, pruned = lodge_db.connect, []

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchall(self):
            result = self.cursor.fetchall()
            if not pruned:
                pruned.append(True)
                # Another writer commits after the page is fetched, before
                # any later query for the oldest/newest cursor.
                store.admit("ops", "d", "pal", "x")
            return result

        def __getattr__(self, name):
            return getattr(self.cursor, name)

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, *args):
            cursor = self.conn.execute(sql, *args)
            if "inbox_events" in sql and "ORDER BY cursor" in sql:
                return Cursor(cursor)
            return cursor

        def __getattr__(self, name):
            return getattr(self.conn, name)

    def connect(root, **kwargs):
        conn = real_connect(root, **kwargs)
        return Connection(conn) if kwargs.get("readonly") else conn

    monkeypatch.setattr(lodge_db, "connect", connect)
    page = store.events()
    assert ([e.cursor for e in page.events], page.gap) in [([1], False), ([2], True)]
    assert rows(tmp_path, "SELECT min(cursor), max(cursor) FROM inbox_events") == [(2, 4)]
