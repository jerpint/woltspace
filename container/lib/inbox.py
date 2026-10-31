"""
The lodge inbox: a mailbox for each agent, which its sessions pull from.

Off unless woltspace.json says `"inbox": {"enabled": true}`; importing this
module opens nothing. Once on, a session hears of new mail by a pasted hint, or
through its engine's native hooks with `"native_hints": true` (native_hints). Callers hand it a wolts dir and explicit Limits.

A message is queued only once its row, its usage and its event have committed
together, in a durable (synchronous=FULL) write. The sender's key makes a
retry safe: the same key with the same body and destination returns the
original message; anything else under that key is a Conflict. Keys belong to
the sending mailbox's id, and a destination is a mailbox id: ids are never
reused, so a recreated wolt neither inherits nor receives an old one's mail.

A session reads through a reader: a token the launcher grants it, of which only
the SHA-256 is stored. A reader is valid while it is not revoked, its session
is running and its mailbox is open, checked inside every transaction. Every
agent and app here runs as the same OS user: readers stop mistakes and stale
sessions, not a misbehaving local program, which can open lodge.db directly.

fetch claims the next queued message, verified human mail first, then oldest,
and returns it; until that claim is acknowledged or released, fetch returns it
again, marked repeated. ack means "received", not "done": it drops the body in
the same commit. A claim whose reader is no longer valid is held: it is never
reassigned, and other mail keeps moving. Acknowledged metadata stays 30 days
after the first ack, then goes; a key is free again after that.

States: queued -> claimed -> acknowledged (body gone), or claimed -> queued
on release. "held" is derived, never stored.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import lodge_db

MAX_PAYLOAD_BYTES = 32 * 1024  # of UTF-8
RETAIN_SECONDS = 30 * 86400    # acknowledged metadata, from the first ack
HUMAN, AGENT = 0, 1            # priority: lower is fetched first
MESSAGE_PAGE = 50              # rows per page of the owner's viewer (Store.messages)
_MAX_INT = 2**63 - 1  # SQLite's INTEGER
# A reader that may act now; binds ? to its hash.
# Acknowledged metadata past its 30 days; binds ? to the cutoff. Absent to every read.
_EXPIRED = "m.state = 'acknowledged' AND m.acked_at < ?"
_VALID = ("SELECT r.mailbox, b.wolt FROM inbox_readers r JOIN inbox_mailboxes b ON b.id = r.mailbox"
          " JOIN sessions s ON s.name = r.session WHERE r.hash = ? AND r.revoked = 0"
          " AND b.state = 'open' AND s.status = 'running' AND s.wolt = b.wolt")


def enabled(wolts_dir) -> bool:
    """The lodge's switch: woltspace.json `inbox.enabled`, off when unreadable."""
    try:
        config = json.loads((Path(wolts_dir) / "woltspace.json").read_text())
        return config.get("inbox", {}).get("enabled") is True
    except (OSError, ValueError, AttributeError):
        return False


def native_hints(wolts_dir) -> bool:
    """woltspace.json `inbox.native_hints`: True for native hooks, missing or false for a
    pasted hint. Any other value is Invalid (a launch refuses before its effects)."""
    try:
        value = json.loads((Path(wolts_dir) / "woltspace.json").read_text()).get("inbox", {}).get("native_hints", False)
    except (OSError, ValueError, AttributeError):
        return False
    if isinstance(value, bool):
        return value
    raise Invalid("inbox.native_hints must be true or false")


class InboxError(Exception):
    code = "inbox_error"


class Invalid(InboxError, ValueError):
    """Malformed input; nothing was written."""
    code = "invalid"


class Conflict(InboxError):
    """The key holds a different body or destination."""
    code = "conflict"


