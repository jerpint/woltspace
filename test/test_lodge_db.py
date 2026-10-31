"""lodge_db: the one SQLite file that holds the lodge's changing state."""

import os
import sqlite3
import stat
from contextlib import closing, contextmanager
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))

import lodge_db


def test_path_is_under_space(tmp_path):
    assert lodge_db.path(tmp_path) == tmp_path / ".space" / "lodge.db"


def test_connect_creates_the_file_and_sets_the_pragmas(tmp_path):
    conn = lodge_db.connect(tmp_path)
    try:
        assert lodge_db.path(tmp_path).is_file()
        pragma = lambda name: conn.execute(f"PRAGMA {name}").fetchone()[0]
        assert pragma("journal_mode") == "wal"
        assert pragma("synchronous") == 1  # NORMAL
        assert pragma("busy_timeout") == 5000
        assert pragma("foreign_keys") == 1
        assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
    finally:
        conn.close()


def test_a_durable_writer_is_full_from_before_its_schema_and_begin(tmp_path, monkeypatch):
    seen = []
    ensure = lodge_db.ensure_schema
    def spy(conn):
        seen.append((conn.in_transaction, conn.execute("PRAGMA synchronous").fetchone()[0]))
        ensure(conn)
    monkeypatch.setattr(lodge_db, "ensure_schema", spy)
    with lodge_db.write(tmp_path, durable=True) as conn:
        seen.append((None, conn.execute("PRAGMA synchronous").fetchone()[0]))
    with lodge_db.write(tmp_path) as conn:
        seen.append((None, conn.execute("PRAGMA synchronous").fetchone()[0]))
    assert seen == [(False, 2), (None, 2), (False, 1), (None, 1)]  # FULL, then NORMAL


def test_schema_is_created_once_and_each_migration_recorded(tmp_path):
    for _ in range(2):
        conn = lodge_db.connect(tmp_path)
        tables = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        applied = lodge_db.applied_migrations(conn)
        conn.close()
        assert {"meta", "sessions"} <= tables
        assert applied == [v for v, _ in lodge_db._MIGRATIONS]


def test_a_migration_that_lands_late_still_runs(tmp_path, monkeypatch):
    base = lodge_db._MIGRATIONS[:1]
    two = (2, ("CREATE TABLE two (x)",))
    three = (3, ("CREATE TABLE three (x)",))
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", base + [three])
    lodge_db.connect(tmp_path).close()
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", base + [two, three])
    conn = lodge_db.connect(tmp_path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"two", "three"} <= tables
    assert lodge_db.applied_migrations(conn) == [1, 2, 3]
    conn.close()


def test_migration_6_adds_empty_inbox_binding_columns_and_keeps_every_row(tmp_path, monkeypatch):
    known = lodge_db._MIGRATIONS
    six = dict(known)[6]
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", [m for m in known if m[0] != 6])
    with lodge_db.write(tmp_path) as conn:
        conn.execute("INSERT INTO sessions(name, wolt, status, created_at, record, updated_at)"
                     " VALUES ('s1', 'pal', 'running', 1, '{}', 1)")
        conn.execute("INSERT INTO inbox_messages(key, recipient, conversation, body, size, digest,"
                     " state, created_at) VALUES ('k1', 'pal', 'c1', x'00', 1, x'00', 'queued', 1)")
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", [m for m in known if m[0] != 6] + [(6, six + ("nonsense",))])
    with pytest.raises(sqlite3.OperationalError):
        lodge_db.connect(tmp_path)  # a failing migration leaves nothing of itself
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", known)
    for _ in range(2):
        with closing(lodge_db.connect(tmp_path)) as conn:
            assert 6 in lodge_db.applied_migrations(conn)
            assert [tuple(r) for r in conn.execute(
                "SELECT name, status, inbox_incarnation, inbox_revision FROM sessions")] == [
                ("s1", "running", None, None)]
            assert [tuple(r) for r in conn.execute("SELECT key, state, request_kind FROM inbox_messages")
                    ] == [("k1", "queued", None)]
            assert conn.execute("SELECT value FROM meta WHERE key='min_schema'").fetchone()[0] == "0"


