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
    (2, (
        # The owner's consents. scope: the approved directory for kind 'auto',
        # the dig's name for kind 'dig' (whose `record` holds the rest).
        """CREATE TABLE IF NOT EXISTS grants (
            kind           TEXT NOT NULL,
            wolt_id        TEXT NOT NULL,
            scope          TEXT NOT NULL DEFAULT '',
            policy_version INTEGER NOT NULL DEFAULT 0,
            approved_at    INTEGER NOT NULL,
            record         TEXT,
            PRIMARY KEY (kind, wolt_id, scope)
        )""",
        # A dig's name is one dig for the whole lodge, whichever wolt holds it.
        "CREATE UNIQUE INDEX IF NOT EXISTS grants_dig_name ON grants(scope) WHERE kind='dig'",
        # The wolf's journal, one row per event. A row with action 'fired'
        # claims `slot` (a minute) for its cron: the newest one is "last run".
        """CREATE TABLE IF NOT EXISTS wolf_runs (
            id      INTEGER PRIMARY KEY,
            wolt    TEXT NOT NULL,
            cron    TEXT NOT NULL,
            slot    TEXT,
            action  TEXT NOT NULL,
            session TEXT,
            ts      REAL NOT NULL,
            extra   TEXT
        )""",
        # Unique so a re-import of a line or stamp already taken is skipped.
        # The key holds a REAL ts: importers copy it bit for bit, and nothing
        # may round or normalize it, or the same event goes in twice.
        "CREATE UNIQUE INDEX IF NOT EXISTS wolf_runs_key ON wolf_runs(wolt, cron, action, ts)",
    )),
    (3, (
        # Which wolt and session a chat talks to. Telegram keys by chat id;
        # Slack by thread ("channel:root_ts") or, for a wolt choice, by the
        # owner's user id. The two never collide: a user id has no colon.
        """CREATE TABLE IF NOT EXISTS chat_state (
            adapter        TEXT NOT NULL,
            chat_id        TEXT NOT NULL,
            active_wolt    TEXT,
            active_session TEXT,
            record         TEXT,
            updated_at     INTEGER NOT NULL,
            PRIMARY KEY (adapter, chat_id)
        )""",
        # Slack messages waiting for the owner to pick a wolt. `text` is
        # cleared once claimed, so a message is delivered at most once.
        """CREATE TABLE IF NOT EXISTS pending_messages (
            id         TEXT PRIMARY KEY,
            adapter    TEXT NOT NULL,
            user       TEXT NOT NULL,
            channel    TEXT NOT NULL,
            root_ts    TEXT NOT NULL,
            picker_ts  TEXT NOT NULL DEFAULT '',
            event_id   TEXT,
            text       TEXT,
            state      TEXT NOT NULL,
            wolt       TEXT,
            session    TEXT,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS pending_user"
        " ON pending_messages(adapter, user, state, expires_at)",
        # Apps the owner wants running; `record` is the whole state dict.
        """CREATE TABLE IF NOT EXISTS apps_running (
            name       TEXT PRIMARY KEY,
            pid        INTEGER,
            port       INTEGER,
            tunnel_pid INTEGER,
            tunnel_url TEXT,
            keeper     TEXT,
            started_at INTEGER NOT NULL,
            record     TEXT NOT NULL
        )""",
    )),
    (4, (
        # Events a connector handed in, by its own id: a resend after a
        # reconnect is answered "duplicate" and not dispatched again. Pruned
        # after a week (connector_events.prune).
        """CREATE TABLE IF NOT EXISTS connector_events (
            connector TEXT NOT NULL,
            event_id  TEXT NOT NULL,
            ts        REAL NOT NULL,
            PRIMARY KEY (connector, event_id)
        )""",
        "CREATE INDEX IF NOT EXISTS connector_events_ts ON connector_events(ts)",
    )),
    (5, (
        # The inbox (inbox.py, RFC #615): mail held until the recipient's
        # engine takes it. Nothing in the lodge writes it yet. A row is never
        # deleted, so seq and key are never reused; body goes once accepted,
        # while size and the SHA-256 of its UTF-8 bytes stay to match retries.
        """CREATE TABLE IF NOT EXISTS inbox_messages (
            seq          INTEGER PRIMARY KEY AUTOINCREMENT,
            key          TEXT NOT NULL UNIQUE,
            recipient    TEXT NOT NULL,
            conversation TEXT NOT NULL,
            body         BLOB,
            size         INTEGER NOT NULL,
            digest       BLOB NOT NULL,
            state        TEXT NOT NULL,
            reason       TEXT,
            calls        INTEGER NOT NULL DEFAULT 0,
            created_at   REAL NOT NULL
        )""",
        # Each conversation's oldest unresolved message, without reading the
        # accepted ones; queries must say `state != 'native_accepted'` to use it.
        "CREATE INDEX IF NOT EXISTS inbox_unresolved"
        " ON inbox_messages(recipient, conversation, seq) WHERE state != 'native_accepted'",
        # One row per engine call, kept for good (at most three per message).
        """CREATE TABLE IF NOT EXISTS inbox_attempts (
            id         TEXT PRIMARY KEY,
            seq        INTEGER NOT NULL REFERENCES inbox_messages(seq),
            ordinal    INTEGER NOT NULL,
            generation TEXT NOT NULL,
            outcome    TEXT,
            UNIQUE (seq, ordinal)
        )""",
        # A fixed-size ring of state changes for observers; the oldest go.
        """CREATE TABLE IF NOT EXISTS inbox_events (
            cursor  INTEGER PRIMARY KEY AUTOINCREMENT,
            seq     INTEGER NOT NULL,
            state   TEXT NOT NULL,
            reason  TEXT,
            repeats INTEGER NOT NULL DEFAULT 1
        )""",
        # Running totals, one row for the lodge ('') and one per recipient,
        # changed in the same transaction as the rows they count.
        """CREATE TABLE IF NOT EXISTS inbox_usage (
            scope   TEXT PRIMARY KEY,
            pending INTEGER NOT NULL,
            bytes   INTEGER NOT NULL,
            keys    INTEGER NOT NULL
        )""",
    )),
    (6, (
        # Who inbox mail is for (inbox_binding.py): a session enrolled by hand,
        # never by the lodge yet. The incarnation is made at enrollment and
        # dies with the row; harness and conversation are frozen there.
        # sessions.status stays the session's only lifecycle.
        "ALTER TABLE sessions ADD COLUMN inbox_incarnation TEXT",
        "ALTER TABLE sessions ADD COLUMN inbox_harness TEXT",
        "ALTER TABLE sessions ADD COLUMN inbox_conversation TEXT",
        "ALTER TABLE sessions ADD COLUMN inbox_readiness TEXT",
        "ALTER TABLE sessions ADD COLUMN inbox_generation TEXT",
        "ALTER TABLE sessions ADD COLUMN inbox_revision INTEGER",
        "CREATE UNIQUE INDEX IF NOT EXISTS sessions_inbox_incarnation"
        " ON sessions(inbox_incarnation) WHERE inbox_incarnation IS NOT NULL",
        # A wolt's pick, newest activity first; queries must order by these
        # words and say `inbox_incarnation IS NOT NULL` to use it.
        "CREATE INDEX IF NOT EXISTS sessions_inbox_pick ON sessions(wolt,"
        " coalesce(last_activity, 0) DESC, created_at DESC, name) WHERE inbox_incarnation IS NOT NULL",
        # What a bound request asked for, kept to match retries after the
        # body is gone. NULL request_kind: a row of the direct Store API.
        "ALTER TABLE inbox_messages ADD COLUMN request_kind TEXT",
        "ALTER TABLE inbox_messages ADD COLUMN request_address TEXT",
        "ALTER TABLE inbox_messages ADD COLUMN bound_harness TEXT",
        # The binding's revision a bound attempt was claimed under.
        "ALTER TABLE inbox_attempts ADD COLUMN revision INTEGER",
    )),
]
_BREAKING: set[int] = set()


class SchemaTooNew(sqlite3.DatabaseError):
    """lodge.db holds a breaking migration this build does not know."""


def path(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "lodge.db"


def connect(wolts_dir, *, readonly: bool = False, durable: bool = False,
            busy_ms: int = 5000) -> sqlite3.Connection:
    """A new connection, never shared across threads. Readonly (mode=ro) fails
    on a missing file, so a reader never creates an empty lodge. Durable syncs
    every commit to disk (synchronous=FULL), from before the schema is touched:
    for mail the lodge holds for someone (inbox.py). busy_ms bounds the wait
    for another writer."""
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
        conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
        if not readonly:
            _wal(conn)
        conn.execute(f"PRAGMA synchronous={'FULL' if durable else 'NORMAL'}")
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
def write(wolts_dir, *, durable: bool = False, busy_ms: int = 5000):
    """One write transaction: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on error.

    BEGIN IMMEDIATE takes the write lock up front, so what the block reads
    cannot change before it writes. This is the lodge's only lock.
    """
    conn = connect(wolts_dir, durable=durable, busy_ms=busy_ms)
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
