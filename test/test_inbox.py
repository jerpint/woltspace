"""inbox: the store under RFC #615's inbox, durable admission to recorded outcome."""

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from inbox_support import accept, failing, limits, lose_confirmation, rows, state, usage

import inbox
import lodge_db

Outcome = inbox.Outcome


@pytest.fixture
def store(tmp_path):
    return inbox.Store(tmp_path, limits())


def test_a_receipt_is_the_committed_row_and_survives_reopening(tmp_path, store):
    receipt = store.admit("k1", "pal", "c1", "hello")
    assert (receipt.key, receipt.state, receipt.calls) == ("k1", "queued", 0)
    assert rows(tmp_path, "SELECT body, size, digest FROM inbox_messages") == [
        (b"hello", 5, hashlib.sha256(b"hello").digest())]
    assert rows(tmp_path, "SELECT seq, state FROM inbox_events") == [(receipt.seq, "queued")]
    assert usage(tmp_path) == {"": (1, 5, 1), "pal": (1, 5, 1)}
    assert inbox.Store(tmp_path, limits()).status("k1") == receipt


@pytest.mark.parametrize("purged", [False, True])
def test_a_retry_returns_the_original_and_a_changed_one_conflicts(tmp_path, store, purged):
    first = store.admit("k1", "pal", "c1", "héllo")
    if purged:
        accept(store)
        first = store.status("k1")
    before = (rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path))
    assert store.admit("k1", "pal", "c1", "héllo") == first
    for changed in (("nunu", "c1", "héllo"), ("pal", "c2", "héllo"), ("pal", "c1", "hello"),
                    ("pal", "c1", "héllo ")):
        with pytest.raises(inbox.Conflict):
            store.admit("k1", *changed)
    assert (rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path)) == before
    assert len(rows(tmp_path, "SELECT * FROM inbox_messages")) == 1


def test_a_live_body_is_compared_byte_for_byte_not_only_by_digest(store, monkeypatch):
    store.admit("k1", "pal", "c1", "one")
    forced = hashlib.sha256(b"one").digest()

    class Same:
        def __init__(self, data):
            pass

        def digest(self):
            return forced

    monkeypatch.setattr(inbox.hashlib, "sha256", Same)
    with pytest.raises(inbox.Conflict):
        store.admit("k1", "pal", "c1", "two")  # same length, same (forced) digest


@pytest.mark.parametrize("bad", [
    dict(key=""), dict(key="k" * 41), dict(recipient=""), dict(conversation="c" * 21),
    dict(key=7), dict(body=b"bytes"), dict(body="\udcff"), dict(body="é" * (16 * 1024) + "x"),
])
def test_malformed_input_is_refused_before_the_store_is_touched(tmp_path, store, bad):
    args = {**dict(key="k1", recipient="pal", conversation="c1", body="hi"), **bad}
    with pytest.raises(inbox.Invalid):
        store.admit(**args)
    assert not lodge_db.path(tmp_path).exists()


def test_the_32_kib_ceiling_counts_utf8_bytes(tmp_path):
    store = inbox.Store(tmp_path, limits(pending_bytes=40_000, recipient_bytes=40_000))
    assert store.admit("k1", "pal", "c1", "é" * (16 * 1024)).state == "queued"
    with pytest.raises(TypeError):
        store.admit("k2", "pal", "c1", "x", priority=1)  # no field beyond the four


def test_limits_have_no_defaults_and_must_be_positive():
    with pytest.raises(TypeError):
        inbox.Limits(pending=1)
    for bad in (dict(pending=0), dict(events=True), dict(page=51)):
        with pytest.raises(inbox.Invalid):
            limits(**bad)


