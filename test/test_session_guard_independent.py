"""Independent supplied-connection and ambiguous-commit oracles; dummy stores only."""
import json
import sqlite3
import traceback
from contextlib import closing

import pytest

from inbox_support import authority, enroll, rows
import lodge_db
import sessions

MARKER = "NEUTRAL_" + "STORAGE_DETAIL"
TABLES = ("sessions", "inbox_messages", "inbox_attempts", "inbox_usage", "inbox_events", "meta")


def facts(root):
    return {table: rows(root, f"SELECT * FROM {table} ORDER BY 1") for table in TABLES}


def prepared(root):
    auth = authority(root)
    enroll(auth, "s1")
    auth.admit("k", "session-name", "s1", "dummy-body")
    record = json.loads(rows(root, "SELECT record FROM sessions WHERE name='s1'")[0][0])
    return sessions.SessionRegistry(wolts_dir=root), {**record, "title": "new-title"}


class Fault:
    def __init__(self, conn, phase):
        self.conn, self.phase = conn, phase

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, *args):
        matches = {"lock": sql.startswith("UPDATE sessions SET name=name"),
                   "read": sql.startswith("SELECT * FROM sessions"),
                   "write": sql.startswith("INSERT INTO sessions"),
                   "begin": sql == "BEGIN IMMEDIATE", "before": sql == "COMMIT"}
        if matches.get(self.phase) or self.phase == "os" and matches["read"]:
            raise (OSError if self.phase == "os" else sqlite3.OperationalError)(MARKER)
        result = self.conn.execute(sql, *args)
        if self.phase == "after" and sql == "COMMIT":
            raise sqlite3.OperationalError(MARKER)  # COMMIT really happened
        return result


def safe(error):
    assert MARKER not in "".join(traceback.format_exception(error))
    assert error.__suppress_context__


@pytest.mark.parametrize("phase", ["lock", "read", "write", "os"])
def test_direct_put_faults_are_fixed_and_caught_refusal_preserves_caller(tmp_path, phase, caplog):
    _, record = prepared(tmp_path)
    before = facts(tmp_path)
    with lodge_db.write(tmp_path) as conn:
        conn.execute("INSERT INTO meta VALUES ('caller-before','yes')")
        with pytest.raises(sessions.SessionStoreUnavailable) as caught:
            sessions.put_session(Fault(conn, phase), record)
        safe(caught.value)
        assert conn.in_transaction and conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        conn.execute("INSERT INTO meta VALUES ('caller-after','yes')")
    after = facts(tmp_path)
    assert {k: v for k, v in after.items() if k != "meta"} == {k: v for k, v in before.items() if k != "meta"}
    assert rows(tmp_path, "SELECT key FROM meta WHERE key LIKE 'caller-%' ORDER BY key") == [
        ("caller-after",), ("caller-before",)]
    assert MARKER not in caplog.text


def test_autocommit_is_refused_before_sql_despite_foreign_ambient_write(tmp_path):
    _, record = prepared(tmp_path / "owned")
    with closing(lodge_db.connect(tmp_path / "owned")) as caller, lodge_db.write(tmp_path / "foreign"):
        statements = []
        caller.set_trace_callback(statements.append)
        mapping = dict(getattr(sessions._TX, "conns", {}))
        with pytest.raises(sessions.SessionWriteRefused, match="transaction_required"):
            sessions.put_session(caller, record)
        assert statements == [] and not caller.in_transaction
        assert dict(getattr(sessions._TX, "conns", {})) == mapping


@pytest.mark.parametrize("kind", ["readonly", "stale"])
def test_real_readonly_or_stale_wal_upgrade_is_unavailable_and_preserves_snapshot(tmp_path, kind):
    _, record = prepared(tmp_path)
    with closing(lodge_db.connect(tmp_path, readonly=kind == "readonly", busy_ms=1)) as conn:
        conn.execute("BEGIN")
        conn.execute("SELECT name FROM sessions").fetchall()  # establish the WAL read snapshot
        if kind == "stale":
            with lodge_db.write(tmp_path) as other:
                other.execute("INSERT INTO meta VALUES ('other-writer', 'committed')")
        before = facts(tmp_path)
        with pytest.raises(sessions.SessionStoreUnavailable) as caught:
            sessions.put_session(conn, record)
        assert caught.value.code in ("store_busy", "store_failed")
        assert conn.in_transaction
        conn.execute("ROLLBACK")
    assert facts(tmp_path) == before


