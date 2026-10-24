"""
The lodge's changing state, in one SQLite file: <wolts_dir>/.space/lodge.db.

Every lodge process on the machine (control plane, session-reg, connectors)
opens the same file. WAL lets readers run while one writer writes, and
busy_timeout makes a second writer wait instead of failing, so no code of ours
takes a lock: a read-modify-write goes inside `write()`, whose BEGIN IMMEDIATE
holds SQLite's write lock until it commits.

Text that agents and humans read (memory, settings, logs, chat history) stays
in files; only rows that change live here.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

# Each entry runs once per file, and meta records it as migration:<id>.
# Every missing id runs, in id order, so PRs that each add one can land in
# any order. Add an entry; never edit a shipped one. An entry is additive by
# default: older code that does not know it still reads and writes the file.
# One that is not (a dropped or renamed column, a changed meaning) also goes
# in _BREAKING, and the file then refuses every build that does not know it.
_MIGRATIONS: list[tuple[int, tuple[str, ...]]] = [
    (1, (
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        # Six columns the lodge queries on; the rest of the session record
        # stays JSON in `record`, so a new field needs no migration.
        """CREATE TABLE IF NOT EXISTS sessions (
            name               TEXT PRIMARY KEY,
            wolt               TEXT NOT NULL,
            status             TEXT NOT NULL,
            created_at         INTEGER NOT NULL,
            last_activity      INTEGER,
            finished_at        INTEGER,
            harness            TEXT,
            harness_session_id TEXT UNIQUE,
            record             TEXT NOT NULL,
            updated_at         INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS sessions_wolt_status"
        " ON sessions(wolt, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS sessions_status ON sessions(status)",
    )),
]
_BREAKING: set[int] = set()


class SchemaTooNew(sqlite3.DatabaseError):
    """lodge.db holds a breaking migration this build does not know."""


