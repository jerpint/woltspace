"""
The inbox: mail the lodge holds for a wolt until its engine takes it (RFC #615).

Inactive: no route, CLI or connector admits mail here yet, and importing this
module opens nothing. Callers hand it a wolts dir and explicit Limits.

A message is queued only once its row, its usage and its event have committed
together, in a durable (synchronous=FULL) write. The caller's key makes a retry
safe: the same key with the same body and destination returns the original
message, even after the body is gone; anything else under that key is a
Conflict. A key is kept for good, so this slice refuses new keys once
Limits.keys are used up.

One actor per conversation (inbox_actor.py) claims the oldest unresolved
message, commits the attempt, calls the engine outside any transaction and
records what came back. A message that may have reached the engine is never
sent again: an unknown result, or an attempt nobody recorded, is `uncertain`
and holds its conversation.

States: queued -> attempting -> native_accepted (body gone), or back to queued
after a definite non-admission (three calls at most), or blocked (deferred,
stopped by the user, calls used up), or uncertain.
"""

from __future__ import annotations

import enum
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import lodge_db

MAX_PAYLOAD_BYTES = 32 * 1024  # of UTF-8
MAX_CALLS = 3
_MAX_INT = 2**63 - 1  # SQLite's INTEGER
_UNRESOLVED = "state != 'native_accepted'"  # word for word, or inbox_unresolved goes unused


class Outcome(str, enum.Enum):
    """What an engine adapter says happened to one call."""
    ACCEPTED = "accepted"
    NOT_ADMITTED = "definitive_non_admission"  # surely never reached the agent
    DEFERRED = "defer_without_submission"
    USER_STOP = "user_stop"
    UNKNOWN = "unknown"


class InboxError(Exception):
    pass


class Invalid(InboxError, ValueError):
    """Malformed input; nothing was written."""


class Conflict(InboxError):
    """The key holds a different body or destination."""


