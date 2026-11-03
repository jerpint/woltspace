"""
Who inbox mail is for (RFC #615): an enrolled session, bound for good.

Inactive: nothing in the lodge enrolls a session or admits through here. The
tests enroll their own sessions and supply what a runtime would have to
verify (readiness, generation); nothing here inspects a process.

Enrollment gives an existing session row an incarnation, a UUID made here,
and freezes the harness and conversation it had. The incarnation dies with
the row: a session made again under the same name is a new one, and mail
bound to the old incarnation never moves to it. sessions.status stays the
only lifecycle.

A request names a wolt, a session name or an incarnation. The key is looked
up first: a retry gets the original message back, wherever it was bound and
whatever has become of that session since. Only a new key resolves, once, to
an eligible session, and is admitted in the same durable transaction, so the
check and the binding cannot drift apart.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import inbox
import lodge_db
import sessions
from inbox import Invalid, Refused

KINDS = ("wolt", "session-name", "incarnation")
READINESS = ("running", "resting", "unknown")  # what the runtime verified, supplied by the caller
ENDED = ("stopped", "completed", "failed")
LIFECYCLE = ("running", "resting", sessions.STOPPING, *ENDED)
TERMINAL = (*ENDED, "archived")  # archive_session's status: never enrolled or reopened, nor set here
HOLDS = ("target_resting", "target_stopping", "target_unknown")  # released once it runs again
_INCARNATION_CHARS = 36
_ENROLLED = "inbox:enrollments"  # meta key: enrollments ever made, never released

# May take new mail: verified running, or resting on its enrolled conversation.
_ELIGIBLE = ("((status = 'running' AND inbox_readiness = 'running')"
             " OR (status = 'resting' AND inbox_readiness = 'resting'))"
             " AND harness IS inbox_harness AND harness_session_id IS inbox_conversation")


@dataclass(frozen=True)
class BindingLimits:
    """Supplied by the caller, like inbox.Limits: there are no defaults."""
    enrollments: int    # ever made, in this slice
    field_chars: int    # every name, address, harness and generation

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value < 1:
                raise Invalid(f"limit {name} must be a positive int")


@dataclass(frozen=True)
class Binding:
    name: str
    wolt: str
    incarnation: str
    harness: str
    conversation: str
    status: str
    readiness: str
    generation: str
    revision: int


def _binding(row) -> Binding:
    return Binding(row["name"], row["wolt"], row["inbox_incarnation"], row["inbox_harness"],
                   row["inbox_conversation"], row["status"], row["inbox_readiness"],
                   row["inbox_generation"], row["inbox_revision"])


def _refusal(row) -> str | None:
    """Why this session row takes no new mail, or None when it does."""
    if row is None:
        return "no_target"
    if row["inbox_incarnation"] is None:
        return "target_unbound"
    if row["eligible"]:
        return None
    if (row["harness"], row["harness_session_id"]) != (row["inbox_harness"], row["inbox_conversation"]):
        return "binding_mismatch"
    if row["status"] == sessions.STOPPING:
        return "target_stopping"  # retry later
    return "target_ended" if row["status"] in TERMINAL else "target_unknown"


def _target(incarnation, revision) -> None:
    """Checked before the store is opened."""
    inbox._text(incarnation, _INCARNATION_CHARS, "incarnation")
    inbox._count(revision, "revision", 1)


class Authority:
    def __init__(self, store: inbox.Store, limits: BindingLimits):
        if store.limits.name_chars < _INCARNATION_CHARS:
            raise Invalid(f"limit name_chars must hold an incarnation ({_INCARNATION_CHARS})")
        self.store, self.limits = store, limits

    def enroll(self, name: str, *, harness: str, conversation: str, readiness: str,
               generation: str) -> Binding:
        """Bind an existing session whose row still has this harness and
        conversation. The same enrollment again returns it; any other refuses."""
        self._text(name, "name")
        self._text(harness, "harness")
        self._text(conversation, "conversation", self.store.limits.name_chars)
        self._text(generation, "generation", self.store.limits.name_chars)
        if readiness not in READINESS:
            raise Invalid(f"readiness must be one of {READINESS}")
        with self._owned() as conn:
            row = self._session(conn, "name", name)
            if row is None:
                raise Refused("no_target")
            if (row["harness"], row["harness_session_id"]) != (harness, conversation):
                raise Refused("binding_mismatch")
            if row["inbox_incarnation"] is not None:
                if (row["inbox_harness"], row["inbox_conversation"], row["inbox_readiness"],
                        row["inbox_generation"]) != (harness, conversation, readiness, generation):
                    raise Refused("already_enrolled")
                return _binding(row)
            if row["status"] == sessions.STOPPING or row["status"] in TERMINAL:
                raise Refused("target_ended" if row["status"] in TERMINAL else "target_stopping")
            used = conn.execute("SELECT value FROM meta WHERE key=?", (_ENROLLED,)).fetchone()
            used = int(used[0]) if used else 0
            if used >= self.limits.enrollments:
                raise Refused("enrollments")
            conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)"
                         " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (_ENROLLED, str(used + 1)))
            conn.execute(
                "UPDATE sessions SET inbox_incarnation=?, inbox_harness=?, inbox_conversation=?,"
                " inbox_readiness=?, inbox_generation=?, inbox_revision=1 WHERE name=?",
                (str(uuid.uuid4()), harness, conversation, readiness, generation, name))
            return _binding(self._session(conn, "name", name))

    def admit(self, key: str, kind: str, address: str, body: str) -> inbox.Status:
        """Queue body for the session the request names, or return the message
        already under key. The receipt's recipient is the incarnation."""
        if kind not in KINDS:
            raise Invalid(f"kind must be one of {KINDS}")
        inbox._text(key, self.store.limits.key_chars, "key")
        self._text(address, "address")
        payload, digest = self.store._payload(body)
        with self._owned() as conn:
            row = self.store._retained(conn, key, payload, digest,
                                       request_kind=kind, request_address=address)
            if row is not None:
                return inbox._status(row)
            if kind == "wolt":
                target = conn.execute(
                    f"SELECT *, 1 AS eligible FROM sessions WHERE wolt=? AND inbox_incarnation IS NOT NULL"
                    f" AND {_ELIGIBLE} ORDER BY coalesce(last_activity, 0) DESC, created_at DESC, name"
                    f" LIMIT 1", (address,)).fetchone()
            else:
                target = self._session(conn, "name" if kind == "session-name" else "inbox_incarnation",
                                       address)
            code = _refusal(target)
            if code is not None:
                raise Refused(code)
            status = self.store._insert(conn, key, target["inbox_incarnation"], target["inbox_conversation"],
                                        payload, digest, (kind, address, target["inbox_harness"]))
            if target["status"] != "running":  # resting: held in the same commit
                status = self._move(conn, status.seq, "blocked", "target_resting")
            return status

    def transition(self, incarnation: str, revision: int, *, status: str, readiness: str,
                   generation: str | None = None,
                   predecessor_fenced: bool = False) -> tuple[Binding, list[inbox.Status]]:
        """Move an enrolled session to a new lifecycle state, as the runtime
        verified it, if its binding is still at revision. A new generation is
        for the same conversation only, once the caller has fenced the old one.
        Returns the binding and the mail this settled, both committed."""
        if status not in LIFECYCLE:
            raise Invalid(f"status must be one of {LIFECYCLE}")
        if readiness not in READINESS:
            raise Invalid(f"readiness must be one of {READINESS}")
        if generation is not None:
            self._text(generation, "generation", self.store.limits.name_chars)
        _target(incarnation, revision)
        with self._owned() as conn:
            row = self._current(conn, incarnation, revision)
            if row["status"] in TERMINAL:
                raise Refused("target_ended")  # an end is for good
            generation = generation or row["inbox_generation"]
            if (status, readiness, generation) == (row["status"], row["inbox_readiness"], row["inbox_generation"]):
                return _binding(row), []  # nothing changed: a call in flight stays valid
            if generation != row["inbox_generation"] and not (predecessor_fenced is True and status == "running"):
                raise Refused("generation_unfenced")
            conn.execute(
                "UPDATE sessions SET status=?, record=json_set(record, '$.status', ?), updated_at=?,"
                " inbox_readiness=?, inbox_generation=?, inbox_revision=? WHERE name=?",
                (status, status, int(time.time()), readiness, generation, revision + 1, row["name"]))
            row = self._session(conn, "name", row["name"])
            return _binding(row), self._settle(conn, row, ended=status in ENDED)

    def delete(self, incarnation: str, revision: int) -> list[inbox.Status]:
        """Delete an enrolled session's row. Its mail stays, bound to an
        incarnation that no longer exists: blocked, never sent elsewhere.
        An ended session can be deleted too."""
        _target(incarnation, revision)
        with self._owned() as conn:
            row = self._current(conn, incarnation, revision)
            settled = self._settle(conn, row, ended=True)
            conn.execute("DELETE FROM sessions WHERE name=?", (row["name"],))
            return settled

    # -- storage --------------------------------------------------------------

    def _current(self, conn, incarnation, revision):
        """The enrolled session, still as enrolled and at revision; or Refused."""
        row = self._session(conn, "inbox_incarnation", incarnation)
        if row is None:
            raise Refused("no_target")
        if row["inbox_revision"] != revision:
            raise Refused("stale_revision")
        if (row["harness"], row["harness_session_id"]) != (row["inbox_harness"], row["inbox_conversation"]):
            raise Refused("binding_mismatch")
        if revision >= inbox._MAX_INT:
            raise Refused("revision_overflow")
        return row

    def _settle(self, conn, row, ended):
        """Bring the session's unresolved mail in line with its state, in the
        same commit. A call in flight may have reached the engine: uncertain.
        Queued mail is held while the session cannot take it, and held mail
        is queued again once it runs as enrolled; an end blocks both for good.
        Accepted, uncertain and otherwise blocked mail keeps its state."""
        if ended:
            hold = "target_ended"
        elif row["status"] == "running" and row["inbox_readiness"] == "running":
            hold = None
        elif row["status"] == "resting" and row["inbox_readiness"] == "resting":
            hold = "target_resting"
        else:
            hold = "target_stopping" if row["status"] == sessions.STOPPING else "target_unknown"
        settled = []
        for seq, state, reason in conn.execute(
                f"SELECT seq, state, reason FROM inbox_messages WHERE recipient=? AND conversation=?"
                f" AND {inbox._UNRESOLVED} ORDER BY seq",
                (row["inbox_incarnation"], row["inbox_conversation"])).fetchall():
            if state == "attempting":
                conn.execute("UPDATE inbox_attempts SET outcome='unknown' WHERE seq=? AND outcome IS NULL",
                             (seq,))
                state, reason = "uncertain", "target_changed"
            elif state == "queued" and hold is not None:
                state, reason = "blocked", hold
            elif state == "blocked" and reason in HOLDS and reason != hold:
                state, reason = ("queued", None) if hold is None else ("blocked", hold)
            else:
                continue
            settled.append(self._move(conn, seq, state, reason))
        return settled

    def _text(self, value, what, cap=None):
        return inbox._text(value, min(self.limits.field_chars, cap or self.limits.field_chars), what)

    @staticmethod
    def _session(conn, column, value):
        return conn.execute(f"SELECT *, {_ELIGIBLE} AS eligible FROM sessions WHERE {column}=?",
                            (value,)).fetchone()

    @contextmanager
    def _owned(self):
        """A durable write of our own. Never inside another write on this
        thread, the registry's included: it would be NORMAL, or wait on itself."""
        if lodge_db.in_write() or getattr(sessions._TX, "conns", None):
            raise Refused("transaction_conflict")
        with self.store._write() as conn:
            main = conn.execute("PRAGMA database_list").fetchone()["file"]
            if not (conn.in_transaction and conn.execute("PRAGMA synchronous").fetchone()[0] == 2
                    and os.path.realpath(main) == os.path.realpath(lodge_db.path(self.store.root))):
                raise inbox.Unavailable("store_failed")
            yield conn

    def _move(self, conn, seq, state, reason) -> inbox.Status:
        conn.execute("UPDATE inbox_messages SET state=?, reason=? WHERE seq=?", (state, reason, seq))
        self.store._event(conn, seq, state, reason)
        return inbox._status(conn.execute("SELECT * FROM inbox_messages WHERE seq=?", (seq,)).fetchone())