def test_zero_row_lock_changes_no_row_or_trigger_but_excludes_another_writer(tmp_path):
    _, record = prepared(tmp_path)
    with closing(lodge_db.connect(tmp_path)) as caller:
        caller.execute("CREATE TABLE audit (name TEXT)")
        caller.execute("CREATE TRIGGER audit_update AFTER UPDATE ON sessions BEGIN INSERT INTO audit VALUES(new.name); END")
        caller.execute("BEGIN")
        count = caller.total_changes
        assert sessions._locked_row(caller, "missing") is None
        assert caller.total_changes == count and caller.execute("SELECT * FROM audit").fetchall() == []
        with closing(sqlite3.connect(lodge_db.path(tmp_path), timeout=0, isolation_level=None)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        caller.execute("ROLLBACK")


@pytest.mark.parametrize("phase", ["open", "begin", "read", "write", "before", "after"])
def test_registry_faults_and_actual_commit_confirmation_use_fresh_reader_oracle(tmp_path, monkeypatch, phase, caplog):
    reg, _ = prepared(tmp_path)
    before, connect = facts(tmp_path), lodge_db.connect
    def broken(*args, **kwargs):
        if phase == "open":
            raise OSError(MARKER)
        return Fault(connect(*args, **kwargs), phase)
    with monkeypatch.context() as patch:
        patch.setattr(lodge_db, "connect", broken)
        with pytest.raises(sessions.SessionStoreUnavailable) as caught:
            reg.describe("s1", "new-title", "new-summary")
        safe(caught.value)
    after = facts(tmp_path)
    assert not lodge_db.in_write() and not getattr(sessions._TX, "conns", {})
    if phase == "after":
        assert after != before
        assert reg.get("s1", check_alive=False)["title"] == "new-title"
        assert all(after[t] == before[t] for t in TABLES if t != "sessions")
    else:
        assert after == before
    assert MARKER not in caplog.text


def test_malformed_stored_conversion_has_fixed_direct_error_without_leaking_value(tmp_path):
    _, record = prepared(tmp_path)
    with lodge_db.write(tmp_path) as conn:  # TEST-ONLY unsupported older-writer corruption
        conn.execute("UPDATE sessions SET record=json_set(record,'$.created_at',?) WHERE name='s1'", (MARKER,))
    before = facts(tmp_path)
    with lodge_db.write(tmp_path) as conn:
        with pytest.raises(sessions.SessionWriteRefused, match="binding_invalid") as caught:
            sessions.put_session(conn, record)
        safe(caught.value)
    assert facts(tmp_path) == before


@pytest.mark.parametrize("direct", [False, True])
def test_benign_serialization_failure_is_fixed_on_both_write_boundaries(tmp_path, direct):
    reg, record = prepared(tmp_path)
    before = facts(tmp_path)
    with pytest.raises(sessions.SessionWriteRefused, match="binding_invalid") as caught:
        if direct:
            with lodge_db.write(tmp_path) as conn:
                sessions.put_session(conn, {**record, "title": object()})
        else:
            reg.describe("s1", object(), "summary")
    safe(caught.value)
    assert facts(tmp_path) == before


def test_invalid_utf8_stored_marker_is_unavailable_not_unbound(tmp_path):
    _, record = prepared(tmp_path)
    with lodge_db.write(tmp_path) as conn:  # TEST-ONLY invalid SQLite TEXT seed
        conn.execute("UPDATE sessions SET inbox_generation=CAST(X'80' AS TEXT) WHERE name='s1'")
    with lodge_db.write(tmp_path) as conn:
        with pytest.raises(sessions.SessionStoreUnavailable, match="store_failed") as caught:
            sessions.put_session(conn, record)
        safe(caught.value)


def test_valid_deferred_direct_put_is_only_a_statement_until_caller_commit(tmp_path):
    _, record = prepared(tmp_path)
    before = facts(tmp_path)
    with closing(sqlite3.connect(lodge_db.path(tmp_path), isolation_level=None)) as conn:
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("BEGIN")
        sessions.put_session(conn, record)  # a plain supplied connection, no ambient write flag
        assert conn.in_transaction and not lodge_db.in_write()
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert facts(tmp_path) == before  # statement return is not a commit receipt
        conn.execute("ROLLBACK")
    assert facts(tmp_path) == before


def test_direct_uniqueness_keeps_legacy_type_with_fixed_message(tmp_path):
    _, record = prepared(tmp_path)
    with lodge_db.write(tmp_path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="^session_integrity$") as caught:
            sessions.put_session(conn, {**record, "name": "collision"})
        safe(caught.value)
        assert conn.in_transaction