class Refused(InboxError):
    """Not allowed or a limit is reached; nothing was written. `code` names it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Unavailable(InboxError):
    """The store could not be read or written. After a write, whether it
    committed is unknown: retry the same call, or read again later.

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
    pending: int            # unacknowledged messages, lodge-wide
    pending_bytes: int
    mailbox_pending: int
    mailbox_bytes: int
    keys: int               # messages retained, acknowledged ones included
    key_chars: int
    name_chars: int         # wolt, sender and session names
    events: int             # ring size
    page: int               # events per read
    busy_ms: int            # how long a write or read waits for another writer

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise Invalid(f"limit {name} must be a positive int")
        if self.page > self.events:
            raise Invalid("limit page exceeds events")


# What the lodge's own service uses.
LIMITS = Limits(pending=10_000, pending_bytes=64 * 2**20, mailbox_pending=1_000,
                mailbox_bytes=16 * 2**20, keys=100_000, key_chars=200, name_chars=200,
                events=10_000, page=500, busy_ms=5000)


@dataclass(frozen=True)
class Status:
    seq: int
    sender: str     # the sending wolt's or provider's name, for display
    key: str
    to: str
    priority: int
    state: str      # queued, claimed, held or acknowledged


@dataclass(frozen=True)
class Delivery:
    seq: int
    sender: str
    priority: int
    body: str
    claim: int
    repeated: bool


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


def _hash(token) -> bytes:
    if type(token) is not str or not 20 <= len(token) <= 200:
        raise Refused("reader_invalid")
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