@pytest.mark.parametrize("code, tight, body", [
    ("keys", dict(keys=2), "x"),
    ("attempts", dict(attempts=8), "x"),
    ("pending", dict(pending=2), "x"),
    ("pending_bytes", dict(pending_bytes=5), "xxx"),
    ("recipient_pending", dict(recipient_pending=1), "x"),
    ("recipient_bytes", dict(recipient_bytes=3), "xx"),
])
def test_each_limit_refuses_new_mail_but_never_a_retry(tmp_path, code, tight, body):
    store = inbox.Store(tmp_path, limits(**tight))
    store.admit("a", "pal", "c1", "xxx" if "bytes" in code else "x")
    if code in ("keys", "attempts", "pending"):
        store.admit("b", "nunu", "c1", "x")
    before = (rows(tmp_path, "SELECT * FROM inbox_messages"), usage(tmp_path))
    with pytest.raises(inbox.Refused) as refused:
        store.admit("new", "pal", "c1", body)
    assert refused.value.code == code
    assert store.admit("a", "pal", "c1", "xxx" if "bytes" in code else "x").key == "a"
    with pytest.raises(inbox.Conflict):
        store.admit("a", "pal", "c1", "changed")
    assert (rows(tmp_path, "SELECT * FROM inbox_messages"), usage(tmp_path)) == before


def test_acceptance_frees_pending_room_but_never_the_key(tmp_path):
    store = inbox.Store(tmp_path, limits(keys=2, pending=1))
    store.admit("a", "pal", "c1", "x")
    accept(store)
    store.admit("b", "pal", "c1", "x")
    accept(store)
    assert usage(tmp_path)[""] == (0, 0, 2)
    with pytest.raises(inbox.Refused, match="keys"):
        store.admit("c", "pal", "c1", "x")
    assert store.admit("a", "pal", "c1", "x").state == "native_accepted"


def test_shrunk_limits_keep_every_body_and_key(tmp_path, store):
    for key in ("a", "b", "c"):
        store.admit(key, "pal", "c1", "xx")
    small = inbox.Store(tmp_path, limits(pending=1, pending_bytes=1, keys=1))
    for key in ("a", "b", "c"):
        assert small.admit(key, "pal", "c1", "xx").state == "queued"
    with pytest.raises(inbox.Refused):
        small.admit("d", "pal", "c1", "x")
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages WHERE body IS NOT NULL") == [(3,)]


_SENDER = """
import json, sys, time
from pathlib import Path
sys.path[:0] = [sys.argv[1], sys.argv[2]]
import inbox, inbox_support
root, go, key, body = Path(sys.argv[3]), Path(sys.argv[4]), sys.argv[5], sys.argv[6]
store = inbox.Store(root, inbox_support.limits(pending=3))
while not go.exists():
    time.sleep(0.001)
try:
    print(json.dumps(["ok", store.admit(key, "pal", "c1", body).seq]))
except inbox.InboxError as exc:
    print(json.dumps([type(exc).__name__, str(exc)]))
"""


def _race(tmp_path, keys, bodies):
    lib, here = Path(inbox.__file__).parent, Path(__file__).parent
    go = tmp_path / "go"
    go.unlink(missing_ok=True)
    procs = [subprocess.Popen([sys.executable, "-I", "-c", _SENDER, str(lib), str(here), str(tmp_path),
                               str(go), key, body], stdout=subprocess.PIPE, text=True)
             for key, body in zip(keys, bodies)]
    go.touch()
    return [json.loads(p.communicate(timeout=60)[0]) for p in procs]


def test_sender_processes_racing_agree_on_one_message_and_never_overfill(tmp_path):
    store = inbox.Store(tmp_path, limits(pending=3))
    store.admit("a", "pal", "c1", "x")
    same = _race(tmp_path, ["k"] * 6, ["same"] * 6)
    assert len({tuple(r) for r in same}) == 1 and same[0][0] == "ok"
    mixed = _race(tmp_path, ["d"] * 3 + ["e"] * 3, ["one"] * 3 + ["two"] * 3)
    assert sorted(r[0] for r in mixed) == ["Refused"] * 3 + ["ok"] * 3
    assert rows(tmp_path, "SELECT count(*), count(DISTINCT key) FROM inbox_messages") == [(3, 3)]
    assert rows(tmp_path, "SELECT count(*) FROM inbox_events") == [(3,)]
    assert usage(tmp_path)[""] == (3, 1 + 4 + 3, 3)