def path(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "lodge.db"


def connect(wolts_dir, *, readonly: bool = False) -> sqlite3.Connection:
    """A new connection, never shared across threads. Readonly (mode=ro) fails
    on a missing file, so a reader never creates an empty lodge."""
    db = path(wolts_dir)
    if readonly:
        conn = sqlite3.connect(
            f"{db.resolve().as_uri()}?mode=ro", uri=True, isolation_level=None
        )
    else:
        db.parent.mkdir(parents=True, exist_ok=True)
        # Session prompts live here: owner-only from birth (SQLite gives
        # -wal and -shm the main file's mode). An existing file keeps its own.
        os.close(os.open(db, os.O_CREAT | os.O_WRONLY, 0o600))
        conn = sqlite3.connect(db, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            _wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if readonly:
            # mode=ro opens lazily: touch the file now so a missing one
            # fails here, not at the caller's first query.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            _refuse_newer(conn)
        else:
            ensure_schema(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _wal(conn: sqlite3.Connection) -> None:
    """Switch a new file to WAL. The mode is stored in the file, so only its
    first openers set it, and busy_timeout does not cover the switch: several
    processes opening a new file at once retry it here."""
    for _ in range(100):
        if conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal":
            return
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc):
                raise
            time.sleep(0.05)
    conn.execute("PRAGMA journal_mode=WAL")


def _applied(conn: sqlite3.Connection) -> set[int]:
    try:
        rows = conn.execute("SELECT key FROM meta WHERE key LIKE 'migration:%'")
        return {int(row[0].split(":", 1)[1]) for row in rows}
    except sqlite3.OperationalError:  # no meta table: a new file
        return set()


def applied_migrations(conn: sqlite3.Connection) -> list[int]:
    return sorted(_applied(conn))


def _build() -> str:
    try:
        return importlib.metadata.version("woltspace")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _refuse_newer(conn: sqlite3.Connection) -> int:
    """The file's floor (meta.min_schema), or SchemaTooNew when this build is below it."""
    try:
        meta = dict(conn.execute(
            "SELECT key, value FROM meta WHERE key IN ('min_schema', 'written_by')"))
    except sqlite3.OperationalError:  # no meta table: a new file
        return 0
    floor, known = int(meta.get("min_schema", 0)), max(v for v, _ in _MIGRATIONS)
    if floor > known:
        raise SchemaTooNew(
            f"lodge.db needs schema >= {floor} (written by {meta.get('written_by', 'unknown')}); "
            f"this build knows up to {known}. Install that build or newer.")
    return floor


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Apply every migration meta has no record of, in one transaction, and
    record the floor and this build. Writes nothing to a file above this build."""
    _refuse_newer(conn)
    if {version for version, _ in _MIGRATIONS} <= _applied(conn):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = _applied(conn)  # another process may have won
        floor = _refuse_newer(conn)
        for version, statements in sorted(_MIGRATIONS):
            if version in done:
                continue
            for statement in statements:
                conn.execute(statement)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, strftime('%s','now'))",
                (f"migration:{version}",),
            )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('min_schema', ?), ('written_by', ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(max([floor, *_BREAKING])), _build()),
        )
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:  # SQLite may have rolled back already (FULL, IOERR)
            conn.execute("ROLLBACK")
        raise


_STATE = threading.local()


@contextmanager
def write(wolts_dir):
    """One write transaction: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on error.

    BEGIN IMMEDIATE takes the write lock up front, so what the block reads
    cannot change before it writes. This is the lodge's only lock.
    """
    conn = connect(wolts_dir)
    try:
        conn.execute("BEGIN IMMEDIATE")
        outer = in_write()
        _STATE.writing = True
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:  # SQLite may have rolled back already (FULL, IOERR)
                conn.execute("ROLLBACK")
            raise
        finally:
            _STATE.writing = outer
        conn.execute("COMMIT")
    finally:
        conn.close()


def in_write() -> bool:
    """True while this thread holds a write transaction.

    A write holds the lodge's one lock, so it runs SQL only: never a
    subprocess (tmux, ps), never network I/O. The tests enforce it with this.
    """
    return getattr(_STATE, "writing", False)


def run_imports(wolts_dir, importers: dict) -> dict:
    """Import whatever old state files are still there, at every boot.

    importers maps a store name to fn(conn, wolts_dir) -> (counts, retire),
    where retire lists the paths it read; an empty retire means there was
    nothing to import. Each importer must insert OR IGNORE on its natural key,
    so a row already in the DB wins over a stale file. That makes the files
    themselves the gate: a lodge downgraded and upgraded again imports what
    the old release wrote in between.

    Each store runs in its own write transaction, which also records
    imported_at:<store> = {"at", ...counts} for /health. Only after the
    commit is each retired path renamed to <name>.imported (a lock file is
    deleted, and so are the locks beside a file or inside a folder): a failed
    commit leaves the sources to import again. A rename that fails is logged
    and never stops the boot. Returns {store: counts} for the stores that ran.
    """
    ran = {}
    for store, importer in importers.items():
        with write(wolts_dir) as conn:
            counts, retire = importer(conn, wolts_dir)
            if not retire:
                continue
            conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"imported_at:{store}", json.dumps({"at": int(time.time()), **counts})),
            )
        for source in retire:
            try:
                _retire(Path(source))
            except OSError as exc:
                print(f"[lodge_db] imported {source} but could not retire it: {exc}",
                      file=sys.stderr)
        ran[store] = counts
    return ran


def _retire(source: Path) -> None:
    if source.suffix == ".lock":
        source.unlink(missing_ok=True)
        return
    if not source.exists():
        return
    target = source.with_name(source.name + ".imported")
    n = 2
    while target.exists():  # an earlier import's leftovers stay as they are
        target = source.with_name(f"{source.name}.imported.{n}")
        n += 1
    source.rename(target)
    locks = target.glob("*.lock") if target.is_dir() else [source.with_suffix(".lock")]
    for lock in locks:
        lock.unlink(missing_ok=True)


def imports(conn: sqlite3.Connection) -> dict:
    """{store: {"at": epoch, ...counts}} for every store imported so far."""
    rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'imported_at:%'")
    return {row[0].split(":", 1)[1]: json.loads(row[1]) for row in rows}