def _newer_build(monkeypatch, tmp_path, extra, breaking):
    """Migrate the file as a newer build would, then come back as this one."""
    known, known_breaking = lodge_db._MIGRATIONS, lodge_db._BREAKING
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", known + extra)
    monkeypatch.setattr(lodge_db, "_BREAKING", known_breaking | breaking)
    lodge_db.connect(tmp_path).close()
    monkeypatch.setattr(lodge_db, "_MIGRATIONS", known)
    monkeypatch.setattr(lodge_db, "_BREAKING", known_breaking)


@pytest.mark.parametrize("readonly", [False, True])
def test_an_additive_newer_migration_still_opens_with_older_code(tmp_path, monkeypatch, readonly):
    newest = max(v for v, _ in lodge_db._MIGRATIONS)
    _newer_build(monkeypatch, tmp_path, [(newest + 1, ("CREATE TABLE later (x)",))], set())
    with closing(lodge_db.connect(tmp_path, readonly=readonly)) as conn:
        assert lodge_db.applied_migrations(conn)[-1] == newest + 1
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["min_schema"] == "0"
    assert meta["written_by"] == lodge_db._build()


@pytest.mark.parametrize("readonly", [False, True])
def test_a_breaking_newer_migration_makes_older_code_refuse(tmp_path, monkeypatch, readonly):
    newest = max(v for v, _ in lodge_db._MIGRATIONS)
    # The newer build skipped a migration this one would add: refusing must
    # also mean not migrating.
    monkeypatch.setattr(lodge_db, "_build", lambda: "9.9.9")
    _newer_build(monkeypatch, tmp_path, [(newest + 2, ("CREATE TABLE later (x)",))], {newest + 2})
    monkeypatch.setattr(lodge_db, "_MIGRATIONS",
                        lodge_db._MIGRATIONS + [(newest + 1, ("CREATE TABLE mine (x)",))])
    db = lodge_db.path(tmp_path)
    before = db.read_bytes()
    with pytest.raises(lodge_db.SchemaTooNew) as refused:
        lodge_db.connect(tmp_path, readonly=readonly)
    assert str(refused.value) == (
        f"lodge.db needs schema >= {newest + 2} (written by 9.9.9); "
        f"this build knows up to {newest + 1}. Install that build or newer.")
    assert isinstance(refused.value, sqlite3.DatabaseError)
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name='mine'").fetchone() == (0,)


def test_the_floor_never_goes_down(tmp_path, monkeypatch):
    newest = max(v for v, _ in lodge_db._MIGRATIONS)
    _newer_build(monkeypatch, tmp_path, [(newest + 1, ("CREATE TABLE later (x)",))], {newest + 1})
    _newer_build(monkeypatch, tmp_path, [(newest + 1, ("CREATE TABLE later (x)",)),
                                         (newest + 2, ("CREATE TABLE last (x)",))], set())
    with closing(sqlite3.connect(lodge_db.path(tmp_path))) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='min_schema'").fetchone() == (
            str(newest + 1),)