def test_acceptance_its_event_and_the_purge_commit_or_roll_back_together(tmp_path, store):
    store.admit("k1", "pal", "c1", "hello")
    attempt = store.claim("pal", "c1", "g1")
    before = (rows(tmp_path, "SELECT * FROM inbox_messages"), rows(tmp_path, "SELECT * FROM inbox_attempts"),
              rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path))
    with failing(tmp_path, "inbox_events"):
        with pytest.raises(inbox.Unavailable):
            store.record(attempt, Outcome.ACCEPTED)
    assert (rows(tmp_path, "SELECT * FROM inbox_messages"), rows(tmp_path, "SELECT * FROM inbox_attempts"),
            rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path)) == before
    store.record(attempt, Outcome.ACCEPTED)
    assert state(tmp_path, "k1") == [("native_accepted", None, 1, 1)]
    assert usage(tmp_path)["pal"] == (0, 0, 1)
    with pytest.raises(inbox.Stale):  # a second acceptance frees nothing twice
        store.record(attempt, Outcome.ACCEPTED)
    assert usage(tmp_path)["pal"] == (0, 0, 1)


@pytest.mark.parametrize("table", ["inbox_events", "inbox_usage", "inbox_messages"])
def test_a_failed_admission_leaves_nothing_behind(tmp_path, store, table):
    store.admit("a", "pal", "c1", "x")
    before = [rows(tmp_path, f"SELECT * FROM {t}") for t in ("inbox_messages", "inbox_events", "inbox_usage")]
    with failing(tmp_path, table, "UPDATE" if table == "inbox_usage" else "INSERT"):
        with pytest.raises(inbox.Unavailable):
            store.admit("b", "pal", "c1", "y")
    assert [rows(tmp_path, f"SELECT * FROM {t}") for t in ("inbox_messages", "inbox_events", "inbox_usage")] == before
    assert store.status("b") is None


def test_every_store_write_is_full_and_ordinary_opens_stay_normal(tmp_path, store, monkeypatch):
    seen, real = [], lodge_db.connect

    def connect(root, **kw):
        conn = real(root, **kw)
        seen.append((kw.get("readonly", False), conn.execute("PRAGMA synchronous").fetchone()[0]))
        return conn

    monkeypatch.setattr(lodge_db, "connect", connect)
    store.admit("k1", "pal", "c1", "x")
    accept(store)
    store.reconcile()
    store.status("k1")
    with lodge_db.write(tmp_path):
        pass
    assert seen == [(False, 2)] * 4 + [(True, 1), (False, 1)]


def test_a_status_read_never_creates_a_store_and_says_unavailable(tmp_path, store):
    with pytest.raises(inbox.Unavailable):
        store.status("k1")
    with pytest.raises(inbox.Unavailable):
        store.events()
    assert not lodge_db.path(tmp_path).exists()


def test_a_failed_read_is_unavailable_never_missing(tmp_path, store, monkeypatch):
    store.admit("k1", "pal", "c1", "x")
    assert store.status("nope") is None

    def broken(root, **kw):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(lodge_db, "connect", broken)
    for read in (lambda: store.status("k1"), lambda: store.status("nope"), store.events):
        with pytest.raises(inbox.Unavailable):
            read()


