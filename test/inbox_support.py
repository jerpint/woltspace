"""Shared helpers for the inbox tests: limits, raw reads, injected faults, a fake
engine, enrolled sessions, and (run as a script) one store call in a process of its own."""

import asyncio
import dataclasses
import json
import subprocess
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))

import inbox
import inbox_binding
import lodge_db
import sessions

ROOM = dict(pending=10, pending_bytes=10_000, recipient_pending=10, recipient_bytes=10_000,
            keys=20, attempts=60, key_chars=40, name_chars=20, events=50, page=10,
            waiters=4, work=4, busy_ms=5000)


def limits(**over):
    return inbox.Limits(**{**ROOM, **over})


def authority(root, enrollments=10, field_chars=40, **over):
    store = inbox.Store(root, limits(**{"name_chars": 40, **over}))
    return inbox_binding.Authority(store, inbox_binding.BindingLimits(enrollments, field_chars))


def session(root, name, wolt="pal", status="running", conversation=None, last_activity=None,
            created_at=1, harness="claude"):
    """A session row as the registry writes it (an upsert keeps the inbox columns)."""
    with lodge_db.write(root) as conn:
        sessions.put_session(conn, dict(
            name=name, wolt=wolt, status=status, created_at=created_at, last_activity=last_activity,
            harness=harness, harness_session_id=conversation or f"conv-{name}"))


def enroll(auth, name, readiness="running", generation="g1", **row):
    session(auth.store.root, name, **row)
    return auth.enroll(name, harness=row.get("harness", "claude"),
                       conversation=row.get("conversation") or f"conv-{name}",
                       readiness=readiness, generation=generation)


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


def spawn(root, op, pause=None, **over):
    """Run one Authority call, op = (method, args, kwargs), in a new process; with pause, it stops inside its
    write (the lodge's lock held) until pause.go exists."""
    return subprocess.Popen([sys.executable, __file__, str(root), json.dumps([op, over]), str(pause or "")],
                            stdout=subprocess.PIPE, text=True)


def ordered(first, second, pause, root, **over):
    """first takes the lock and holds it while second starts and waits on it."""
    a = spawn(root, first, pause, **over)
    deadline = time.monotonic() + 10
    while not Path(f"{pause}.in").exists():
        assert time.monotonic() < deadline and a.poll() is None, "first never took the lock"
        time.sleep(0.01)
    b = spawn(root, second, **over)
    time.sleep(0.3)
    assert b.poll() is None, "second did not wait for the lock"
    Path(f"{pause}.go").touch()
    return [json.loads(p.communicate(timeout=20)[0]) for p in (a, b)]


def _child(root, spec, pause):
    (name, args, kwargs), over = json.loads(spec)
    real = inbox.Store._write

    @contextmanager
    def write(store):
        with real(store) as conn:
            if pause:
                Path(f"{pause}.in").touch()
                deadline = time.monotonic() + 10
                while not Path(f"{pause}.go").exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
            yield conn

    inbox.Store._write = write
    auth = authority(root, **over)
    try:
        result = getattr(auth, name)(*args, **kwargs)
        print(json.dumps({"ok": result}, default=dataclasses.asdict))
    except inbox.Refused as exc:
        print(json.dumps({"refused": exc.code}))
    except inbox.InboxError as exc:
        print(json.dumps({"error": type(exc).__name__}))


if __name__ == "__main__":
    _child(*sys.argv[1:])
