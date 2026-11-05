"""Shared helpers for the inbox tests: limits, wolts with running sessions and
their readers, raw reads and injected faults."""

import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))

import inbox
import lodge_db
import sessions

ROOM = dict(pending=10, pending_bytes=10_000, mailbox_pending=10, mailbox_bytes=10_000,
            keys=20, key_chars=40, name_chars=20, events=50, page=10, busy_ms=5000)


def limits(**over):
    return inbox.Limits(**{**ROOM, **over})


def wolt(root, name):
    (Path(root) / name / "wolt").mkdir(parents=True, exist_ok=True)
    (Path(root) / name / "wolt" / "wolt.json").write_text("{}")


def session(root, name, wolt="pal", status="running"):
    """A session row as the registry writes it."""
    with lodge_db.write(root) as conn:
        sessions.put_session(conn, dict(name=name, wolt=wolt, status=status, created_at=1))


def reader(store, name, wolt_name="pal"):
    """A running session of the wolt and a fresh reader for it."""
    wolt(store.root, wolt_name)
    session(store.root, name, wolt_name)
    return store.grant(name, wolt_name)


def rows(root, sql, *args):
    """Read through a connection of our own, never the store's."""
    with closing(sqlite3.connect(lodge_db.path(root))) as conn:
        return conn.execute(sql, args).fetchall()


def state(root, key):
    return rows(root, "SELECT state, claim, body IS NULL FROM inbox_messages WHERE key=?", key)


def usage(root):
    return {r[0]: r[1:] for r in rows(root, "SELECT scope, pending, bytes, keys FROM inbox_usage")}


def acked(store, token):
    """Fetch the next message and acknowledge it."""
    got = store.fetch(token)
    return got, store.ack(token, got.seq, got.claim)


@contextmanager
def failing(root, table, when="INSERT"):
    """A real SQL failure inside every transaction that writes table."""
    with closing(sqlite3.connect(lodge_db.path(root))) as conn:
        conn.execute(f"CREATE TRIGGER fail BEFORE {when} ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END")
        conn.commit()
    try:
        yield
    finally:
        with closing(sqlite3.connect(lodge_db.path(root))) as conn:
            conn.execute("DROP TRIGGER fail")
            conn.commit()


def lose_confirmation(monkeypatch, nth):
    """The nth write commits for real; its caller then sees an error instead."""
    real, count = lodge_db.write, [0]

    @contextmanager
    def write(root, **kw):
        count[0] += 1
        with real(root, **kw) as conn:
            yield conn
        if count[0] == nth:
            raise sqlite3.OperationalError("confirmation lost")

    monkeypatch.setattr(lodge_db, "write", write)