def test_a_locked_or_unwritable_store_is_unavailable(tmp_path):
    store = inbox.Store(tmp_path, limits(busy_ms=50))
    store.admit("a", "pal", "c1", "x")
    holder = sqlite3.connect(lodge_db.path(tmp_path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    with pytest.raises(inbox.Unavailable, match="store_busy"):
        store.admit("b", "pal", "c1", "x")
    assert time.monotonic() - started < 2  # the supplied bound, not lodge_db's 5 s
    holder.execute("ROLLBACK")
    holder.close()
    os.chmod(lodge_db.path(tmp_path), 0o400)
    try:
        with pytest.raises(inbox.Unavailable):
            store.admit("b", "pal", "c1", "x")
    finally:
        os.chmod(lodge_db.path(tmp_path), 0o600)
    assert store.status("b") is None


def test_a_lost_admission_confirmation_is_recovered_by_the_same_key(tmp_path, store, monkeypatch):
    lose_confirmation(monkeypatch, 1)
    with pytest.raises(inbox.Unavailable):
        store.admit("k1", "pal", "c1", "x")
    again = store.admit("k1", "pal", "c1", "x")
    assert again.state == "queued"
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages") == [(1,)]
    assert rows(tmp_path, "SELECT count(*) FROM inbox_events") == [(1,)]


@pytest.mark.parametrize("outcome, expect", [
    (Outcome.NOT_ADMITTED, ("queued", None)),
    (Outcome.DEFERRED, ("blocked", "defer_without_submission")),
    (Outcome.USER_STOP, ("blocked", "user_stop")),
    (Outcome.UNKNOWN, ("uncertain", "unknown")),
])
def test_each_outcome_keeps_the_body_and_its_state(tmp_path, store, outcome, expect):
    store.admit("k1", "pal", "c1", "x")
    store.record(store.claim("pal", "c1", "g1"), outcome)
    assert state(tmp_path, "k1") == [(*expect, 1, 0)]
    assert usage(tmp_path)["pal"] == (1, 1, 1)


def test_three_refused_calls_block_the_message(tmp_path, store):
    store.admit("k1", "pal", "c1", "x")
    for _ in range(inbox.MAX_CALLS):
        store.record(store.claim("pal", "c1", "g1"), Outcome.NOT_ADMITTED)
    assert state(tmp_path, "k1") == [("blocked", "calls_used", 3, 0)]
    assert store.claim("pal", "c1", "g1") is None
    assert rows(tmp_path, "SELECT ordinal, outcome FROM inbox_attempts") == [
        (n, "definitive_non_admission") for n in (1, 2, 3)]


def test_a_result_for_another_attempt_or_generation_changes_nothing(tmp_path, store):
    store.admit("k1", "pal", "c1", "x")
    attempt = store.claim("pal", "c1", "g1")
    before = rows(tmp_path, "SELECT * FROM inbox_messages")
    for wrong in (inbox.Attempt("nope", attempt.seq, "k1", 1, "g1", "x"),
                  inbox.Attempt(attempt.id, attempt.seq, "k1", 1, "g0", "x")):
        with pytest.raises(inbox.Stale):
            store.record(wrong, Outcome.ACCEPTED)
    assert rows(tmp_path, "SELECT * FROM inbox_messages") == before


def test_a_held_head_holds_its_conversation_but_not_others(tmp_path, store):
    for key, conv in (("a", "c1"), ("b", "c1"), ("c", "c2")):
        store.admit(key, "pal", conv, key)
    first = store.claim("pal", "c1", "g1")
    assert store.claim("pal", "c1", "g1") is None  # attempting holds b
    assert store.claim("pal", "c2", "g1").key == "c"
    for outcome in (Outcome.UNKNOWN, Outcome.DEFERRED):  # so do uncertain and blocked
        conn = sqlite3.connect(lodge_db.path(store.root))
        conn.execute("UPDATE inbox_messages SET state=? WHERE seq=?",
                     ("uncertain" if outcome is Outcome.UNKNOWN else "blocked", first.seq))
        conn.commit()
        conn.close()
        assert store.claim("pal", "c1", "g1") is None


def test_reconcile_makes_an_unrecorded_attempt_uncertain_and_never_requeues(tmp_path, store):
    for key in ("a", "b", "c"):
        store.admit(key, "pal", key, "x")
    accept(store, conversation="a")
    store.claim("pal", "b", "g1")
    assert sorted(inbox.Store(tmp_path, limits()).reconcile()) == [("pal", "b"), ("pal", "c")]
    assert [state(tmp_path, k)[0][0] for k in "abc"] == ["native_accepted", "uncertain", "queued"]
    assert rows(tmp_path, "SELECT outcome FROM inbox_attempts ORDER BY ordinal, seq") == [
        ("accepted",), ("unknown",)]
    assert store.claim("pal", "b", "g2") is None
    store.claim("pal", "c", "g2")
    with failing(tmp_path, "inbox_events"):
        with pytest.raises(inbox.Unavailable):
            store.reconcile()
    assert state(tmp_path, "c")[0][0] == "attempting"


def test_the_event_ring_drops_its_oldest_shows_the_gap_and_keeps_the_facts(tmp_path):
    store = inbox.Store(tmp_path, limits(events=3, page=2))
    for key in "abcd":
        store.admit(key, "pal", key, "x")
    page = store.events()
    assert ([e.cursor for e in page.events], page.next, page.gap, page.oldest, page.newest) == (
        [2, 3], 3, True, 2, 4)
    page = store.events(page.next, limit=50)
    assert ([e.cursor for e in page.events], page.gap) == ([4], False)
    assert [store.status(k).state for k in "abcd"] == ["queued"] * 4
    attempt = store.claim("pal", "a", "g1")
    store.record(attempt, Outcome.DEFERRED)
    with store._write() as conn:  # the same observation again is counted, not added
        store._event(conn, attempt.seq, "blocked", "defer_without_submission")
    assert rows(tmp_path, "SELECT state, repeats FROM inbox_events ORDER BY cursor DESC LIMIT 2") == [
        ("blocked", 2), ("attempting", 1)]


def test_the_migration_is_idempotent_and_its_tables_start_empty(tmp_path):
    for _ in range(2):
        lodge_db.connect(tmp_path).close()
    assert 5 in lodge_db.applied_migrations(lodge_db.connect(tmp_path))
    for table in ("inbox_messages", "inbox_attempts", "inbox_events", "inbox_usage"):
        assert rows(tmp_path, f"SELECT count(*) FROM {table}") == [(0,)]


def test_importing_the_inbox_does_nothing(tmp_path):
    lib = Path(inbox.__file__).parent
    code = ("import sys, threading; sys.path.insert(0, sys.argv[1]); import inbox, inbox_actor, inbox_binding;"
            " print(threading.active_count())")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), "WOLTSPACE_WOLTS_DIR": str(tmp_path)}
    out = subprocess.run([sys.executable, "-I", "-c", code, str(lib)], cwd=tmp_path, env=env,
                         capture_output=True, text=True, check=True).stdout
    assert out.split() == ["1"]
    assert list(tmp_path.iterdir()) == []


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


