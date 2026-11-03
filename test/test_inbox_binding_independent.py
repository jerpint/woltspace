"""Independent custody oracles: owned transactions and ambiguous commit results."""
import asyncio
import sqlite3
import traceback
from contextlib import closing, contextmanager
from pathlib import Path

import pytest

from inbox_support import authority, enroll, rows, session, state
import inbox
import inbox_actor
import lodge_db
import sessions

TABLES = ("sessions", "meta", "inbox_messages", "inbox_attempts", "inbox_usage",
          "inbox_events", "sqlite_sequence")


def snapshot(root):
    """Fresh independent readers see every persistent effect, including reservations."""
    return {table: sorted(rows(root, f"SELECT * FROM {table}"), key=repr) for table in TABLES}


def prepared(root, operation):
    auth = authority(root)
    binding = enroll(auth, "s1")
    if operation == "enroll":
        session(root, "s2")
        call = lambda: auth.enroll("s2", harness="claude", conversation="conv-s2",
                                  readiness="running", generation="g1")
    elif operation == "admit":
        call = lambda: auth.admit("new", "session-name", "s1", "héllo")
    else:
        auth.admit("held", "session-name", "s1", "body")
        if operation == "end":
            call = lambda: auth.transition(binding.incarnation, 1, status="stopped", readiness="unknown")
        else:
            call = lambda: auth.delete(binding.incarnation, 1)
    return auth, binding, call


class Connection:
    """Intercept actual SQLite COMMIT; all other operations reach the real connection."""
    def __init__(self, conn, fault):
        self.conn, self.fault, self.commits = conn, fault, 0

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        if sql == "COMMIT":
            self.commits += 1
            if self.fault == "before":
                raise sqlite3.OperationalError("private-fault-sentinel")
            result = self.conn.execute(sql, *args)
            if self.fault == "after":
                raise sqlite3.OperationalError("private-fault-sentinel")
            return result
        return self.conn.execute(sql, *args)


@pytest.mark.parametrize("operation", ["admit", "enroll", "end", "delete"])
@pytest.mark.parametrize("fault", ["before", "after"])
def test_actual_commit_boundary_distinguishes_rollback_from_lost_confirmation(tmp_path, monkeypatch,
                                                                           operation, fault):
    auth, binding, call = prepared(tmp_path, operation)
    before, real, connections = snapshot(tmp_path), lodge_db.connect, []

    def connect(*args, **kwargs):
        conn = Connection(real(*args, **kwargs), fault)
        connections.append(conn)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(lodge_db, "connect", connect)
        with pytest.raises(inbox.Unavailable) as caught:
            call()
    assert len(connections) == 1 and connections[0].commits == 1
    assert "private-fault-sentinel" not in "".join(traceback.format_exception(caught.value))
    assert not lodge_db.in_write() and not getattr(sessions._TX, "conns", {})
    persisted = snapshot(tmp_path)
    if fault == "before":
        assert persisted == before
        call()  # the failed operation did not consume its revision, key or enrollment slot
    else:
        assert persisted != before
        if operation == "admit":
            assert auth.admit("new", "session-name", "s1", "héllo").state == "queued"
            assert rows(tmp_path, "SELECT pending, bytes, keys FROM inbox_usage WHERE scope=''") == [(1, 6, 1)]
        elif operation == "enroll":
            assert call().incarnation == rows(tmp_path, "SELECT inbox_incarnation FROM sessions WHERE name='s2'")[0][0]
            assert rows(tmp_path, "SELECT value FROM meta WHERE key='inbox:enrollments'") == [("2",)]
        else:
            assert auth.admit("held", "session-name", "s1", "body").reason == "target_ended"
            with pytest.raises(inbox.Refused, match="stale_revision|no_target"):
                call()
        assert snapshot(tmp_path) == persisted  # recovery creates no second effect/event
    with lodge_db.write(tmp_path) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["admit", "enroll", "end", "delete"])
def test_owned_transaction_is_one_physical_full_connection_and_never_ambient(tmp_path, monkeypatch,
                                                                         operation):
    auth, _, call = prepared(tmp_path, operation)
    real, observed, trace, ambient = lodge_db.connect, [], [], []

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        observed.append((id(conn), kwargs, conn.execute("PRAGMA synchronous").fetchone()[0],
                         Path(conn.execute("PRAGMA database_list").fetchone()["file"])))

        def sql(statement):
            ambient.append(bool(getattr(sessions._TX, "conns", {})))
            trace.append((id(conn), statement))

        conn.set_trace_callback(sql)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(lodge_db, "connect", connect)
        call()
    assert len(observed) == 1 and not any(ambient)
    identity, options, synchronous, path = observed[0]
    assert options["durable"] is True and synchronous == 2
    assert path.resolve() == lodge_db.path(tmp_path).resolve()
    assert {item[0] for item in trace} == {identity}
    statements = [item[1] for item in trace]
    assert statements.count("BEGIN IMMEDIATE") == statements.count("COMMIT") == 1
    assert any("sessions" in sql for sql in statements)
    if operation != "enroll":
        assert any("inbox_messages" in sql for sql in statements)