def test_readonly_refuses_writes_and_a_missing_file(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        lodge_db.connect(tmp_path, readonly=True)
    assert not lodge_db.path(tmp_path).exists()
    lodge_db.connect(tmp_path).close()
    conn = lodge_db.connect(tmp_path, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('k', 'v')")
    finally:
        conn.close()


def test_write_commits_or_rolls_back(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        conn.execute("INSERT INTO meta VALUES ('kept', '1')")
    with pytest.raises(RuntimeError):
        with lodge_db.write(tmp_path) as conn:
            conn.execute("INSERT INTO meta VALUES ('dropped', '1')")
            raise RuntimeError("boom")
    conn = lodge_db.connect(tmp_path)
    keys = {r[0] for r in conn.execute("SELECT key FROM meta")}
    conn.close()
    assert "kept" in keys and "dropped" not in keys


def test_write_serializes_read_modify_write_across_threads(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        conn.execute("INSERT INTO meta VALUES ('n', '0')")

    def bump():
        for _ in range(25):
            with lodge_db.write(tmp_path) as conn:
                n = int(conn.execute("SELECT value FROM meta WHERE key='n'").fetchone()[0])
                conn.execute("UPDATE meta SET value=? WHERE key='n'", (str(n + 1),))

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    conn = lodge_db.connect(tmp_path)
    assert conn.execute("SELECT value FROM meta WHERE key='n'").fetchone()[0] == "100"
    conn.close()


def test_a_new_file_is_owner_only_whatever_the_umask(tmp_path):
    old = os.umask(0o022)
    try:
        lodge_db.connect(tmp_path).close()
    finally:
        os.umask(old)
    assert stat.S_IMODE(lodge_db.path(tmp_path).stat().st_mode) == 0o600


def _importer(calls, *retire):
    """One meta row per retired path that is there, OR IGNORE like a real importer."""
    def run(conn, wolts_dir):
        calls.append(1)
        live = [p for p in retire if p.exists()]
        for path in live:
            conn.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (
                f"row:{path.name}", path.read_text() if path.is_file() else "dir"))
        return {"imported": len(live)}, live
    return run


def test_files_are_the_gate_and_rows_in_the_db_win(tmp_path):
    source = tmp_path / "a.json"
    source.write_text("old")
    calls = []
    assert lodge_db.run_imports(tmp_path, {"a": _importer(calls, source)}) == {"a": {"imported": 1}}
    # Nothing left: the importer runs, finds nothing, and nothing is recorded.
    assert lodge_db.run_imports(tmp_path, {"a": _importer(calls, source)}) == {}
    # An older release wrote the file again: imported, the row already there kept,
    # and the file retired beside the first one under a free name.
    for n in (2, 3):
        source.write_text(f"stale {n}")
        assert lodge_db.run_imports(tmp_path, {"a": _importer(calls, source)}) == {"a": {"imported": 1}}
        assert (tmp_path / f"a.json.imported.{n}").read_text() == f"stale {n}"
    assert len(calls) == 4 and (tmp_path / "a.json.imported").read_text() == "old"
    conn = lodge_db.connect(tmp_path)
    assert conn.execute("SELECT value FROM meta WHERE key='row:a.json'").fetchone()[0] == "old"
    assert lodge_db.imports(conn)["a"]["imported"] == 1
    conn.close()


def test_sources_are_retired_only_after_the_commit(tmp_path, monkeypatch):
    folder = tmp_path / "sessions"
    folder.mkdir()
    (folder / "s.json").write_text("{}")
    (folder / "s.lock").write_text("")
    single = tmp_path / "grants.json"
    single.write_text("{}")
    (tmp_path / "grants.lock").write_text("")
    lone_lock = tmp_path / "recovery.lock"
    lone_lock.write_text("")
    real = lodge_db.write

    @contextmanager
    def commit_fails(wolts_dir):
        with real(wolts_dir) as conn:
            yield conn
            raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(lodge_db, "write", commit_fails)
    with pytest.raises(sqlite3.OperationalError):
        lodge_db.run_imports(tmp_path, {"a": _importer([], folder, single, lone_lock)})
    assert (folder / "s.lock").exists() and single.exists() and lone_lock.exists()
    monkeypatch.setattr(lodge_db, "write", real)
    lodge_db.run_imports(tmp_path, {"a": _importer([], folder, single, lone_lock)})
    assert sorted(p.name for p in tmp_path.iterdir() if p.name != ".space") == [
        "grants.json.imported", "sessions.imported"]
    assert [p.name for p in (tmp_path / "sessions.imported").iterdir()] == ["s.json"]


def test_a_retire_that_fails_is_logged_and_the_boot_goes_on(tmp_path, monkeypatch, capsys):
    source = tmp_path / "a.json"
    source.write_text("x")

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(lodge_db, "_retire", denied)
    assert lodge_db.run_imports(tmp_path, {"a": _importer([], source)}) == {"a": {"imported": 1}}
    assert "could not retire" in capsys.readouterr().err


def test_a_failed_import_writes_nothing_and_runs_again(tmp_path):
    def broken(conn, wolts_dir):
        conn.execute("INSERT INTO meta VALUES ('half', 'x')")
        raise OSError("disk")

    with pytest.raises(OSError):  # a broken importer is a bug: loud, and retried next boot
        lodge_db.run_imports(tmp_path, {"a": broken})
    conn = lodge_db.connect(tmp_path)
    assert lodge_db.imports(conn) == {}
    assert conn.execute("SELECT 1 FROM meta WHERE key='half'").fetchone() is None
    conn.close()


def test_many_first_openers_at_once_all_get_in(tmp_path):
    errors = []

    def open_and_write(root):
        try:
            with lodge_db.write(root) as conn:
                conn.execute("SELECT 1")
        except sqlite3.Error as exc:
            errors.append(exc)

    for n in range(30):
        root = tmp_path / str(n)
        threads = [threading.Thread(target=open_and_write, args=(root,)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    assert errors == []