def test_negative_page_limit_cannot_return_the_whole_ring(tmp_path):
    store = inbox.Store(tmp_path, limits(page=1))
    for key in ("a", "b", "c"):
        store.admit(key, "pal", "c1", "x")
    with pytest.raises(inbox.Invalid):
        store.events(limit=-1)
    assert len(store.events().events) == 1
    assert len(store.events(limit=50).events) == 1  # positive requests clamp safely


@pytest.mark.parametrize("field", ["key", "recipient", "conversation"])
def test_unencodable_identity_is_invalid_before_write(store, monkeypatch, field):
    args = dict(key="a", recipient="pal", conversation="c1", body="x")
    args[field] = "\udcff"
    def unexpected_write():
        pytest.fail("unencodable identity opened storage")
    monkeypatch.setattr(store, "_write", unexpected_write)
    with pytest.raises(inbox.Invalid):
        store.admit(**args)


@pytest.mark.parametrize("generation", ["", "g" * 21, "\udcff", 7, None, b"g"])
def test_claim_generation_is_bounded_utf8_before_write(tmp_path, store, monkeypatch, generation):
    store.admit("a", "pal", "c1", "x")
    before = (state(tmp_path, "a"), usage(tmp_path), rows(tmp_path, "SELECT * FROM inbox_events"))
    with monkeypatch.context() as patch:
        def unexpected_write():
            pytest.fail("invalid generation opened storage")
        patch.setattr(store, "_write", unexpected_write)
        with pytest.raises(inbox.Invalid):
            store.claim("pal", "c1", generation)
    assert (state(tmp_path, "a"), usage(tmp_path), rows(tmp_path, "SELECT * FROM inbox_events")) == before
    assert rows(tmp_path, "SELECT * FROM inbox_attempts") == []


def test_event_page_and_gap_share_one_committed_snapshot(tmp_path, monkeypatch):
    store = inbox.Store(tmp_path, limits(events=3, page=1))
    for key in "abc":
        store.admit(key, "pal", "c1", "x")
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
                store.admit("d", "pal", "c1", "x")
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