@pytest.mark.parametrize("outer", ["normal", "full", "foreign_registry", "map_only"])
def test_any_outer_write_or_map_refuses_before_connect_and_preserves_caller(tmp_path, monkeypatch, outer):
    auth, binding, _ = prepared(tmp_path / "owned", "end")
    root = tmp_path / "foreign" if outer == "foreign_registry" else auth.store.root
    registry = sessions.SessionRegistry(wolts_dir=root)
    context = registry._tx() if outer in ("foreign_registry", "map_only") else lodge_db.write(root, durable=outer == "full")
    with context as caller:
        caller.execute("INSERT INTO meta VALUES ('caller-before', 'ok')")
        mapping = dict(getattr(sessions._TX, "conns", {}))
        statements = []
        caller.set_trace_callback(statements.append)
        with monkeypatch.context() as patch:
            if outer == "map_only":
                patch.setattr(lodge_db._STATE, "writing", False)
            def forbidden(*args, **kwargs):
                pytest.fail("authority opened a connection inside caller's transaction")
            patch.setattr(lodge_db, "connect", forbidden)
            calls = [lambda: auth.admit("held", "session-name", "s1", "body"),
                     lambda: auth.enroll("s1", harness="claude", conversation="conv-s1",
                                         readiness="running", generation="g1"),
                     lambda: auth.transition(binding.incarnation, 1, status="stopped", readiness="unknown"),
                     lambda: auth.delete(binding.incarnation, 1)]
            for call in calls:
                with pytest.raises(inbox.Refused, match="transaction_conflict"):
                    call()
                assert dict(getattr(sessions._TX, "conns", {})) == mapping
                assert caller.in_transaction
                assert statements == []
        caller.execute("INSERT INTO meta VALUES ('caller-after', 'ok')")
    assert rows(root, "SELECT key FROM meta WHERE key LIKE 'caller-%' ORDER BY key") == [
        ("caller-after",), ("caller-before",)]
    assert state(auth.store.root, "held") == [("queued", None, 0, 0)]


@pytest.mark.parametrize("defect", ["normal", "foreign", "not_in_transaction"])
def test_owned_label_cannot_substitute_for_physical_connection_properties(tmp_path, monkeypatch, defect):
    auth, _, call = prepared(tmp_path / "owned", "admit")
    before, real = snapshot(auth.store.root), lodge_db.write

    @contextmanager
    def wrong(root, **kwargs):
        if defect == "not_in_transaction":
            with closing(lodge_db.connect(root, durable=True)) as conn:
                yield conn
        else:
            with real(tmp_path / "foreign" if defect == "foreign" else root,
                      durable=defect != "normal") as conn:
                yield conn

    monkeypatch.setattr(lodge_db, "write", wrong)
    with pytest.raises(inbox.Unavailable, match="store_failed"):
        call()
    assert snapshot(auth.store.root) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("status, readiness, reason", [("resting", "resting", "target_resting"),
                                                       ("stopping", "unknown", "target_stopping"),
                                                       ("running", "unknown", "target_unknown")])
async def test_lifecycle_hold_is_committed_visible_and_waiter_observes_it(tmp_path, status, readiness, reason):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k", "session-name", "s1", "body")
    async def forbidden(attempt):
        pytest.fail("held recipient reached adapter")
    coordinator = inbox_actor.Coordinator(auth.store, forbidden, generation="g1", overdue_after=60, authority=auth)
    try:
        waiter = asyncio.create_task(coordinator.wait("k", 1))
        await asyncio.sleep(0)
        coordinator.transition(binding.incarnation, 1, status=status, readiness=readiness)
        result = await waiter
        assert (result.state, result.reason) == ("blocked", reason)
        assert state(tmp_path, "k") == [("blocked", reason, 0, 0)]
        assert rows(tmp_path, "SELECT state, reason FROM inbox_events ORDER BY cursor DESC LIMIT 1") == [("blocked", reason)]
    finally:
        await coordinator.stop()


@pytest.mark.parametrize("outcome", [inbox.Outcome.UNKNOWN, inbox.Outcome.USER_STOP,
                                    inbox.Outcome.DEFERRED, inbox.Outcome.NOT_ADMITTED,
                                    inbox.Outcome.ACCEPTED])
def test_lifecycle_release_preserves_stronger_facts_and_body_custody(tmp_path, outcome):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k", "session-name", "s1", "body")
    for _ in range(inbox.MAX_CALLS if outcome is inbox.Outcome.NOT_ADMITTED else 1):
        auth.store.record(auth.store.claim(binding.incarnation, binding.conversation, "g1"), outcome)
    before = snapshot(tmp_path)
    for revision, status, readiness in [(1, "resting", "resting"), (2, "running", "running"),
                                       (3, "stopped", "unknown")]:
        auth.transition(binding.incarnation, revision, status=status, readiness=readiness)
        after = snapshot(tmp_path)
        for table in TABLES[2:]:
            assert after[table] == before[table], (outcome, table)
    assert auth.store.claim(binding.incarnation, binding.conversation, "g1") is None


@pytest.mark.asyncio
async def test_pending_fake_adapter_holds_no_database_transaction(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    entered, reply = asyncio.Event(), asyncio.get_running_loop().create_future()
    async def adapter(attempt):
        entered.set()
        return await reply
    coordinator = inbox_actor.Coordinator(auth.store, adapter, generation="g1", overdue_after=60, authority=auth)
    await coordinator.start()
    try:
        coordinator.bound_admit("k", "session-name", "s1", "body")
        await asyncio.wait_for(entered.wait(), 2)
        assert not lodge_db.in_write() and not getattr(sessions._TX, "conns", {})
        with closing(sqlite3.connect(lodge_db.path(tmp_path), timeout=0, isolation_level=None)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO meta VALUES ('during-await', 'ok')")
            conn.execute("COMMIT")
        reply.set_result(inbox.Outcome.ACCEPTED)
        assert (await coordinator.wait("k", 2)).state == "native_accepted"
    finally:
        await coordinator.stop()