class Refused(InboxError):
    """A limit is reached; nothing was written. `code` names it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Stale(InboxError):
    """A result for an attempt that is not the open one."""
    code = "stale_attempt"


class Unavailable(InboxError):
    """The store could not be read or written. After a write, whether it
    committed is unknown: retry the same key, or read again later.

    Its text is a fixed code: SQLite's own message (which may quote a body
    or a path) is dropped, never chained."""

    def __init__(self, code: str):
        super().__init__(f"inbox store unavailable ({code})")
        self.code = code


def _unavailable(exc: BaseException) -> Unavailable:
    busy = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
    return Unavailable("store_busy" if busy else "store_failed")


@dataclass(frozen=True)
class Limits:
    """Every bound the inbox keeps, supplied by the caller: there are no defaults."""
    pending: int            # unresolved messages, lodge-wide
    pending_bytes: int
    recipient_pending: int
    recipient_bytes: int
    keys: int               # keys ever admitted; never released in this slice
    attempts: int           # attempt rows; MAX_CALLS reserved per key at admission
    key_chars: int
    name_chars: int         # recipient and conversation
    events: int             # ring size
    page: int               # events per read
    waiters: int            # callers waiting at once (inbox_actor)
    work: int               # conversations dispatching at once (inbox_actor)
    busy_ms: int            # how long a write or read waits for another writer

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise Invalid(f"limit {name} must be a positive int")
        if self.page > self.events:
            raise Invalid("limit page exceeds events")


@dataclass(frozen=True)
class Status:
    seq: int
    key: str
    recipient: str
    conversation: str
    state: str
    reason: str | None
    calls: int


@dataclass(frozen=True)
class Attempt:
    id: str
    seq: int
    key: str
    ordinal: int
    generation: str
    body: str


@dataclass(frozen=True)
class Event:
    cursor: int
    seq: int
    state: str
    reason: str | None
    repeats: int


@dataclass(frozen=True)
class Page:
    events: list[Event]
    next: int       # pass back as `after`
    gap: bool       # events after `after` were pruned before this read
    oldest: int     # the ring's first and last cursor (0, 0 when empty)
    newest: int


def _text(value, cap: int, what: str, shortest: int = 1) -> str:
    """A string of shortest to cap characters that encodes as UTF-8."""
    if type(value) is not str or not shortest <= len(value) <= cap:
        raise Invalid(f"{what} must be a string of {shortest} to {cap} characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise Invalid(f"{what} is not valid UTF-8") from None
    return value


def _count(value, what: str, low: int) -> int:
    if type(value) is not int or not low <= value <= _MAX_INT:
        raise Invalid(f"{what} must be an int from {low}")
    return value


def _status(row) -> Status:
    return Status(row["seq"], row["key"], row["recipient"], row["conversation"],
                  row["state"], row["reason"], row["calls"])


class Store:
    def __init__(self, wolts_dir, limits: Limits):
        self.root = wolts_dir
        self.limits = limits

    # -- public -------------------------------------------------------------

    def admit(self, key: str, recipient: str, conversation: str, body: str) -> Status:
        """Queue body for (recipient, conversation), or return the message
        already under key. The receipt is a committed snapshot, not delivery."""
        _text(key, self.limits.key_chars, "key")
        _text(recipient, self.limits.name_chars, "recipient")
        _text(conversation, self.limits.name_chars, "conversation")
        payload = _text(body, MAX_PAYLOAD_BYTES, "body", 0).encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise Invalid(f"body exceeds {MAX_PAYLOAD_BYTES} bytes of UTF-8")
        digest = hashlib.sha256(payload).digest()
        with self._write() as conn:
            row = conn.execute("SELECT * FROM inbox_messages WHERE key=?", (key,)).fetchone()
            if row is not None:
                same = (row["recipient"], row["conversation"], row["size"], row["digest"]) == (
                    recipient, conversation, len(payload), digest)
                if not same or row["body"] is not None and row["body"] != payload:
                    raise Conflict(key)
                return _status(row)
            lodge, mine = self._usage(conn, ""), self._usage(conn, recipient)
            limits = self.limits
            for code, over in (
                ("keys", lodge["keys"] >= limits.keys),
                ("attempts", (lodge["keys"] + 1) * MAX_CALLS > limits.attempts),
                ("pending", lodge["pending"] >= limits.pending),
                ("pending_bytes", lodge["bytes"] + len(payload) > limits.pending_bytes),
                ("recipient_pending", mine["pending"] >= limits.recipient_pending),
                ("recipient_bytes", mine["bytes"] + len(payload) > limits.recipient_bytes),
            ):
                if over:
                    raise Refused(code)
            seq = conn.execute(
                "INSERT INTO inbox_messages(key, recipient, conversation, body, size, digest,"
                " state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)",
                (key, recipient, conversation, payload, len(payload), digest, time.time()),
            ).lastrowid
            self._charge(conn, recipient, 1, len(payload), 1)
            self._event(conn, seq, "queued", None)
            row = conn.execute("SELECT * FROM inbox_messages WHERE seq=?", (seq,)).fetchone()
        return _status(row)

    def status(self, key: str) -> Status | None:
        """None only when a readable store has no such key."""
        with self._read() as conn:
            row = conn.execute("SELECT * FROM inbox_messages WHERE key=?", (key,)).fetchone()
        return None if row is None else _status(row)

    def events(self, after: int = 0, limit: int | None = None) -> Page:
        _count(after, "after", 0)
        limit = self.limits.page if limit is None else min(_count(limit, "limit", 1), self.limits.page)
        with self._read() as conn:
            conn.execute("BEGIN")  # one snapshot: a prune between the reads cannot fake a gap
            oldest, newest = conn.execute(
                "SELECT coalesce(min(cursor), 0), coalesce(max(cursor), 0) FROM inbox_events").fetchone()
            rows = conn.execute(
                "SELECT * FROM inbox_events WHERE cursor > ? ORDER BY cursor LIMIT ?",
                (after, limit)).fetchall()
            conn.execute("COMMIT")
        events = [Event(r["cursor"], r["seq"], r["state"], r["reason"], r["repeats"]) for r in rows]
        return Page(events, events[-1].cursor if events else after, oldest > after + 1,
                    oldest, newest)

    # -- the actor's ----------------------------------------------------------

    def claim(self, recipient: str, conversation: str, generation: str) -> Attempt | None:
        """Commit an attempt on the conversation's oldest unresolved message, or
        None when there is none or it is not queued (it holds the ones after)."""
        _text(generation, self.limits.name_chars, "generation")
        with self._write() as conn:
            row = conn.execute(
                f"SELECT * FROM inbox_messages WHERE recipient=? AND conversation=?"
                f" AND {_UNRESOLVED} ORDER BY seq LIMIT 1", (recipient, conversation)).fetchone()
            if row is None or row["state"] != "queued":
                return None
            attempt = Attempt(uuid.uuid4().hex, row["seq"], row["key"], row["calls"] + 1,
                              generation, row["body"].decode("utf-8"))
            conn.execute(
                "INSERT INTO inbox_attempts(id, seq, ordinal, generation) VALUES (?, ?, ?, ?)",
                (attempt.id, attempt.seq, attempt.ordinal, generation))
            conn.execute("UPDATE inbox_messages SET state='attempting', reason=NULL, calls=?"
                         " WHERE seq=?", (attempt.ordinal, attempt.seq))
            self._event(conn, attempt.seq, "attempting", None)
        return attempt

    def record(self, attempt: Attempt, outcome: Outcome) -> Status:
        """Store what the engine said about the open attempt. Accepted drops the
        body and frees its pending usage in the same transaction."""
        outcome = Outcome(outcome)
        with self._write() as conn:
            row = conn.execute(
                "SELECT m.*, a.generation, a.outcome FROM inbox_attempts a"
                " JOIN inbox_messages m ON m.seq = a.seq WHERE a.id=?", (attempt.id,)).fetchone()
            if (row is None or (row["seq"], row["generation"]) != (attempt.seq, attempt.generation)
                    or row["outcome"] is not None or row["state"] != "attempting"):
                raise Stale(attempt.id)
            conn.execute("UPDATE inbox_attempts SET outcome=? WHERE id=?", (outcome.value, attempt.id))
            body = row["body"]
            if outcome is Outcome.ACCEPTED:
                state, reason, body = "native_accepted", None, None
                self._charge(conn, row["recipient"], -1, -row["size"], 0)
            elif outcome is Outcome.NOT_ADMITTED:
                state, reason = ("queued", None) if row["calls"] < MAX_CALLS else ("blocked", "calls_used")
            elif outcome is Outcome.UNKNOWN:
                state, reason = "uncertain", "unknown"
            else:
                state, reason = "blocked", outcome.value
            conn.execute("UPDATE inbox_messages SET state=?, reason=?, body=? WHERE seq=?",
                         (state, reason, body, attempt.seq))
            self._event(conn, attempt.seq, state, reason)
            row = conn.execute("SELECT * FROM inbox_messages WHERE seq=?", (attempt.seq,)).fetchone()
        return _status(row)

    def reconcile(self) -> list[tuple[str, str]]:
        """After a restart, with the old actors stopped: every attempt nobody
        recorded becomes uncertain (it may have reached the engine). Returns the
        conversations with unresolved mail."""
        with self._write() as conn:
            open_ = conn.execute(
                f"SELECT seq FROM inbox_messages WHERE {_UNRESOLVED} AND state='attempting'").fetchall()
            for (seq,) in open_:
                conn.execute("UPDATE inbox_attempts SET outcome='unknown'"
                             " WHERE seq=? AND outcome IS NULL", (seq,))
                conn.execute("UPDATE inbox_messages SET state='uncertain', reason='interrupted'"
                             " WHERE seq=?", (seq,))
                self._event(conn, seq, "uncertain", "interrupted")
            rows = conn.execute(f"SELECT DISTINCT recipient, conversation FROM inbox_messages"
                                f" WHERE {_UNRESOLVED}").fetchall()
        return [tuple(r) for r in rows]

    # -- storage --------------------------------------------------------------

    @contextmanager
    def _write(self):
        try:
            with lodge_db.write(self.root, durable=True, busy_ms=self.limits.busy_ms) as conn:
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise _unavailable(exc) from None

    @contextmanager
    def _read(self):
        try:
            conn = lodge_db.connect(self.root, readonly=True, busy_ms=self.limits.busy_ms)
            try:
                yield conn
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise _unavailable(exc) from None

    @staticmethod
    def _usage(conn, scope):
        row = conn.execute("SELECT * FROM inbox_usage WHERE scope=?", (scope,)).fetchone()
        return row or {"pending": 0, "bytes": 0, "keys": 0}

    @staticmethod
    def _charge(conn, recipient, pending, size, keys):
        for scope in ("", recipient):
            conn.execute(
                "INSERT INTO inbox_usage(scope, pending, bytes, keys) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(scope) DO UPDATE SET pending=pending+excluded.pending,"
                " bytes=bytes+excluded.bytes, keys=keys+excluded.keys",
                (scope, pending, size, keys))

    def _event(self, conn, seq, state, reason):
        """Append to the ring, or count a repeat of the newest event."""
        last = conn.execute("SELECT * FROM inbox_events ORDER BY cursor DESC LIMIT 1").fetchone()
        if last is not None and (last["seq"], last["state"], last["reason"]) == (seq, state, reason):
            conn.execute("UPDATE inbox_events SET repeats=repeats+1 WHERE cursor=?", (last["cursor"],))
            return
        cursor = conn.execute("INSERT INTO inbox_events(seq, state, reason) VALUES (?, ?, ?)",
                              (seq, state, reason)).lastrowid
        conn.execute("DELETE FROM inbox_events WHERE cursor <= ?", (cursor - self.limits.events,))