class Store:
    def __init__(self, wolts_dir, limits: Limits):
        self.root = wolts_dir
        self.limits = limits

    # -- readers and mailboxes, for the launcher and the lodge -----------------

    def grant(self, session: str, wolt: str) -> str | None:
        """Revoke the session's earlier readers and mint a new one on the
        wolt's mailbox, in one write. The token is returned once, never stored.
        None for a wolt that cannot have a mailbox: the revocation still holds."""
        _text(session, self.limits.name_chars, "session")
        _text(wolt, self.limits.name_chars, "wolt", 0)
        token = secrets.token_urlsafe(32)
        with self._write() as conn:
            conn.execute("UPDATE inbox_readers SET revoked=1 WHERE session=?", (session,))
            try:
                mailbox = self._mailbox(conn, wolt, create=True)
            except Refused as exc:
                if exc.code != "no_mailbox":
                    raise
                return None
            conn.execute("INSERT INTO inbox_readers(hash, mailbox, session, created_at) VALUES (?, ?, ?, ?)",
                         (_hash(token), mailbox, session, time.time()))
        return token

    def set_state(self, wolt: str, state: str) -> None:
        """archived blocks admission and claims; open undoes that; closed
        retires the mailbox for good, refused while it holds unacknowledged
        mail. A wolt with no mailbox has nothing to change."""
        if state not in ("open", "archived", "closed"):
            raise Invalid("state")
        with self._write() as conn:
            row = conn.execute("SELECT id FROM inbox_mailboxes WHERE wolt=? AND state != 'closed'",
                               (wolt,)).fetchone()
            if row is None:
                return
            if state == "closed" and conn.execute(
                    "SELECT 1 FROM inbox_messages WHERE mailbox=? AND state != 'acknowledged' LIMIT 1",
                    (row[0],)).fetchone():
                raise Refused("mailbox_custody")
            conn.execute("UPDATE inbox_mailboxes SET state=? WHERE id=?", (state, row[0]))
            if state == "closed":
                conn.execute("UPDATE inbox_readers SET revoked=1 WHERE mailbox=?", (row[0],))

    # -- the waker's (inbox_wake.py), under the agent's lifecycle lock ------------

    def wake_facts(self, wolt: str) -> dict | None:
        """The open mailbox's queued counts, its wake state and the mail no wake answered
        yet, in one snapshot."""
        with self._read() as conn:
            row = conn.execute("SELECT id, state, wake FROM inbox_mailboxes WHERE wolt=?"
                               " AND state != 'closed'", (wolt,)).fetchone()
            if row is None:
                return None
            wake = json.loads(row["wake"] or "{}")
            # mail after the last one a wake answered, in any state (before any answer: the
            # queued only); a reader's session once one has taken it
            since, args = (("m.seq > ?", (wake["answered"],)) if "answered" in wake
                           else ("m.state = 'queued'", ()))
            return {"open": row["state"] == "open", "wake": wake,
                    "queued": conn.execute("SELECT count(*) FROM inbox_messages WHERE mailbox=?"
                                           " AND state='queued'", (row["id"],)).fetchone()[0],
                    "human": conn.execute("SELECT count(*) FROM inbox_messages WHERE mailbox=?"
                                          " AND state='queued' AND priority=?", (row["id"], HUMAN)).fetchone()[0],
                    "human_seq": conn.execute("SELECT max(seq) FROM inbox_messages WHERE mailbox=?"
                                              " AND state='queued' AND priority=?", (row["id"], HUMAN)).fetchone()[0],
                    "prefer_queued": conn.execute(  # the preference's own message, still for its session
                        "SELECT 1 FROM inbox_messages WHERE seq=? AND state='queued'",
                        ((wake.get("prefer") or {}).get("seq"),)).fetchone() is not None,
                    "senders": list(dict.fromkeys(  # who the queued agent mail is from, oldest first
                        name for name, in conn.execute(
                            "SELECT sender_name FROM inbox_messages WHERE mailbox=? AND state='queued'"
                            " AND priority != ? ORDER BY seq", (row["id"], HUMAN)))),
                    "unanswered": [{"seq": seq, "human": priority == HUMAN, "session": taker}
                                   for seq, priority, taker in conn.execute(
                        "SELECT m.seq, m.priority, CASE WHEN m.state != 'queued' THEN r.session END"
                        " FROM inbox_messages m LEFT JOIN inbox_readers r ON r.hash = m.reader"
                        f" WHERE m.mailbox=? AND {since} ORDER BY m.seq", (row["id"], *args))]}

    def reader_session(self, token: str) -> tuple[str, str] | None:
        """(session, wolt) of a valid reader, or None."""
        with self._read() as conn:
            row = conn.execute(_VALID.replace("SELECT r.mailbox, b.wolt", "SELECT r.session, b.wolt"),
                               (_hash(token),)).fetchone()
            return (row[0], row[1]) if row else None

    def queued_wolts(self) -> list[str]:
        with self._read() as conn:
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT b.wolt FROM inbox_mailboxes b JOIN inbox_messages m ON m.mailbox = b.id"
                " WHERE b.state = 'open' AND m.state = 'queued'")]

    def set_wake(self, wolt: str, wake: dict) -> None:
        """The waker's write. What admission records in the same field (_note) is kept from
        the stored state, never from the waker's copy: the baseline once set, the preferred
        session, and the routes of the mail not answered yet."""
        with self._write() as conn:
            row = conn.execute("SELECT wake FROM inbox_mailboxes WHERE wolt=? AND state != 'closed'",
                               (wolt,)).fetchone()
            if row is None:
                return
            stored, wake = json.loads(row[0] or "{}"), {k: v for k, v in (wake or {}).items()
                                                          if k not in ("prefer", "chats")}
            if "answered" in stored:
                wake["answered"] = max(wake.get("answered", stored["answered"]), stored["answered"])
            if "prefer" in stored:
                wake["prefer"] = stored["prefer"]
            chats = {k: v for k, v in stored.get("chats", {}).items() if int(k) > wake.get("answered", -1)}
            if chats:
                wake["chats"] = chats
            conn.execute("UPDATE inbox_mailboxes SET wake=? WHERE wolt=? AND state != 'closed'",
                         (json.dumps(wake) if wake else None, wolt))

    # -- the reader's -----------------------------------------------------------

    def send(self, token: str, to: str, key: str, body: str, session: str | None = None) -> Status:
        """Mail from the reader's own wolt, always at agent priority. session: the one the
        sender named, only the wake's first choice (inbox_wake checks it is to's)."""
        reader = _hash(token)
        if session is not None:
            _text(session, self.limits.name_chars, "session")

        def sender(conn):
            mailbox, wolt = self._reader(conn, reader)
            return f"mailbox:{mailbox}", wolt
        return self._admit(sender, key, to, body, AGENT, route={"session": session})

    def admit(self, sender: str, key: str, to: str, body: str, *, human: bool = False,
              mailbox: int | None = None, route: dict | None = None) -> Status:
        """Library admission for a trusted provider that has verified its
        sender itself; only it may say human. route: {"session", "chat"}, the
        session the sender is talking to and the chat its answer goes to,
        recorded with the message (_note)."""
        _text(sender, self.limits.name_chars, "sender")
        return self._admit(lambda conn: (f"provider:{sender}", sender), key, to, body, HUMAN if human else AGENT,
                           mailbox, route)

    def fetch(self, token: str) -> Delivery | None:
        """The reader's open claim again, or a claim on the next queued
        message (human first, then oldest), or None when there is none."""
        reader = _hash(token)
        with self._write() as conn:
            mailbox, _ = self._reader(conn, reader)
            row = conn.execute("SELECT * FROM inbox_messages WHERE reader=? AND state='claimed'",
                               (reader,)).fetchone()
            repeated = row is not None
            if row is None:
                row = conn.execute("SELECT * FROM inbox_messages WHERE mailbox=? AND state='queued'"
                                   " ORDER BY priority, seq LIMIT 1", (mailbox,)).fetchone()
                if row is None:
                    return None
                conn.execute("UPDATE inbox_messages SET state='claimed', reader=?, claim=claim+1"
                             " WHERE seq=?", (reader, row["seq"]))
                self._event(conn, row["seq"], "claimed", None)
            claim = row["claim"] + (0 if repeated else 1)
        return Delivery(row["seq"], row["sender_name"], row["priority"], row["body"].decode("utf-8"),
                        claim, repeated)

    def ack(self, token: str, seq: int, claim: int) -> Status:
        """Receipt, by the claiming reader only: the body goes, its usage is
        freed and the event written, in one commit. Again: no change."""
        reader, now = _hash(token), time.time()
        with self._write() as conn:
            row = self._claimed(conn, reader, seq, claim, ("claimed", "acknowledged"))
            if row["state"] == "claimed":
                conn.execute("UPDATE inbox_messages SET state='acknowledged', body=NULL, acked_at=?"
                             " WHERE seq=?", (now, seq))
                conn.execute("UPDATE inbox_readers SET acks=acks+1 WHERE hash=?", (reader,))
                self._charge(conn, row["mailbox"], -1, -row["size"], 0)
                self._event(conn, seq, "acknowledged", None)
            return self._status(conn, "m.seq=?", (seq,))

    def release(self, token: str, seq: int, claim: int) -> Status:
        """Give an unhandled claim back: queued again, for any reader."""
        reader = _hash(token)
        with self._write() as conn:
            self._claimed(conn, reader, seq, claim, ("claimed",))
            conn.execute("UPDATE inbox_messages SET state='queued', reader=NULL WHERE seq=?", (seq,))
            self._event(conn, seq, "queued", "released")
            return self._status(conn, "m.seq=?", (seq,))

    def status(self, token: str, key: str | None = None) -> Status | dict | None:
        """With key: a message this reader's wolt sent (None when not retained).
        Without: how many messages its mailbox holds in each state. Reads only."""
        reader, cutoff = _hash(token), time.time() - RETAIN_SECONDS
        if key is not None:
            _text(key, self.limits.key_chars, "key")
        with self._read() as conn:
            conn.execute("BEGIN")  # one snapshot: the reader is checked in the read it allows
            mailbox, _ = self._reader(conn, reader)
            if key is not None:
                return self._status(conn, f"m.sender=? AND m.key=? AND NOT ({_EXPIRED})",
                                    (f"mailbox:{mailbox}", key, cutoff))
            rows = conn.execute(
                "SELECT state, count(*) FROM (SELECT CASE WHEN m.state='claimed' AND NOT EXISTS ("
                + _VALID.replace("r.hash = ?", "r.hash = m.reader") + ") THEN 'held' ELSE m.state END state"
                f" FROM inbox_messages m WHERE m.mailbox=? AND NOT ({_EXPIRED})) GROUP BY state",
                (mailbox, cutoff)).fetchall()
        return {state: 0 for state in ("queued", "claimed", "held", "acknowledged")} | dict(rows)

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

    # -- inside a write ---------------------------------------------------------

    def _admit(self, sender_of, key, to, body, priority, expected=None, route=None) -> Status:
        _text(key, self.limits.key_chars, "key")
        _text(to, self.limits.name_chars, "to")
        payload = _text(body, MAX_PAYLOAD_BYTES, "body", 0).encode("utf-8")
        if len(payload) > MAX_PAYLOAD_BYTES:
            raise Invalid(f"body exceeds {MAX_PAYLOAD_BYTES} bytes of UTF-8")
        digest, now = hashlib.sha256(payload).digest(), time.time()
        with self._write() as conn:
            sender, name = sender_of(conn)
            row = conn.execute("SELECT * FROM inbox_messages WHERE sender=? AND key=?", (sender, key)).fetchone()
            if row is not None:
                current = conn.execute("SELECT id FROM inbox_mailboxes WHERE wolt=? AND state != 'closed'",
                                       (to,)).fetchone()
                if ((row["mailbox"], row["priority"], row["size"], row["digest"])
                        != (current and current[0], priority, len(payload), digest)
                        or row["body"] is not None and row["body"] != payload):
                    raise Conflict("conflict")  # fixed: the key is the caller's text
                return self._status(conn, "m.seq=?", (row["seq"],))
            mailbox = self._mailbox(conn, to, create=True)
            if expected is not None and mailbox != expected:  # bound to that very mailbox: its wolt was recreated
                raise Refused("no_mailbox")
            lodge, mine = self._usage(conn, ""), self._usage(conn, str(mailbox))
            limits = self.limits
            for code, over in (
                ("keys", lodge["keys"] >= limits.keys),
                ("pending", lodge["pending"] >= limits.pending),
                ("pending_bytes", lodge["bytes"] + len(payload) > limits.pending_bytes),
                ("mailbox_pending", mine["pending"] >= limits.mailbox_pending),
                ("mailbox_bytes", mine["bytes"] + len(payload) > limits.mailbox_bytes),
            ):
                if over:
                    raise Refused(code)
            seq = conn.execute(
                "INSERT INTO inbox_messages(sender, sender_name, key, mailbox, priority, body, size, digest,"
                " state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)",
                (sender, name, key, mailbox, priority, payload, len(payload), digest, now)).lastrowid
            self._charge(conn, mailbox, 1, len(payload), 1)
            self._event(conn, seq, "queued", None)
            self._note(conn, mailbox, seq, priority, route or {}, now)
            return self._status(conn, "m.seq=?", (seq,))

    @staticmethod
    def _note(conn, mailbox: int, seq: int, priority: int, route: dict, now: float) -> None:
        """The waker's accounting, in the admission's own write (inbox_wake answers human mail
        only once its route is here): on the mailbox's first admission since accounting
        began, the answered baseline just below the queued mail, whose human messages take
        the default route; the preferred session; and a human message's route (None: the
        default channel). A duplicate admission never gets here."""
        wake = json.loads(conn.execute("SELECT wake FROM inbox_mailboxes WHERE id=?", (mailbox,)).fetchone()[0]
                          or "{}")
        chats = wake.setdefault("chats", {})
        if "answered" not in wake:  # older mail already handled is never answered again
            queued = conn.execute("SELECT seq, priority FROM inbox_messages WHERE mailbox=? AND state='queued'"
                                  " ORDER BY seq", (mailbox,)).fetchall()
            wake["answered"] = queued[0][0] - 1
            chats.update({str(q): None for q, p in queued if p == HUMAN})
        if route.get("session"):
            wake["prefer"] = {"session": route["session"], "seq": seq, "at": int(now)}
        if priority == HUMAN:
            chats[str(seq)] = route.get("chat")
        conn.execute("UPDATE inbox_mailboxes SET wake=? WHERE id=?", (json.dumps(wake), mailbox))

    def _mailbox(self, conn, wolt, *, create=False) -> int:
        """The wolt's open mailbox; a new one for a wolt the lodge knows."""
        row = conn.execute("SELECT id, state FROM inbox_mailboxes WHERE wolt=? AND state != 'closed'",
                           (wolt,)).fetchone()
        if row is not None:
            if row["state"] != "open":
                raise Refused("mailbox_archived")
            return row["id"]
        if not (create and wolt not in ("", ".", "..") and "/" not in wolt
                and (Path(self.root) / wolt / "wolt" / "wolt.json").is_file()):
            raise Refused("no_mailbox")
        return conn.execute("INSERT INTO inbox_mailboxes(wolt, state, created_at) VALUES (?, 'open', ?)",
                            (wolt, time.time())).lastrowid

    @staticmethod
    def _reader(conn, reader: bytes) -> tuple[int, str]:
        row = conn.execute(_VALID, (reader,)).fetchone()
        if row is None:
            raise Refused("reader_invalid")
        return row[0], row[1]

    def _claimed(self, conn, reader, seq, claim, states):
        """The message this valid reader holds under exactly this claim."""
        _count(seq, "seq", 1)
        _count(claim, "claim", 1)
        self._reader(conn, reader)
        row = conn.execute("SELECT * FROM inbox_messages WHERE seq=? AND reader=? AND claim=?",
                           (seq, reader, claim)).fetchone()
        if row is None or row["state"] not in states:
            raise Refused("stale_claim")
        return row

    @staticmethod
    def _status(conn, where, args) -> Status | None:
        row = conn.execute(
            "SELECT m.seq, m.sender_name, m.key, b.wolt, m.priority, CASE WHEN m.state='claimed' AND NOT EXISTS ("
            + _VALID.replace("r.hash = ?", "r.hash = m.reader") + ") THEN 'held' ELSE m.state END"
            " FROM inbox_messages m JOIN inbox_mailboxes b ON b.id = m.mailbox WHERE " + where,
            args).fetchone()
        return None if row is None else Status(*row)

    def _prune(self, conn, now) -> None:
        """Acknowledged metadata past its 30 days, never anything else."""
        cutoff = now - RETAIN_SECONDS
        for mailbox, count in conn.execute(
                f"SELECT mailbox, count(*) FROM inbox_messages m WHERE {_EXPIRED} GROUP BY mailbox",
                (cutoff,)).fetchall():
            self._charge(conn, mailbox, 0, 0, -count)
        conn.execute(f"DELETE FROM inbox_messages AS m WHERE {_EXPIRED}", (cutoff,))
        conn.execute("DELETE FROM inbox_readers WHERE revoked=1 AND created_at < ? AND hash NOT IN"
                     " (SELECT reader FROM inbox_messages WHERE reader IS NOT NULL)", (cutoff,))

    # -- storage --------------------------------------------------------------

    @contextmanager
    def _write(self):
        """Every durable write first drops what has expired: an expired
        receipt is unknown from then on, never acknowledged again."""
        try:
            with lodge_db.write(self.root, durable=True, busy_ms=self.limits.busy_ms) as conn:
                self._prune(conn, time.time())
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
    def _charge(conn, mailbox, pending, size, keys):
        for scope in ("", str(mailbox)):
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

    # -- the lodge's view, counts only -----------------------------------------

    def summary(self) -> list[dict]:
        """Each mailbox not closed, in one read snapshot: queued human and agent
        mail, claimed and held, and when the oldest queued message came. Mail
        for a human first, then the longest waiting. Reads no body, sender or key."""
        held = ("CASE WHEN m.state = 'claimed' AND NOT EXISTS ("
                + _VALID.replace("r.hash = ?", "r.hash = m.reader") + ") THEN 'held' ELSE m.state END")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT b.wolt, b.state, coalesce(sum(s.st = 'queued' AND s.priority = ?), 0) human,"
                " coalesce(sum(s.st = 'queued' AND s.priority = ?), 0) agent, coalesce(sum(s.st = 'claimed'), 0) claimed,"
                " coalesce(sum(s.st = 'held'), 0) held, min(CASE WHEN s.st = 'queued' THEN s.created_at END) oldest"
                " FROM inbox_mailboxes b LEFT JOIN (SELECT m.mailbox, m.priority, m.created_at, " + held + " st"
                " FROM inbox_messages m WHERE m.state != 'acknowledged') s ON s.mailbox = b.id"
                " WHERE b.state != 'closed' GROUP BY b.id ORDER BY human > 0 DESC, oldest IS NULL, oldest, b.wolt",
                (HUMAN, AGENT)).fetchall()
        return [{"wolt": r[0], "state": r[1], "queued_human": r[2], "queued_agent": r[3],
                 "claimed": r[4], "held": r[5], "oldest_queued_at": r[6]} for r in rows]

    def messages(self, wolt: str | None = None, before: int | None = None, limit: int = MESSAGE_PAGE) -> dict:
        """The owner's read-only view of retained mail, newest first: every queued, claimed or
        held message (an acknowledged one has no text left) for one receiver, or all when wolt
        is None. Each row: seq (the page cursor only), the sender's name, sender_wolt (set only
        while the sending mailbox is open, so it names a real wolt), human, when it came, its
        state and its text. Never a key, claim, reader or route. before: the previous page's
        oldest seq; next: this page's, or None on the last page."""
        if wolt is not None:
            _text(wolt, self.limits.name_chars, "wolt")
        cursor = _MAX_INT if before is None else _count(before, "before", 1)
        limit = min(_count(limit, "limit", 1), MESSAGE_PAGE)
        held = ("CASE WHEN m.state = 'claimed' AND NOT EXISTS ("
                + _VALID.replace("r.hash = ?", "r.hash = m.reader") + ") THEN 'held' ELSE m.state END")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT m.seq, m.sender_name, src.wolt, m.priority, m.created_at, " + held + ", m.body"
                " FROM inbox_messages m JOIN inbox_mailboxes dest ON dest.id = m.mailbox"
                " LEFT JOIN inbox_mailboxes src ON m.sender = 'mailbox:' || src.id AND src.state = 'open'"
                " WHERE m.state != 'acknowledged' AND dest.state != 'closed' AND (? IS NULL OR dest.wolt = ?)"
                " AND m.seq < ? ORDER BY m.seq DESC LIMIT ?", (wolt, wolt, cursor, limit + 1)).fetchall()
        page = [{"seq": r[0], "sender": r[1], "sender_wolt": r[2], "human": r[3] == HUMAN, "at": r[4],
                 "state": r[5], "text": (r[6] or b"").decode("utf-8", "replace")} for r in rows[:limit]]
        return {"messages": page, "next": page[-1]["seq"] if len(rows) > limit else None}

    def unnoticed_agent_mail(self, wolt: str, reason: str) -> list[dict]:
        """Queued mail for the wolt from other agents (never a provider: a lodge
        notice is never noticed) without a lodge notice for this reason yet:
        its seq, the sending mailbox's wolt (only while that very mailbox is
        not closed: a recreated wolt is someone else) and the sender's own key.
        Reads no body."""
        with self._read() as conn:
            rows = conn.execute(
                "SELECT m.seq, s.wolt, m.key, s.id FROM inbox_messages m JOIN inbox_mailboxes b ON b.id = m.mailbox"
                " JOIN inbox_mailboxes s ON m.sender = 'mailbox:' || s.id AND s.state != 'closed'"
                " WHERE b.wolt = ? AND b.state != 'closed' AND m.state = 'queued'"
                " AND NOT EXISTS (SELECT 1 FROM inbox_messages n"
                " WHERE n.sender = 'provider:lodge' AND n.key = 'stuck:' || m.seq || ':' || ?) ORDER BY m.seq",
                (wolt, reason)).fetchall()
        return [{"seq": r[0], "sender": r[1], "key": r[2], "mailbox": r[3]} for r in rows]
