"""Shared helpers for the inbox tests: limits, raw reads, injected faults, a fake engine."""

import asyncio
import json
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))

import inbox
import lodge_db

ROOM = dict(pending=10, pending_bytes=10_000, recipient_pending=10, recipient_bytes=10_000,
            keys=20, attempts=60, key_chars=40, name_chars=20, events=50, page=10,
            waiters=4, work=4, busy_ms=5000)


def limits(**over):
    return inbox.Limits(**{**ROOM, **over})


def rows(root, sql, *args):
    """Read through a connection of our own, never the store's."""
    with closing(sqlite3.connect(lodge_db.path(root))) as conn:
        return conn.execute(sql, args).fetchall()


def state(root, key):
    return rows(root, "SELECT state, reason, calls, body IS NULL FROM inbox_messages WHERE key=?", key)


def usage(root):
    return {r[0]: r[1:] for r in rows(root, "SELECT scope, pending, bytes, keys FROM inbox_usage")}


def accept(store, recipient="pal", conversation="c1", generation="g1"):
    attempt = store.claim(recipient, conversation, generation)
    return attempt, store.record(attempt, inbox.Outcome.ACCEPTED)


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


class FakeEngine:
    """An adapter whose every call waits on a future the test resolves. An
    accepted call writes the effect to a ledger file first: the test's
    independent record of what reached the 'engine'."""

    def __init__(self, ledger: Path):
        self.ledger = ledger
        self.calls = []      # (attempt, future)
        self.called = asyncio.Event()

    async def __call__(self, attempt):
        future = asyncio.get_running_loop().create_future()
        self.calls.append((attempt, future))
        self.called.set()
        result = await future
        if isinstance(result, BaseException):
            raise result
        if result is inbox.Outcome.ACCEPTED:
            with open(self.ledger, "a") as f:
                f.write(json.dumps([attempt.key, attempt.id, attempt.generation]) + "\n")
        return result

    def effects(self):
        return self.ledger.read_text().splitlines() if self.ledger.exists() else []

    async def wait_calls(self, n):
        """Wait until n calls have been made; returns the newest."""
        while len(self.calls) < n:
            self.called.clear()
            await asyncio.wait_for(self.called.wait(), 5)
        return self.calls[-1][0]

    def resolve(self, result, index=-1):
        self.calls[index][1].set_result(result)
