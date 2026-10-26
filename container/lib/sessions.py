from __future__ import annotations

"""
Session Registry & Spawning — single source of truth for session lifecycle.

Each session is one row in the lodge's SQLite file (lodge_db), its record kept
whole as JSON. Viewport URL is stored in the record itself — no separate
current-url files.

Usage:
    from sessions import SessionRegistry, start_session
    reg = SessionRegistry()
    reg.create("neowolt-chompy-dam-a3f1e2", wolt="neowolt", creature="beaver", ...)
"""

import fcntl
import hashlib
import json
import os
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import lodge_db
from env_compat import get_env
from paths import (
    WOLTS_DIR as _PATHS_WOLTS_DIR,
    tunnel_state_file,
    space_dir,
)
from harnesses import (
    resolve_agent_handle,
    HARNESSES,
    DEFAULT_HARNESS,
    resolve_harness,
    creature_model,
    resolve_model,
    get_harness,
    get_default_harness,
    harness_install_problem,
    build_command,
    session_has_agent_process,
    sessions_with_agent_process,
    platform_skill_invoke,
    PLATFORM_SKILL_NAMESPACE,
    LEGACY_PLATFORM_SKILL_PREFIX,
)
from skills_sync import wolt_skills_delivery
from sites import ensure_site
from session_expiry import hold_active, public_hold
from session_runtime import RuntimeHandle, get_runtime
from session_targets import SessionTarget, normalize_session_target
from execution_policy import (
    AutoGrantStore,
    ExecutionPolicy,
    resolve_execution_policy,
)
from runtime_context import RuntimeContext
from trust import ensure_claude_dir_trusted, ensure_codex_dir_trusted
from notify_prompt import notify_reply_instruction, session_send_reply_instruction

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
# Resolved relative to this file so the dev clone drives its own script —
# a hardcoded production path pairs new sessions.py with old run-session.sh.
RUN_SESSION_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "run-session.sh"


# Why a session stopped being `running`. Recorded on the record itself, because
# "orphaned" alone cannot tell a crashed agent from a machine that went away —
# and the adoption report that knew the difference is overwritten by the next
# reconcile.
ORPHAN_TMUX_MISSING = "tmux-session-missing"
ORPHAN_RUNTIME_GONE = "runtime-gone"
# Tmux still holds the session, but nothing in it is an agent any more: a
# failed respawn, an OOM-killed harness, or a session whose agent exited and
# left the login shell behind. Indistinguishable from healthy if you only ask
# tmux, which is exactly how an "alive" row used to drop a human into an
# agentless shell.
ORPHAN_AGENT_GONE = "agent-process-missing"

# The fields get(check_alive=True) and list() compute rather than read off disk.
LIVENESS_FIELDS = frozenset({"alive", "tmux_alive", "agent_alive"})
# A session whose tmux is being stopped outside any write (stop_and_settle).
STOPPING = "stopping"
# What decides whether a session may be stopped: a claim compares these only.
_CLAIM_FIELDS = ("status", "last_activity", "hold", "harness_session_id")
# How long a message waits for a stop in progress before it goes on.
STOPPING_WAIT = 30

# One write transaction per thread per lodge, shared by nested registry calls.
_TX = threading.local()


_COLUMNS = ("name, wolt, status, created_at, last_activity, finished_at,"
            " harness, harness_session_id, record, updated_at")


def _row_values(record: dict) -> tuple:
    return (
        record["name"], record.get("wolt") or "", record.get("status") or "stopped",
        int(record.get("created_at") or 0), record.get("last_activity"),
        record.get("finished_at"), record.get("harness"),
        record.get("harness_session_id") or None,
        json.dumps(record), int(time.time()),
    )


def put_session(conn, record: dict) -> None:
    """Write one session row: the whole record as JSON, the queried fields as columns.

    An upsert on the name, never INSERT OR REPLACE: a REPLACE would delete
    another session that holds the same harness_session_id, where this must
    fail with IntegrityError so the second claimer of a conversation loses.
    """
    conn.execute(
        f"INSERT INTO sessions ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET wolt=excluded.wolt, status=excluded.status,"
        " created_at=excluded.created_at, last_activity=excluded.last_activity,"
        " finished_at=excluded.finished_at, harness=excluded.harness,"
        " harness_session_id=excluded.harness_session_id, record=excluded.record,"
        " updated_at=excluded.updated_at",
        _row_values(record),
    )


def import_sessions(conn, wolts_dir) -> tuple[dict, list]:
    """Bring the old per-wolt session files into lodge.db (see lodge_db.run_imports).

    Reads every <wolt>/.state/sessions/*.json, archived wolts' too. A row
    already in the DB wins over a file, and of two files claiming one
    conversation id the newer one wins; every file not taken is logged and
    counted as skipped. Returns the counts and the folders to retire, with
    the lodge-wide resume-id-recovery.lock the old recovery used.
    """
    wolts_dir = Path(wolts_dir)
    folders = sorted(wolts_dir.glob("*/.state/sessions")) + sorted(
        wolts_dir.glob(".archive/*/.state/sessions"))
    found = []
    skipped = 0
    for folder in folders:
        wolt = folder.parent.parent.name
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as exc:
                print(f"[sessions] import skipped unreadable {path}: {exc}", file=sys.stderr)
                skipped += 1
                continue
            data.setdefault("name", path.stem)
            data.setdefault("wolt", wolt)
            found.append((data, path))
    found.sort(key=lambda item: item[0].get("created_at") or 0, reverse=True)
    imported = 0
    for data, path in found:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO sessions ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            _row_values(data),
        )
        if cursor.rowcount:
            imported += 1
        else:
            print(f"[sessions] import skipped {path}: its name or conversation id "
                  "is already in lodge.db", file=sys.stderr)
            skipped += 1
    retire = list(folders)
    recovery_lock = wolts_dir / ".state" / "resume-id-recovery.lock"
    if recovery_lock.exists():
        retire.append(recovery_lock)
    return {"imported": imported, "skipped": skipped}, retire


def _lodge_record(raw: dict) -> dict:
    """Only the session fields the lodge UI consumes."""
    record = {
        "name": raw["name"],
        "wolt": raw.get("wolt") or "",
        "status": raw.get("status") or "stopped",
    }
    if raw.get("last_activity") is not None:
        record["last_activity"] = raw["last_activity"]
    elif raw.get("created_at") is not None:
        record["created_at"] = raw["created_at"]
    for field in ("title", "summary"):
        if raw.get(field):
            record[field] = raw[field]
    if hold_active(raw, int(time.time())):
        record["hold"] = public_hold(raw["hold"])
    record["openable"] = bool(
        raw.get("harness_session_id") or raw.get("claude_session_id")
    )
    record["_tmux_session_name"] = (
        RuntimeHandle.from_record(raw).tmux_session_name or record["name"]
    )
    return record


class ResumeUnavailable(Exception):
    """This session can never be resumed — there is no conversation to replay.

    Distinct from ResumeFailed: retrying will not help, and the caller should
    offer a fresh session instead. Surfaces as 409 on the API.
    """


class ResumeFailed(Exception):
    """Resume was attempted and the agent did not come up. Surfaces as 502."""


class HarnessNotInstalled(ValueError):
    """The chosen engine's program is not on this lodge's PATH.

    Raised before anything is recorded or spawned, so the caller can show the
    reason instead of a session that dies with exit 127.
    """


# How long resume waits for a relaunched agent to show up in the process tree
# before calling it a failure. The launching shim (run-session.sh) counts, and
# that appears within a second or two of the tmux spawn — this is slack for a
# loaded host, not for a slow model. Running out is never persisted as
# "failed": it is a statement about our patience, not about the session.
_AGENT_APPEAR_TIMEOUT = 12.0
# The in-place revival gets a shorter leash: it pastes a command into a pane
# that either runs it right away or never will (a busy foreground process eats
# it), and the escalation behind it needs the remaining patience.
_AGENT_REVIVE_TIMEOUT = 6.0
_AGENT_POLL_INTERVAL = 0.4

# How long a message waits for an agent that is up to start taking input
# before it is refused instead of pasted (see _await_input_ready). Generous on
# purpose: running out turns a Telegram message into a fresh session.
INPUT_READY_TIMEOUT = 15
_INPUT_READY_POLL = 0.25
# How old the agent process must be before a paste. This is the readiness
# test; the terminal mode is not. Measured on Claude Code 2.1.300: the tty
# goes raw ~0.05 s after exec; input that early is caught and replayed, then
# pasted input is DROPPED from ~0.25 to ~0.65 s, and from ~0.7 s the composer
# takes it. The drop is inside Claude, long after raw, so raw tells nothing:
# do not simplify readiness back to raw mode. 3 s and not 1, because that
# window is boot time: it stretches with CPU load, and a loaded host or a
# small container boots Claude in 2 to 3 s. The age counts from the wrapper's
# process start, not the exec: natively wclaude execs at once, but in a
# container trust-dir runs first (~0.1-0.3 s), which eats that much margin.
AGENT_INPUT_READY_AGE = 3


class SessionRegistry:
    """The lodge's session registry: one row per session in lodge.db.

    An archived wolt's sessions drop out of every lookup and come back with
    it. db_dir names the lodge whose database holds the rows (default
    wolts_dir): the archive's sweep reads wolts/.archive through the lodge's
    own file.
    """

    def __init__(self, wolts_dir: str | Path = None, *, db_dir: str | Path = None):
        self.wolts_dir = Path(wolts_dir or WOLTS_DIR)
        self.db_dir = Path(db_dir) if db_dir else self.wolts_dir

    # --- Storage ---

    @contextmanager
    def _tx(self):
        """A write transaction, shared by nested registry calls on this thread."""
        key = str(lodge_db.path(self.db_dir))
        conns = _TX.__dict__.setdefault("conns", {})
        if key in conns:
            yield conns[key]
            return
        with lodge_db.write(self.db_dir) as conn:
            conns[key] = conn
            try:
                yield conn
            finally:
                del conns[key]

    @contextmanager
    def _reader(self):
        conn = getattr(_TX, "conns", {}).get(str(lodge_db.path(self.db_dir)))
        if conn is not None:
            yield conn
            return
        if lodge_db.path(self.db_dir).exists():
            conn = lodge_db.connect(self.db_dir, readonly=True)
        else:  # no lodge here (yet): no sessions, and no new file
            conn = sqlite3.connect(":memory:", isolation_level=None)
            conn.row_factory = sqlite3.Row
            lodge_db.ensure_schema(conn)
        try:
            yield conn
        finally:
            conn.close()

    def _visible(self, wolt: str) -> bool:
        """False only while the wolt is archived: gone from here, in .archive (wolt_archive)."""
        return bool(wolt) and (
            (self.wolts_dir / wolt).is_dir() or not (self.wolts_dir / ".archive" / wolt).is_dir())

    def _load(self, row) -> dict | None:
        try:
            data = json.loads(row["record"])
            self._normalize_description(data)
            return normalize_session_target(
                data, wolts_dir=self.wolts_dir, fallback_wolt=row["wolt"]
            )
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def _row(self, conn, name: str, wolt: str | None = None) -> dict | None:
        row = conn.execute("SELECT wolt, record FROM sessions WHERE name=?", (name,)).fetchone()
        if row is None or (wolt and row["wolt"] != wolt) or not self._visible(row["wolt"]):
            return None
        return self._load(row)

    def _rows(self, conn, wolt: str | None = None) -> list[dict]:
        if wolt:
            rows = conn.execute("SELECT wolt, record FROM sessions WHERE wolt=?", (wolt,))
        else:
            rows = conn.execute("SELECT wolt, record FROM sessions")
        visible = {}
        loaded = (self._load(row) for row in rows
                  if visible.setdefault(row["wolt"], self._visible(row["wolt"])))
        return [data for data in loaded if data is not None]

    def _put(self, conn, data: dict) -> None:
        normalized = normalize_session_target(
            data, wolts_dir=self.wolts_dir, fallback_wolt=data.get("wolt", "")
        )
        # The reason belongs to the orphaned state and dies with it. Enforced
        # here rather than at each writer because there are many ways back to
        # running — adoption, three resume branches — and a record that says
        # `running` while still carrying `orphaned_reason` is a record that
        # lies to whoever reads it next.
        if normalized.get("status") != "orphaned":
            normalized.pop("orphaned_reason", None)
        # Same rule for a hold: it covered a task running in this session, and
        # that task died with it. Every write that leaves running (rest, death,
        # stop, finish, orphaned) drops it, so a woken session never inherits one.
        if normalized.get("status") not in ("running", STOPPING):
            normalized.pop("hold", None)
        put_session(conn, normalized)

    def _read(self, wolt: str, name: str) -> dict | None:
        """One stored record, without liveness."""
        with self._reader() as conn:
            return self._row(conn, name, wolt)

    def _write(self, wolt: str, name: str, data: dict) -> None:
        """Store one record as given: a raw write, no last_activity stamp."""
        with self._tx() as conn:
            self._put(conn, {**data, "name": name, "wolt": data.get("wolt") or wolt})

    @contextmanager
    def edit(self, name: str, *, wolt: str = None):
        """Read-modify-write one session in a single write transaction.

        Yields the record, or None when there is none. Changes made to it are
        written when the block ends; an exception writes nothing. Nothing else
        can write any session in between, so a check inside the block and the
        write it guards cannot be separated. Keep the block short: every other
        lodge writer waits for it.
        """
        with self._tx() as conn:
            data = self._row(conn, name, wolt)
            before = json.dumps(data, sort_keys=True, default=str)
            yield data
            if data is not None and json.dumps(data, sort_keys=True, default=str) != before:
                self._put(conn, data)

    def claim(self, name: str, seen: dict, status: str, *, wolt: str = None) -> bool:
        """Move a session to `status` only if what decides a stop is still as `seen`.

        Compares status, last_activity, hold and the resume id, never the
        whole record: a title or viewport written meanwhile must not cancel a
        rest. False when a use, a hold or a resume came first.
        """
        with self.edit(name, wolt=wolt) as current:
            if not still_as_seen(current, seen):
                return False
            current["status"] = status
        return True

    def records(self, *, wolt: str = None) -> list[dict]:
        """Every visible session as stored, with no liveness work."""
        with self._reader() as conn:
            return self._rows(conn, wolt)

    def taken_resume_ids(self, exclude: str = "") -> set[str]:
        """Conversation ids already owned by another session."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT harness_session_id FROM sessions"
                " WHERE harness_session_id IS NOT NULL AND name != ?", (exclude,))
            return {row[0] for row in rows}

    # --- Core API ---

    @staticmethod
    def _normalize_description(data: dict) -> dict:
        """Expose the original opening prompt separately from a description.

        Existing records stored the opening prompt only as ``prompt``.  Keep
        that field for compatibility, while giving the lodge a stable field it
        can use as the italic fallback when a session has not described itself.
        This is deliberately a read-compatible migration: records are updated
        on their next ordinary write, with no eager colony-wide rewrite.
        """
        if "prompt_preview" not in data:
            data["prompt_preview"] = str(data.get("prompt") or "")[:500]
        data.setdefault("title", "")
        data.setdefault("summary", "")
        if not data["summary"] and data["title"] == _title_from_prompt(data.get("prompt") or ""):
            data["title"] = ""
        return data

    def create(
        self,
        name: str,
        *,
        wolt: str = "",
        creature: str = "",
        model: str = "",
        harness: str = "",
        dir: str = "",
        app: str = "",
        title: str = "",
        summary: str = "",
        prompt: str = "",
        adapter: str = "",
        chat_id: str = "",
        user_id: str = "",
        thread_ts: str = "",
        session_url: str = "",
        target: SessionTarget | None = None,
        execution_policy: ExecutionPolicy | dict | str | None = None,
        auto_grant: dict | None = None,
    ) -> dict:
        """Create a new session entry. Returns the full session dict."""
        if not wolt:
            raise ValueError("wolt is required for session creation")
        now = int(time.time())
        if target is None:
            target = SessionTarget.from_record(
                {"wolt": wolt, "dir": dir},
                wolts_dir=self.wolts_dir,
                fallback_wolt=wolt,
            )
        data = {
            "name": name,
            "wolt": wolt,
            "creature": creature,
            "model": model,
            # harness the session was born on — fixed for life (resume must use
            # the same harness; conversation state doesn't transfer). Missing
            # field on old sessions means claude.
            "harness": harness or DEFAULT_HARNESS,
            "app": app,
            "status": "running",
            "created_at": now,
            "finished_at": None,
            "exit_code": None,
            "target": target.to_record(),
            "wolt_id": target.wolt_id,
            "workdir": str(target.canonical_workdir),
            "dir": str(target.canonical_workdir),
            "execution_policy": ExecutionPolicy.from_record(
                execution_policy
            ).to_record(),
            "auto_grant": auto_grant,
            "title": title,
            "summary": summary,
            "prompt_preview": prompt[:500],
            "prompt": prompt[:500],
            "last_activity": now,
            # routing — array for multi-adapter support
            "routing": [],
            # viewport — stored in session JSON, no separate files
            "viewport_url": "",
            "viewport_port": 7777,
            "viewport_updated": 0,
            "session_url": session_url,
            # redirect — stored in session JSON
            "redirect_to": None,
        }
        # Add initial routing entry if adapter provided
        if adapter:
            entry = {"adapter": adapter}
            if chat_id:
                entry["chat_id"] = chat_id
            if user_id:
                entry["user_id"] = user_id
            if thread_ts:
                entry["thread_ts"] = thread_ts
            data["routing"].append(entry)
        # Backwards compat — keep flat fields for existing consumers
        data["adapter"] = adapter
        data["chat_id"] = chat_id
        data["user_id"] = user_id
        data["thread_ts"] = thread_ts

        with self._tx() as conn:
            self._put(conn, data)
        return data

    def describe(self, name: str, title: str, summary: str, *, wolt: str = None) -> dict | None:
        """Set the short, human-facing description for a session."""
        return self.update(
            name,
            wolt=wolt,
            title=title,
            summary=summary,
        )

    def update(self, name: str, *, wolt: str = None, **fields) -> dict | None:
        """Update fields on an existing session. Returns updated dict or None.

        Merges into the stored record inside one write transaction, so
        concurrent writers touching different fields don't clobber each other.
        """
        with self.edit(name, wolt=wolt) as data:
            if data is None:
                return None
            data.update(fields)
            data["last_activity"] = int(time.time())
        return data

    def touch(self, name: str, *, wolt: str = None) -> bool:
        """Stamp last_activity on a running session. Returns True if stamped.

        Only while running, in the UPDATE itself: a touch racing a rest must
        never write running back over resting while tmux is gone.
        """
        now = int(time.time())
        with self._tx() as conn:
            row = conn.execute("SELECT wolt FROM sessions WHERE name=?", (name,)).fetchone()
            if row is None or (wolt and row["wolt"] != wolt) or not self._visible(row["wolt"]):
                return False
            return conn.execute(
                "UPDATE sessions SET last_activity=?, updated_at=?,"
                " record=json_set(record, '$.last_activity', ?)"
                " WHERE name=? AND status='running'",
                (now, now, now, name),
            ).rowcount == 1

    def finish(self, name: str, exit_code: int, *, wolt: str = None) -> dict | None:
        """Mark a session as completed/failed."""
        status = "completed" if exit_code == 0 else "failed"
        return self.update(name, wolt=wolt, status=status, exit_code=exit_code, finished_at=int(time.time()))

    def get(self, name: str, *, wolt: str = None, check_alive: bool = True) -> dict | None:
        """Get session data, optionally checking liveness.

        check_alive reports the same three fields as `list()`, and means the
        same thing by them — one definition of alive, whether you ask about one
        session or all of them:

          tmux_alive  — tmux still holds the session (the historical `alive`).
          agent_alive — True/False, or None when undetermined (the process
                        table could not be read).
          alive       — agent_alive, with None falling back to tmux presence.

        Asking tmux alone is what let a husk — a tmux session whose agent died,
        leaving the login shell holding it open — read as a healthy running
        session here. `check_session` then showed it alive with pane output
        while `list()` called the same session orphaned, and `deliver_message`
        pasted into its shell.

        `alive` is deliberately the permissive answer: a ps hiccup must not
        report the whole colony offline. Callers about to do something
        destructive — delivery pastes text a shell would EXECUTE — must gate on
        `agent_alive is True` instead, and deliver_message does.
        """
        with self._reader() as conn:
            data = self._row(conn, name, wolt)
        if data is None:
            return None
        if check_alive:
            tmux_alive = _tmux_alive(data)
            # No panes means no processes: definitively agentless, and worth
            # skipping the ps fork for. Launching shims count as an agent here
            # so a session mid-boot does not read as orphaned — same as list().
            agent_alive = (
                session_has_agent_process(data, resolve_harness(data.get("harness")))
                if tmux_alive else False
            )
            data["tmux_alive"] = tmux_alive
            data["agent_alive"] = agent_alive
            data["alive"] = tmux_alive if agent_alive is None else agent_alive
            if data["status"] == "running" and not data["alive"]:
                data["status"] = "orphaned"
                data["orphaned_reason"] = (
                    ORPHAN_TMUX_MISSING if not tmux_alive else ORPHAN_AGENT_GONE
                )
        return data

    def set_viewport(self, name: str, url: str, *, wolt: str = None, port: int = 7777) -> dict | None:
        """Update the viewport URL for a session.

        For app URLs (/app/{name}/...), viewport_port is the primary
        field — split.html uses it to connect directly to the app port,
        bypassing the FastAPI proxy. If port is not explicitly given, the
        running app's port is looked up automatically.
        """
        if port == 7777:
            app_match = re.match(r"^/app/([^/]+)", url)
            if app_match:
                try:
                    from apps import running_apps
                    app_name = app_match.group(1)
                    running = {r["name"]: r for r in running_apps()}
                    if app_name in running:
                        port = running[app_name]["port"]
                except Exception:
                    pass

        return self.update(
            name,
            wolt=wolt,
            viewport_url=url,
            viewport_port=port,
            viewport_updated=int(time.time() * 1000),
        )

    def set_redirect(self, from_session: str, to_session: str, *, wolt: str = None) -> dict | None:
        """Set a redirect on a session (for session handoff)."""
        return self.update(from_session, wolt=wolt, redirect_to=to_session)

    def clear_redirect(self, name: str, *, wolt: str = None) -> str | None:
        """Read and clear a pending redirect. Returns target session name or None."""
        with self.edit(name, wolt=wolt) as data:
            if not data or not data.get("redirect_to"):
                return None
            target = data["redirect_to"]
            data["redirect_to"] = None
            data["last_activity"] = int(time.time())
        return target

    def list(self, *, alive_only: bool = False, wolt: str = None) -> list[dict]:
        """List sessions, sorted by created_at desc.

        If wolt is given, only list that wolt's sessions.
        Otherwise, scan all wolts.

        Liveness is reported three ways, because "is it alive" has two honest
        answers and callers want different ones:

          tmux_alive  — tmux still holds the session (the historical meaning).
          agent_alive — an agent process, or its launching shim, is somewhere
                        in that session's process tree.
          alive       — agent_alive. This is what the lists key off, and the
                        reason: a died or failed-to-respawn agent leaves a bare
                        login shell in a live tmux session, which the old
                        tmux-only check reported as alive. The TUI then
                        *attached* to it, dropping the human into an agentless
                        shell instead of resuming the conversation.

        Both flags come from two process calls for the whole list, not two per
        session (see sessions_with_agent_process).
        """
        live_sessions = _tmux_sessions()
        # None = undetermined (unreadable process table). Never read as dead:
        # a ps hiccup must not mark the whole colony offline and send every
        # Enter through a resume.
        agent_sessions = sessions_with_agent_process()
        results = []

        with self._reader() as conn:
            records = self._rows(conn, wolt)
        for data in records:
            name = data.get("name", "")
            tmux_name = RuntimeHandle.from_record(data).tmux_session_name or name
            tmux_alive = tmux_name in live_sessions
            agent_alive = (
                tmux_alive if agent_sessions is None
                else tmux_name in agent_sessions
            )
            data["tmux_alive"] = tmux_alive
            data["agent_alive"] = agent_alive
            data["alive"] = agent_alive
            if data["status"] == "running" and not agent_alive:
                data["status"] = "orphaned"
                data["orphaned_reason"] = (
                    ORPHAN_TMUX_MISSING if not tmux_alive else ORPHAN_AGENT_GONE
                )
            if alive_only and not agent_alive:
                continue
            results.append(data)

        return sorted(results, key=lambda s: s.get("created_at") or 0, reverse=True)

    def list_lodge_view(
        self, *, recent_seconds: int = 86400, per_wolt: int = 8,
    ) -> dict:
        """Return the small, liveness-aware session projection used by the lodge.

        The full registry remains available through ``list()``. This view reads
        only display/state fields, keeps the newest rows needed by each wolt
        expander, and never performs liveness work for terminal records.
        """
        with self._reader() as conn:
            records = [_lodge_record(data) for data in self._rows(conn)]

        totals: dict[str, int] = {}
        by_wolt: dict[str, list[dict]] = {}
        for record in records:
            wolt = record["wolt"]
            totals[wolt] = totals.get(wolt, 0) + 1
            by_wolt.setdefault(wolt, []).append(record)

        now = int(time.time())
        cutoff = now - recent_seconds
        selected: list[dict] = []
        for wolt_records in by_wolt.values():
            ordered = sorted(
                wolt_records,
                key=lambda row: row.get("last_activity") or row.get("created_at") or 0,
                reverse=True,
            )
            newest = {row["name"] for row in ordered[:per_wolt]}
            selected.extend(
                row for row in ordered
                if row.get("status") == "running"
                or (row.get("last_activity") or row.get("created_at") or 0) >= cutoff
                or row["name"] in newest
            )

        # Lodge state has one deliberately lightweight meaning: an "open"
        # session is a running record whose tmux window still exists. Whether
        # its agent process survived is checked only when that session is
        # opened, where a missing agent can be resumed before attachment.
        live_sessions = _tmux_sessions()
        for row in selected:
            tmux_name = row.pop("_tmux_session_name", row["name"])
            alive = row.get("status") == "running" and tmux_name in live_sessions
            row["alive"] = alive
            if row.get("status") == "running" and not alive:
                row["status"] = "orphaned"

        selected.sort(
            key=lambda row: (
                0 if row.get("status") == "running" else 1,
                -(row.get("last_activity") or row.get("created_at") or 0),
            )
        )
        return {"sessions": selected, "totals": totals}

    def reconcile(self) -> list[str]:
        """Check all 'running' sessions against tmux. Mark dead ones as orphaned."""
        live_sessions = _tmux_sessions()
        orphaned = []
        with self._tx() as conn:
            for data in self._rows(conn):
                if data.get("status") == "running" and data.get("name") not in live_sessions:
                    data["status"] = "orphaned"
                    data["orphaned_reason"] = ORPHAN_TMUX_MISSING
                    data["last_activity"] = int(time.time())
                    self._put(conn, data)
                    orphaned.append(data["name"])
        return orphaned

    def adopt_runtime_sessions(self) -> dict:
        """Reconcile only registered resumable sessions after control-plane boot.

        Registry records are the authority: this deliberately does not enumerate
        tmux or import unmanaged sessions. Live registered runtimes become running
        and refresh their exact agent pane when it can be resolved. Missing
        runtimes become orphaned. Terminal records remain untouched.
        """
        report = {
            "at": int(time.time()),
            "adopted": [],
            "orphaned": [],
            "unchanged": [],
            "settled": [],
        }
        runtime = _runtime()
        for wolt in self._all_wolts():
            with self._reader() as conn:
                rows = conn.execute(
                    "SELECT name, wolt, record FROM sessions WHERE wolt=? ORDER BY name", (wolt,)
                ).fetchall()
            for row in rows:
                data = self._load(row)
                if data is None:
                    report["unchanged"].append({
                        "session": row["name"],
                        "wolt": wolt,
                        "reason": "unreadable registry record",
                    })
                    continue

                name = data.get("name") or row["name"]
                previous = data.get("status") or ""
                if previous == STOPPING:
                    report["settled"].append(self._finish_stop(name, wolt, data, runtime))
                    continue
                if previous not in {"running", "orphaned"}:
                    report["unchanged"].append({
                        "session": name,
                        "wolt": wolt,
                        "status": previous,
                        "reason": "terminal record",
                    })
                    continue

                handle = RuntimeHandle.from_record(data)
                if not runtime.is_alive(handle):
                    if previous != "orphaned":
                        with self.edit(name, wolt=wolt) as current:
                            if current is not None:
                                current["status"] = "orphaned"
                                current["orphaned_reason"] = ORPHAN_RUNTIME_GONE
                    report["orphaned"].append({
                        "session": name,
                        "wolt": wolt,
                        "previous_status": previous,
                        "reason": ORPHAN_RUNTIME_GONE,
                        "runtime": handle.to_record(),
                    })
                    continue

                resolved = resolve_agent_handle(
                    handle,
                    harness=data.get("harness"),
                    include_launching=True,
                )
                adopted_handle = resolved or handle
                changed = previous != "running" or adopted_handle != handle
                if changed:
                    with self.edit(name, wolt=wolt) as current:
                        if current is not None:
                            current["status"] = "running"
                            current["runtime"] = adopted_handle.to_record()
                report["adopted"].append({
                    "session": name,
                    "wolt": wolt,
                    "previous_status": previous,
                    "runtime": adopted_handle.to_record(),
                    "handle_refreshed": adopted_handle != handle,
                })
        return report

    def _finish_stop(self, name: str, wolt: str, data: dict, runtime) -> dict:
        """Settle a session a control plane left `stopping` when it died."""
        handle = RuntimeHandle.from_record(data)
        if runtime.is_alive(handle):
            runtime.stop(handle)
        now = int(time.time())
        with self.edit(name, wolt=wolt) as current:
            if current is not None and current.get("status") == STOPPING:
                if stored_resume_id(current):
                    current.update(status="resting", rested_at=now,
                                   rest_reason=current.get("rest_reason") or "interrupted")
                else:
                    current.update(status="stopped", finished_at=now)
        return {"session": name, "wolt": wolt, "status": (current or {}).get("status")}

    def delete(self, name: str, *, wolt: str = None) -> bool:
        """Remove a session. Returns True if it existed."""
        with self._tx() as conn:
            if self._row(conn, name, wolt) is None:
                return False
            conn.execute("DELETE FROM sessions WHERE name=?", (name,))
        return True

    def forget_wolt(self, wolt: str) -> int:
        """Remove every session of a wolt, visible or not. Returns how many."""
        with self._tx() as conn:
            return conn.execute("DELETE FROM sessions WHERE wolt=?", (wolt,)).rowcount

    def _all_wolts(self) -> list[str]:
        """Every visible wolt that has sessions, sorted."""
        with self._reader() as conn:
            wolts = [row[0] for row in conn.execute("SELECT DISTINCT wolt FROM sessions ORDER BY wolt")]
        return [wolt for wolt in wolts if self._visible(wolt)]


# --- Helpers ---

def _tmux_sessions() -> set[str]:
    """Every live session name, in one call.

    The batched form of _tmux_alive and the same definition — a session is
    live while it has any pane — so list()/reconcile() and get(check_alive)
    can never disagree about the same session.
    """
    return _runtime().list_session_names()


def _runtime():
    """The shared process-control boundary (see session_runtime.get_runtime)."""
    return get_runtime()


def _runtime_handle(session: str | dict | RuntimeHandle) -> RuntimeHandle:
    if isinstance(session, RuntimeHandle):
        return session
    if isinstance(session, dict):
        return RuntimeHandle.from_record(session)
    data = SessionRegistry(WOLTS_DIR).get(session, check_alive=False)
    return RuntimeHandle.from_record(data or {"name": session})


def _tmux_alive(session: str | dict | RuntimeHandle) -> bool:
    """Whether the session's tmux session exists — one definition, everywhere.

    Session-level, matching list()/reconcile() and the pre-refactor
    has-session check. Pane identity decides where a message is *delivered*
    (resolve_delivery_pane), never whether the session counts as alive.
    """
    return _runtime().is_alive(_runtime_handle(session))


# One paste at a time per session: the tmux paste buffer is per session, so
# two pastes at once (server threads, a chat bot, the CLI) can lose or merge a
# message. A flock, so it holds across processes, on one of PASTE_LOCKS fixed
# files: a session's file is a stable hash of its name, and two sessions that
# share one only wait for each other. Best-effort, like any lock file here: a
# filesystem without flock falls through. Hold it around _tmux_paste alone,
# never inside a lodge_db write.
PASTE_LOCKS = 32


@contextmanager
def _paste_lock(session: str, wolts_dir: Path):
    k = int(hashlib.sha256(session.encode()).hexdigest(), 16) % PASTE_LOCKS
    lock = space_dir(Path(wolts_dir)) / "run" / "paste" / f"{k}.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_WRONLY, 0o600)
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError:
            pass
        yield
    finally:
        os.close(fd)


def _tmux_paste(target: str | dict | RuntimeHandle, text: str, settle: float = 0.0):
    """Paste text into a tmux pane and press Enter.

    Uses set-buffer + paste-buffer instead of send-keys -l.
    send-keys -l sends each character as an individual keystroke which
    blocks on long messages (the pane input buffer backs up). Buffer
    paste delivers the entire text atomically — same as a clipboard
    paste from a human.

    Enter is sent as a separate send-keys call after the paste. Claude
    Code's TUI has paste-aware input: a \\n inside a paste is treated
    as a literal newline (for multi-line input) rather than submit.
    A standalone Enter keystroke arriving after the paste completes
    is the canonical "submit" signal.

    Exits copy-mode on the target pane first. With `mouse on`, scrolling
    up in a pane auto-enters copy-mode — paste-buffer to a copy-mode
    pane visibly inserts the text but it never reaches the underlying
    process, and the Enter keystroke is consumed as a copy-mode command.
    `send-keys -X cancel` is a no-op when the pane isn't in a mode,
    errors harmlessly which we ignore via check=False.

    Uses a named buffer (the target session name) so concurrent pastes
    to different sessions don't clobber each other.  The -d flag on
    paste-buffer deletes the named buffer after pasting.

    The paste is bracketed (paste-buffer -p), so a TUI that enabled
    bracketed paste sees exactly where the message ends and the Enter
    submits it. See TmuxSessionRuntime.paste.

    settle: seconds to wait between the paste and the Enter keystroke.
    Codex's TUI folded an immediate Enter into an unbracketed paste, and
    Claude drops one while it attaches pasted image paths — every harness
    entry keeps paste_settle=0.5.
    """
    _runtime().paste(_runtime_handle(target), text, settle=settle)


def _tmux_capture(target: str | dict | RuntimeHandle, start: str | None = "-30") -> str:
    """Capture one named session through its exact persisted runtime handle.

    start is the tmux -S history offset; None captures the visible pane only.
    """
    return _runtime().capture(_runtime_handle(target), start=start)


def _tmux_spawn(name: str, cwd: str, command: str) -> RuntimeHandle:
    """Spawn and return the exact handle to persist on the session record."""
    return _runtime().spawn(name, cwd, command)


def _tmux_spawn_in_session(
    handle: RuntimeHandle, cwd: str, command: str
) -> RuntimeHandle:
    """Create a dedicated execution surface inside a surviving session."""
    return _runtime().spawn_in_session(handle, cwd, command)


def _tmux_stop(session: str | dict | RuntimeHandle) -> bool:
    """Stop one exact named tmux session."""
    return _runtime().stop(_runtime_handle(session))


def _session_scope(session: str | dict | RuntimeHandle) -> RuntimeHandle:
    """The same address with the pane dropped — "anywhere in this session".

    Liveness walks narrow to the persisted pane while that pane exists, which
    is the right answer for delivery (paste where the agent actually is) and
    the wrong one for "is anything running in here at all". Killing a session
    is a session-level act, so the question in front of it has to be too.
    """
    return _runtime_handle(session).at_pane("")


def _await_agent(session: str | dict | RuntimeHandle, harness: str | None,
                 timeout: float | None = None) -> bool:
    """Poll until a launching-or-live agent shows up in this session. True/False.

    A relaunch is fire-and-forget — tmux returns the moment the pane exists,
    long before run-session.sh has decided whether it can start anything. That
    gap is where a resume used to be reported as a success and the human got
    attached to a dying shell. Launching processes count, so this waits for
    evidence of a launch, not for the model to be ready.

    Read from the module globals at call time so a test can shorten them.
    """
    limit = _AGENT_APPEAR_TIMEOUT if timeout is None else timeout
    deadline = time.monotonic() + limit
    while True:
        if resolve_agent_handle(session, harness, include_launching=True) is not None:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_AGENT_POLL_INTERVAL)


def _input_ready(handle: RuntimeHandle) -> bool | None:
    """One look at whether the program in this exact pane has its terminal raw.

    A runtime without the probe answers None — unknown — which pastes exactly
    as delivery did before the probe existed.
    """
    probe = getattr(_runtime(), "input_ready", None)
    return probe(handle) if probe is not None else None


def _agent_age(handle: RuntimeHandle, harness: str | None) -> int | None:
    """Seconds the youngest agent process in this exact pane has been up, or None.

    A runtime without the probe answers None, read like an unreadable tty.
    """
    probe = getattr(_runtime(), "agent_age", None)
    if probe is None:
        return None
    return probe(handle, get_harness(harness)["process_names"])


def _await_input_ready(target: str | dict | RuntimeHandle, harness: str | None,
                       timeout: float | None = None) -> RuntimeHandle | None:
    """Wait until the agent in this session takes input: the pane to paste into, or None.

    Finding the agent process is not enough, and neither is a raw terminal.
    Claude Code takes its tty raw about 0.05 s after exec, then drops pasted
    input until its composer mounts, around 0.7 s on an idle host and later on
    a loaded one (AGENT_INPUT_READY_AGE has the measurements). A paste in that
    gap is lost without a trace. So ready is: the agent process found
    (launching excluded) and the youngest agent process in its pane at least
    AGENT_INPUT_READY_AGE old (`agent_age`, read off the process: a revive puts
    a new pid under an old record).

    The terminal mode is read once the agent is old enough, and only reported.
    Raw is pasted into as usual. A cooked or unreadable pane is pasted into
    too, with one log line: a cooked reader buffers a paste and its Enter
    fine, and a harness that never goes raw (hermes and pi are unmeasured)
    must not have every message refused.

    An agent already old at the first look returns at once, which is nearly
    every message. A young one sleeps out the rest of its age, inside the
    same timeout. An age that can't be read is pasted into straight away, as
    before the probe existed.

    None means paste nothing: no agent was found within the timeout (still
    launching), or nothing is booting to wait for — no agent and no launching
    shim, which the caller's own checks then name (agent-gone, session-dead).

    Never call this inside registry.edit(). It can run for the whole timeout,
    and every lodge writer queues on that transaction.

    Read from the module globals at call time so a test can shorten them.
    """
    limit = INPUT_READY_TIMEOUT if timeout is None else timeout
    name = _runtime_handle(target).woltspace_session_id
    started = time.monotonic()
    waited = False
    while True:
        pause = _INPUT_READY_POLL
        found = resolve_agent_handle(target, harness, include_launching=False)
        if found is not None:
            age = _agent_age(found, harness)
            if age is None:
                print(f"[sessions] {name}: can't read the age of the {harness} "
                      f"agent in pane {found.pane_id}; pasting without waiting "
                      f"for input", file=sys.stderr)
                return found
            if age >= AGENT_INPUT_READY_AGE:
                raw = _input_ready(found)
                if raw is None:
                    print(f"[sessions] {name}: the {harness} agent is old enough but "
                          f"the terminal mode of pane {found.pane_id} can't be read; "
                          f"pasting", file=sys.stderr)
                elif not raw:
                    print(f"[sessions] {name}: the {harness} agent is old enough but "
                          f"its terminal is not raw; pasting", file=sys.stderr)
                elif waited:
                    print(f"[sessions] {name}: the {harness} agent took input after "
                          f"{time.monotonic() - started:.2f}s", file=sys.stderr)
                return found
            # Young: inside the window where Claude drops input. Sleep out
            # the rest of the age rather than poll through it.
            pause = AGENT_INPUT_READY_AGE - age
        elif resolve_agent_handle(target, harness, include_launching=True) is None:
            if waited:
                print(f"[sessions] {name}: stopped waiting after "
                      f"{time.monotonic() - started:.2f}s; nothing is booting",
                      file=sys.stderr)
            return None
        elapsed = time.monotonic() - started
        if elapsed >= limit:
            print(f"[sessions] {name}: the {harness} agent was not taking input after "
                  f"{elapsed:.2f}s; not pasting", file=sys.stderr)
            return None
        waited = True
        time.sleep(min(pause, limit - elapsed))


def _guard_paste_text(harness: str | None, text: str) -> str:
    """Defuse harness-specific paste quirks. Pure no-op unless the harness
    opts in via a flag, so shared paste paths (IWCL, resume) are unchanged
    for claude/codex.

      - flatten_paste_newlines (opencode): opencode's TUI drops newlines from a
        pasted message entirely — it joins the lines with NO separator, so a
        multi-line attributed IWCL message renders as jammed-together run-on
        text ("...session=X]body..."). Flatten \\n → space so word boundaries
        survive. claude/codex TUIs are paste-aware (a pasted \\n stays a literal
        newline), so they must NOT flatten — the message renders multi-line.
      - leading_slash_opens_palette (opencode): a message starting with "/"
        opens the command palette and never submits. A leading space makes the
        composer treat it as literal text (benched live 2026-08-08).
    """
    entry = get_harness(harness)
    t = text.replace("\n", " ") if entry.get("flatten_paste_newlines") else text
    if entry.get("leading_slash_opens_palette") and t.startswith("/"):
        t = " " + t
    return t


# ---------------------------------------------------------------------------
# IWCL — Inter-Wolt Communication. Deliver a message into a running session,
# with attribution. The one primitive behind the wolt-to-wolt relay: a wolt
# posts into another wolt's session and the reply comes back into its own.
# Every message carries the sender's wolt name + session id so the receiver
# can reply to the right session. Same contract the telegram bot uses
# (prepended context), just wolt-to-wolt. Delivery is harness-aware (reuses
# _tmux_paste + paste_settle).
# ---------------------------------------------------------------------------

def _iwcl_reply_instruction(from_session: str) -> str:
    """Return a safe reply heredoc, or log why corrupt metadata was omitted."""
    if not from_session:
        return ""
    try:
        return f"\n{session_send_reply_instruction(from_session)}"
    except ValueError as exc:
        print(
            f"[sessions] omitted IWCL reply for invalid sender session "
            f"{from_session!r}: {exc}",
            file=sys.stderr,
        )
        return ""


def format_attributed_message(text: str, from_wolt: str = "",
                              from_session: str = "") -> str:
    """Wrap a message with sender attribution + a reply instruction.

    Unattributed (no from_wolt) returns the text unchanged — e.g. a plain
    system nudge. With a sender, the receiver sees who sent it and, when a
    session id is known, exactly how to reply.
    """
    if not from_wolt:
        return text
    header = f"[message from {from_wolt}"
    if from_session:
        header += f", session={from_session}"
    header += "]"
    reply = _iwcl_reply_instruction(from_session)
    return f"{header}\n{text}{reply}"


def format_spawned_prompt(text: str, from_wolt: str = "",
                          from_session: str = "") -> str:
    """Wrap a spawned session's seed prompt with spawner attribution.

    The spawn counterpart of format_attributed_message: the child session
    learns who created it and, when the spawner has a session, exactly how to
    IWCL back — which is what makes spawn + send compose into delegation.
    Unattributed (no from_wolt) returns the text unchanged — e.g. a lodge UI
    or wolf scheduler spawn.
    """
    if not from_wolt:
        return text
    header = f"[spawned by {from_wolt}"
    if from_session:
        header += f", session={from_session}"
    header += "]"
    reply = _iwcl_reply_instruction(from_session)
    body = f"\n{text}" if text else ""
    return f"{header}{body}{reply}"


def resolve_active_session(wolt: str, registry=None) -> str | None:
    """Return the wolt's most-recently-active LIVE session name, or None.

    Addressing by wolt name resolves to the session a human/wolt would expect
    to reach — the one that's been active most recently.
    """
    reg = registry or SessionRegistry()
    alive = reg.list(alive_only=True, wolt=wolt)
    if not alive:
        return None
    alive.sort(
        key=lambda s: (s.get("last_activity") or 0, s.get("created_at") or 0),
        reverse=True,
    )
    return alive[0]["name"]


def deliver_message(session_id: str, text: str, from_wolt: str = "",
                    from_session: str = "", registry=None) -> dict:
    """Deliver a message into a running session, with optional attribution.

    Returns {"status": ..., "session": session_id} where status is:
      delivered     — pasted into the pane an agent was actually found in
      agent-booting — an agent is starting in the session but was not taking
                      input by the end of the wait; nothing was pasted
      agent-gone    — tmux holds the session but no agent is in it
      session-dead  — the tmux session itself is gone
      no-session    — no such session

    Delivery is the one place liveness has teeth. A paste is not a read: the
    text goes to whatever owns the pane, and a login shell left behind by a
    dead agent will happily EXECUTE an IWCL message. So this refuses on
    anything short of a positive answer:

      * `agent_alive is not True` — False, and also None. `alive` falls back
        to tmux presence when the process table is unreadable, which is right
        for a list but wrong here: undetermined must not become "paste it and
        hope". The conservative side is cheap and self-healing — the caller
        sees agent-gone and resumes, and a resume that finds a live agent just
        delivers the prompt itself.
      * a *booting* agent — a launching one is counted as alive everywhere
        else, so a booting session is not reported orphaned, but a TUI that
        has not mounted its input yet loses a paste. Delivery first waits for
        the agent to take input (_await_input_ready), then finds the agent
        again without waiting; its age only grows, so that is not asked again. Detection
        uses include_launching=False and hands back the exact pane, so a
        session found in one window can never be pasted into another.

    agent-gone is deliberately distinct from session-dead: the tmux session is
    still there, so a caller can resume-then-retry rather than concluding the
    session is beyond reach. agent-booting is distinct from both: the wait has
    already been spent, and a resume on top of it would paste a relaunch into
    the pane the agent is booting in.

    Delivery is harness-aware: it reuses _tmux_paste with the target harness's
    paste_settle (every engine, claude included, pauses before the Enter).
    """
    reg = registry or SessionRegistry()
    existing = wait_out_stopping(reg, session_id)
    if existing is None:
        return {"status": "no-session", "session": session_id}
    wolt = existing.get("wolt", "")
    ready = _await_input_ready(existing, resolve_harness(existing.get("harness")))
    data = reg.get(session_id, wolt=wolt)
    if data is None:
        return {"status": "no-session", "session": session_id}
    if not data.get("tmux_alive", data.get("alive")):
        return {"status": "session-dead", "session": session_id}
    harness = resolve_harness(data.get("harness"))
    if data.get("agent_alive") is not True:
        return {
            "status": "agent-gone",
            "session": session_id,
            "detail": (
                "tmux session is up but could not be confirmed to hold a live "
                f"{harness} agent — resume it and retry"
            ),
        }
    target = resolve_agent_handle(data, harness, include_launching=False)
    if target is None or ready is None:
        return {
            "status": "agent-booting",
            "session": session_id,
            "detail": "the agent is still booting",
        }
    # The delivery claims the session before it pastes, as a rest does: one
    # guarded UPDATE of last_activity WHERE status='running' (touch). A rest
    # claims by comparing that last_activity, so whichever commits first
    # wins: this one, and the rest's claim loses; the rest's, and this finds
    # no running row, waits the stop out, and the caller wakes the session.
    if not reg.touch(session_id, wolt=wolt):
        wait_out_stopping(reg, session_id)
        return {"status": "session-dead", "session": session_id}
    settle = get_harness(harness).get("paste_settle", 0.0)
    body = format_attributed_message(text, from_wolt, from_session)
    with _paste_lock(session_id, reg.db_dir):
        _tmux_paste(target, _guard_paste_text(harness, body), settle=settle)
    return {"status": "delivered", "session": session_id, "harness": harness}


# ---------------------------------------------------------------------------
# Session naming
# ---------------------------------------------------------------------------

SESSION_ADJECTIVES = [
    "chompy", "soggy", "toothy", "muddy", "slappy", "chunky", "bushy", "gnarly",
    "burly", "scruffy", "grumpy", "plucky", "scrappy", "sly", "crafty", "sturdy",
    "sleek", "mossy", "gritty", "silty", "damp", "rugged", "brisk", "snug",
    "bold", "keen", "wild", "swift", "broad", "dense", "dusty", "fuzzy",
]
SESSION_NOUNS = [
    "dam", "lodge", "pond", "creek", "bark", "branch", "log", "stump", "knot",
    "chip", "den", "burrow", "bank", "marsh", "grove", "thicket", "hollow",
    "trail", "ford", "eddy", "tail", "paw", "tooth", "fur", "mound", "birch",
    "oak", "elm", "pine", "cedar", "maple", "willow", "bog", "ridge", "brook",
]

# Backwards-compat alias — creature→model mapping now lives per-harness in
# harnesses.py. This is the claude mapping (bot/core.py and tests import it).
CREATURE_MODELS = HARNESSES[DEFAULT_HARNESS]["models"]


def session_name(prefix: str) -> str:
    """Generate a session name: prefix-adjective-noun-6hex."""
    adj = random.choice(SESSION_ADJECTIVES)
    noun = random.choice(SESSION_NOUNS)
    hex6 = f"{random.randint(0, 0xFFFFFF):06x}"
    return f"{prefix}-{adj}-{noun}-{hex6}"


def get_tunnel_url(wolts_dir: Path = None) -> str:
    """Read the tunnel URL from .space/platform/tunnel.json."""
    try:
        import json
        state = json.loads(tunnel_state_file(wolts_dir).read_text())
        return state.get("url", "").strip().rstrip("/")
    except Exception:
        return ""


def build_session_command(name: str, prompt: str = "", *, resume: bool = False) -> str:
    """Build the run-session.sh invocation that tmux (or a revived pane) executes.

    run-session.sh reads everything else — dir, wolt, model, harness, adapter
    routing — from the registry, so the session must already exist there.
    """
    cmd = f"{RUN_SESSION_SCRIPT} {shlex.quote(name)}"
    if resume:
        cmd += " --resume"
    if prompt:
        cmd += f" {shlex.quote(prompt)}"
    return cmd


def _title_from_prompt(prompt: str) -> str:
    """Short descriptive title from a prompt (first line, ~60 chars, clean)."""
    first_line = prompt.split("\n")[0].split(".")[0].strip()
    clean = re.sub(r"[^\w\s-]", "", first_line).strip()
    return clean[:60].lower()


def _adapter_context(data: dict) -> str:
    """Adapter-specific context so the wolt knows how to communicate from the start.

    The connector's codec reads the session's own bound route; the reply
    instruction is built from it (escaped like any notify argv), never from a
    template a connector supplies.
    """
    import connector_registry

    spec = connector_registry.spec(data.get("adapter", ""))
    route = spec.codec.from_record(data) if spec else None
    if not route:
        return ""
    return (
        f"\nThis session was started from {spec.title}.\n"
        f"{notify_reply_instruction(*spec.reply_args(route))}\n"
        f"Session link: {data.get('session_url', '')}"
    )


# The opening prompt is one argument: claude, codex and pi take it on the
# command line, and opencode and hermes get it through `tmux set-buffer`. Linux
# refuses any single argument over MAX_ARG_STRLEN, 131,072 bytes, a kernel
# constant separate from ARG_MAX that ulimit does not change. Boot memory has no
# size limit of its own, so a wolt that outgrew it could not start a session
# ("Argument list too long", #578; on 2026-10-17, 135,361 bytes of boot files
# made a 135,984-byte prompt). Over this many UTF-8 bytes the boot files are
# cut. That is 32 KB under the kernel's limit, and a healthy wolt's whole
# prompt is about 15 KB.
BOOT_PROMPT_MAX_BYTES = 96 * 1024


def _cut_boot_file(path: Path, text: str, above: int, room: int) -> str:
    """`text` cut to at most `room` UTF-8 bytes, a line saying so included.

    `above` is how many lines strip() took off the top of the file. The note
    numbers lines as the file on disk does, so the wolt reads on from the first
    line it was not shown instead of reading the shown ones again.
    """
    raw = text.encode()
    head = (f"[Cut by the lodge: {path.name} is {len(raw)} bytes and the boot "
            f"limit is {BOOT_PROMPT_MAX_BYTES}.")

    def note(shown: int) -> str:
        if not shown:
            return (f"{head} Nothing of it is shown. Read {path} in full, and trim "
                    "your boot memory.]")
        return (f"{head} Lines 1-{shown} of it are shown above. Read {path} from line "
                f"{shown + 1} on, and trim your boot memory.]")

    # Sized with the file's last line number, which no number in the note exceeds.
    keep = room - len(note(above + raw.count(b"\n") + 1).encode())
    # A line break is one byte that never occurs inside a UTF-8 character, so
    # cutting just after one keeps the last line and every character whole.
    end = raw.rfind(b"\n", 0, max(keep, 0)) + 1
    shown = above + raw.count(b"\n", 0, end) if end else 0
    return raw[:end].decode() + note(shown)


def _fit_boot_files(files: list[tuple[Path, str, int]], budget: int) -> list[str]:
    """The files' texts, in order, sharing `budget` UTF-8 bytes fairly.

    Smallest first, each takes at most an equal part of what is left; a file
    that fits stays whole and leaves the rest to the others. So in practice
    identity.md stays whole and the largest file is cut.
    """
    texts = [text for _, text, _ in files]
    by_size = sorted(range(len(texts)), key=lambda i: len(texts[i].encode()))
    for done, i in enumerate(by_size):
        share = budget // (len(texts) - done)
        if len(texts[i].encode()) > share:
            path, _, above = files[i]
            texts[i] = _cut_boot_file(path, texts[i], above, share)
        budget -= len(texts[i].encode())
    return texts


def _wolt_boot_context(data: dict, over: int = 0) -> str:
    """Load the owning wolt's lean boot memory without touching the repo.

    Harnesses run in arbitrary project directories, so cwd-based instruction
    discovery cannot establish wolt identity. The opening message carries the
    three documented boot files directly; HOME and the repository stay intact.

    `over` is how many bytes the opening prompt is over BOOT_PROMPT_MAX_BYTES.
    Everything around the files stays as it is, so the files give those bytes
    up between them, and the cut is reported on stderr.
    """
    wolt = data.get("wolt_id") or data.get("wolt") or ""
    if not wolt:
        return ""
    memory_dir = WOLTS_DIR / wolt / "wolt" / "memory"
    files = []
    for filename in ("identity.md", "context.md", "learnings.md"):
        path = memory_dir / filename
        try:
            text = path.read_text()
        except OSError:
            continue
        content = text.strip()
        if content:
            above = text[:len(text) - len(text.lstrip())].count("\n")
            files.append((path, content, above))
    if not files:
        return ""
    texts = [content for _, content, _ in files]
    if over > 0:
        total = sum(len(text.encode()) for text in texts)
        texts = _fit_boot_files(files, total - over)
        print(f"[sessions] boot memory of {wolt} cut from {total} to "
              f"{sum(len(text.encode()) for text in texts)} bytes "
              f"(limit {BOOT_PROMPT_MAX_BYTES}); trim wolt/memory/", file=sys.stderr)
    sections = [f"## {path.name}\n{text}" for (path, *_), text in zip(files, texts)]
    target = SessionTarget.from_record(data, wolts_dir=WOLTS_DIR, fallback_wolt=wolt)
    return (
        "[Woltspace boot context]\n"
        f"Wolt: {target.wolt_id}\n"
        f"Working directory: {target.canonical_workdir}\n"
        "Persistent boot memory follows; treat it as the owning wolt's context.\n\n"
        + "\n\n".join(sections)
        + "\n[/Woltspace boot context]"
    )


# A platform-skill invocation, in either delivered spelling: a sigil at the
# start of a word, then the woltspace namespace (`woltspace:`) or the
# copy-sync prefix (`woltspace-`), then a name. Anchored on the sigil so a
# prompt that merely TALKS about /woltspace-notify or the woltspace: namespace
# — "rename woltspace-notify", "the woltspace: prefix" — is prose, not a call,
# and still gets its start-chat.
_PLATFORM_INVOKE_RE = re.compile(r"(?:^|\s)[/@]woltspace[-:]\S")


def _invokes_platform_skill(prompt: str, harness: str, delivery: str) -> bool:
    """True if `prompt` already opens a skill, so start-chat must not follow.

    Both delivered spellings are recognized regardless of this wolt's own —
    a caller may hand us either, and appending start-chat to a prompt that is
    already a skill call is the failure worth avoiding.

    opencode under plugin delivery is the one shape with nothing to anchor on:
    its platform skills answer to bare names, so the sigil alone marks a call,
    and only when the prompt opens with it.
    """
    if _PLATFORM_INVOKE_RE.search(prompt):
        return True
    sigil = platform_skill_invoke(harness, "", delivery=delivery).strip()
    if not sigil or sigil.endswith((f"{PLATFORM_SKILL_NAMESPACE}:",
                                    LEGACY_PLATFORM_SKILL_PREFIX)):
        # A namespaced spelling — the regex above is the whole answer.
        return False
    return prompt.lstrip().startswith(sigil)


def _assemble_spawn_prompt(data: dict, prompt: str, harness: str) -> str:
    """Full opening prompt: user's task + adapter context + start-chat invocation.

    Skips start-chat if the prompt already invokes a skill (e.g. create-wolt).
    How start-chat is spelled follows how this wolt's skills were DELIVERED,
    not just which harness it runs: a copy-path wolt has `woltspace-start-chat`
    sitting in its skills directory and has never heard of `woltspace:`.

    Over BOOT_PROMPT_MAX_BYTES the boot memory is cut, never the rest. Nothing
    here raises for size: a raise in `prepare` would end run-session.sh before
    it records the session as failed.
    """
    context = _adapter_context(data)
    wolt = data.get("wolt") or "wolt"
    delivery = wolt_skills_delivery(WOLTS_DIR / wolt)
    if _invokes_platform_skill(prompt, harness, delivery):
        rest = f"{prompt}{context}"
    else:
        adapter = data.get("adapter") or "lodge"
        # The template may carry a leading space of its own (opencode's palette
        # defuse). Landing mid-prompt it is redundant, not harmful — strip it here
        # so the assembled prompt reads cleanly, and leave the guard to the one
        # place it matters: a prompt that OPENS with the invocation.
        start_chat = platform_skill_invoke(harness, "start-chat", delivery=delivery).strip()
        rest = f"{prompt}{context} {start_chat} {adapter} {wolt}"
    boot_context = _wolt_boot_context(data)
    if not boot_context:
        return rest
    over = len(f"{boot_context}\n\n{rest}".encode()) - BOOT_PROMPT_MAX_BYTES
    if over > 0:
        boot_context = _wolt_boot_context(data, over=over)
    return f"{boot_context}\n\n{rest}"


def stored_resume_id(data: dict) -> str:
    """The harness conversation id a --resume would replay, or "".

    harness_session_id is the generic field, written by us at spawn.
    claude_session_id is the pre-harness spelling — old sessions resume through
    the fallback, UUID-validated because some legacy sessions stored non-UUID
    values there.
    """
    resume_id = data.get("harness_session_id") or ""
    if resume_id:
        return resume_id
    legacy = data.get("claude_session_id") or ""
    return legacy if _UUID_RE.match(legacy) else ""


def taken_resume_ids(records: list[dict], exclude: str = "") -> set[str]:
    """Conversation ids already owned by some session, so recovery never shares one."""
    return {rid for d in records if d.get("name") != exclude for rid in [stored_resume_id(d)] if rid}


def claim_resume_id(registry: "SessionRegistry", data: dict, write) -> str:
    """The stored id, or recover one and persist it with `write(data)`.

    Two sessions can never own one conversation: harness_session_id is UNIQUE
    in lodge.db, so a second claimer's write fails with IntegrityError, and it
    looks again with the winner's id now in the taken set.
    """
    existing = stored_resume_id(data)
    if existing:
        return existing
    for _ in range(3):
        recovered = recover_resume_id(data, registry.taken_resume_ids(exclude=data.get("name", "")))
        if not recovered:
            return ""
        data["harness_session_id"] = recovered
        try:
            write(data)
            return recovered
        except sqlite3.IntegrityError:
            data.pop("harness_session_id", None)
    return ""


def stamp_resume_id(registry: "SessionRegistry", wolt: str, name: str, resume_id: str) -> None:
    """Persist a recovered conversation id and nothing else.

    Never update(): update() stamps last_activity, and a recovered id is not
    use. Raises IntegrityError when another session already owns the id.
    """
    with registry.edit(name, wolt=wolt) as current:
        if current is not None and not stored_resume_id(current):
            current["harness_session_id"] = resume_id


def still_as_seen(record: dict | None, seen: dict) -> bool:
    """Nothing that decides a stop or a settle changed since `seen` was read."""
    return record is not None and all(record.get(k) == seen.get(k) for k in _CLAIM_FIELDS)


def stop_and_settle(registry: "SessionRegistry", name: str, seen: dict, stop, settle,
                    *, wolt: str = None) -> bool | None:
    """Stop a session's tmux with no write transaction around the stop.

    A write holds the lodge's one lock, and a slow tmux must not hold up every
    other session. So: claim the record `stopping` if it is still as `seen`
    (the judgment), run stop(seen) outside any write, then settle(record) in a
    write. A message that finds `stopping` waits for it to end, then wakes the
    session (wait_out_stopping), so it never lands in a pane being stopped.
    A failed stop gives back the judged status and nothing else, so what was
    written meanwhile stays. None: the claim lost. False: the stop failed.
    """
    if not registry.claim(name, seen, STOPPING, wolt=wolt):
        return None
    stopped = bool(stop(seen))
    with registry.edit(name, wolt=wolt) as current:
        if current is not None and current.get("status") == STOPPING:
            if stopped:
                settle(current)
            else:
                current["status"] = seen.get("status")
    return stopped


def wait_out_stopping(registry: "SessionRegistry", name: str) -> dict | None:
    """The session's record once no stop is in progress (or after STOPPING_WAIT)."""
    deadline = time.monotonic() + STOPPING_WAIT
    while True:
        data = registry.get(name, check_alive=False)
        if data is None or data.get("status") != STOPPING or time.monotonic() > deadline:
            return data
        time.sleep(0.2)


def recover_resume_id(data: dict, taken: set[str]) -> str:
    """The stored id, or one recovered from the harness's own files, or "".

    Read-only: callers that act on it stamp harness_session_id themselves.
    """
    existing = stored_resume_id(data)
    if existing:
        return existing
    recover = get_harness(resolve_harness(data.get("harness"))).get("recover_session_id")
    return (recover(data, taken) or "") if recover else ""


def prepare_session_command(name: str, mode: str, prompt: str = "") -> str:
    """Build the full agent command for a session — the run-session.sh backend.

    Everything comes from the registry. Spawn also stamps harness_session_id
    (used later for --resume). A session describes itself separately once its
    focus is clear; the opening prompt remains the untitled preview.

    Raises ValueError if the session isn't in the registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    harness = resolve_harness(data.get("harness"))
    wolt = data.get("wolt", "")
    model = data.get("model", "")

    if wolt and (WOLTS_DIR / wolt / "wolt" / "wolt.json").is_file():
        from lodge_skills import sync_lodge_skills
        sync_lodge_skills(WOLTS_DIR, WOLTS_DIR / wolt)

    # Claude and codex both refuse to start in a directory they have not been
    # trusted for, and a headless spawn has nobody to answer the dialog. This
    # is the one seam both modes pass through on their way to run-session.sh —
    # spawn, resume, revive — so the workdir gets trusted exactly once per
    # agent launch. Scoped to the data root; opencode carries its own mechanism.
    trust_writer = {
        "claude": ensure_claude_dir_trusted,
        "codex": ensure_codex_dir_trusted,
    }.get(harness)
    if trust_writer:
        target = SessionTarget.from_record(data, wolts_dir=WOLTS_DIR, fallback_wolt=wolt)
        trust_writer(target.canonical_workdir, WOLTS_DIR)

    if mode == "spawn":
        # Harnesses that accept a preset session id (claude --session-id) get
        # one generated and stamped now. Others (codex) assign their own —
        # run-session.sh discovers it after launch via discover-id.
        session_id = ""
        updates = {}
        if get_harness(harness).get("preset_session_id"):
            session_id = str(uuid.uuid4())
            updates["harness_session_id"] = session_id
        full_prompt = _assemble_spawn_prompt(data, prompt, harness)
        # Harnesses that can't take the boot prompt on the CLI (opencode) get
        # it stamped here and pasted in by deliver_boot_prompt after launch.
        if get_harness(harness).get("prompt_via_paste"):
            updates["pending_boot_prompt"] = full_prompt
            full_prompt = ""
        registry.update(name, wolt=wolt, **updates)
        return build_command(
            harness, "spawn",
            session_id=session_id, session_name=name,
            model=model, prompt=full_prompt,
            execution_policy=data.get("execution_policy"),
        )

    if mode == "resume":
        resume_id = stored_resume_id(data)
        if not resume_id:
            # Loud on purpose. Every harness silently drops its resume flag
            # when handed an empty id (see _claude_command / _codex_command /
            # _opencode_command), so this used to launch a BRAND NEW agent in
            # the resumed session's slot: tmux alive, agent alive, prompt
            # delivered, conversation gone. Better to refuse and say why.
            still_running = _tmux_alive(data)
            guidance = (
                "the tmux session is still running and may be waiting for input — open it"
                if still_running else
                "start a new session instead"
            )
            raise ValueError(
                f"session '{name}' cannot be resumed: no harness_session_id on "
                f"record (the {harness} conversation id was never stamped, so "
                f"there is no transcript to resume) — {guidance}"
            )
        # Same CLI-prompt constraint as spawn — stamp for paste delivery.
        if get_harness(harness).get("prompt_via_paste"):
            if prompt:
                # A boot prompt may already be pending (the agent died before
                # deliver_boot_prompt could fire, e.g. an OOM-kill at boot).
                # The bot resumes such a session WITH the user's next message
                # (bot/core.py, app.py resume) — merge so the start-chat
                # invocation + adapter routing context aren't clobbered.
                # Caveat: the merged prompt has the skill invocation mid-text
                # ("<pending> <user msg>"), so it delivers as literal context
                # rather than auto-invoking — acceptable for the rare boot-crash
                # recovery case; the model still reads and follows it.
                pending = (data.get("pending_boot_prompt") or "").strip()
                merged = f"{pending} {prompt}".strip() if pending else prompt
                registry.update(name, wolt=wolt, pending_boot_prompt=merged)
            prompt = ""
        return build_command(
            harness, "resume", resume_id=resume_id, model=model, prompt=prompt,
            execution_policy=data.get("execution_policy"),
        )

    raise ValueError(f"unknown mode: {mode}")


def discover_session_id_for(name: str, timeout: int = 90) -> str:
    """Discover and stamp the harness-assigned session id for a session.

    For harnesses with preset ids (claude) this returns the stored id
    immediately. For others (codex) it polls the harness's discover function
    until the id shows up on disk, then stamps harness_session_id so resume
    works. Called by run-session.sh in the background right after spawn.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    existing = data.get("harness_session_id", "")
    if existing:
        return existing

    discover = get_harness(resolve_harness(data.get("harness"))).get("discover_session_id")
    if discover is None:
        return ""

    # Small slack behind now — the rollout may have been written between
    # spawn and this poller starting.
    since = time.time() - 15
    deadline = time.time() + timeout
    while time.time() < deadline:
        current = registry.get(name, check_alive=False)
        if current is None:
            return ""
        existing = stored_resume_id(current)
        if existing:
            return existing
        session_id = discover(current, since, registry.taken_resume_ids(exclude=name))
        if session_id:
            # Concurrent Codex pollers can both see the same newest rollout;
            # the UNIQUE id refuses the second, which looks again next round.
            try:
                registry.update(
                    name, wolt=current.get("wolt", ""),
                    harness_session_id=session_id,
                )
                return session_id
            except sqlite3.IntegrityError:
                pass
        time.sleep(2)
    return ""


def deliver_boot_prompt(name: str, timeout: int = 90) -> bool:
    """Paste a pending boot prompt into a session's TUI once it has painted.

    Harnesses flagged prompt_via_paste (opencode) can't take the opening
    prompt on the CLI: the TUI dispatches --prompt before model resolution
    finishes, so the first message goes to the fallback default model instead
    of the pin — and a prompt starting with "/" opens the command palette and
    never submits. A tmux paste after the TUI has painted does neither (both
    benched live on opencode 1.18.3) — same delivery as resume/IWCL.

    prepare_session_command stamps pending_boot_prompt; this waits for the
    pane to be ready, then pastes and clears the stamp. Readiness = the
    harness's tui_ready_marker appears AFTER having been absent at least once.
    The absent-first requirement matters on the revive path (tmux pane alive,
    agent exited): run-session.sh --resume runs in the SAME pane, which may
    still show the dead TUI's last frame — including the marker in its footer.
    Waiting for the marker to clear (the pane repaints when the new agent
    launches) before accepting it prevents pasting into a frozen stale frame.
    A fresh/dead-tmux spawn starts on a blank pane, so absent-first is
    satisfied immediately and costs nothing.

    If the TUI never paints (agent died at boot) the stamp is left in place so
    a later --resume respawn delivers it. Immediate no-op when nothing is
    pending — run-session.sh backgrounds this for every spawn/resume.

    Returns True when the prompt was delivered.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    if not (data.get("pending_boot_prompt") or "").strip():
        return False

    harness = resolve_harness(data.get("harness"))
    entry = get_harness(harness)
    marker = entry.get("tui_ready_marker") or ""

    deadline = time.time() + timeout
    ready = False
    seen_absent = False
    while time.time() < deadline:
        # No marker configured → we can't detect readiness by content, so fall
        # back to the fixed settle below (never gate on a marker we don't have,
        # which would otherwise loop until timeout and strand the prompt).
        if not marker:
            ready = True
            break
        try:
            # Visible pane only (no -S). The gate below waits for the marker to
            # CLEAR when the pane repaints; scrollback keeps a scrolled-off
            # marker permanently "present", so seen_absent would never flip and
            # the prompt would be stranded until the timeout.
            pane = _tmux_capture(name, start=None)
        except (subprocess.SubprocessError, OSError, RuntimeError):
            pane = ""
        present = marker in pane
        if not present:
            seen_absent = True
        elif seen_absent:
            ready = True
            break
        time.sleep(1)
    if not ready:
        return False

    # One beat between first paint and the paste — the marker appears with
    # the TUI's first render and the composer is accepting input right after.
    time.sleep(1)

    # Claim the stamp: re-read and clear BEFORE pasting so a second concurrent
    # poller (a stale revive-path poller still looping) sees it gone and bails
    # rather than double-pasting. Restore it if the paste raises.
    claim = registry.get(name, check_alive=False) or {}
    prompt = (claim.get("pending_boot_prompt") or "").strip()
    if not prompt:
        return False
    wolt = data.get("wolt", "")
    registry.update(name, wolt=wolt, pending_boot_prompt="")
    try:
        # Flatten newlines (a bare \n would submit early) then guard the slash.
        _tmux_paste(name, _guard_paste_text(harness, prompt.replace("\n", " ")),
                    settle=entry.get("paste_settle", 0.0))
    except Exception:
        registry.update(name, wolt=wolt, pending_boot_prompt=prompt)
        raise
    return True


# ---------------------------------------------------------------------------
# Session spawning — shared entry point for all adapters
# ---------------------------------------------------------------------------

def wolt_harness(wolt: str) -> str:
    """The harness a new session for `wolt` would run on.

    Same order start_session resolves: wolt.json "harness" > lodge default >
    platform default. Callers that have to spell a skill invocation before a
    session exists ask here instead of assuming claude.
    """
    wolt_json_path = WOLTS_DIR / wolt / "wolt" / "wolt.json"
    pinned = ""
    try:
        pinned = json.loads(wolt_json_path.read_text()).get("harness", "") or ""
    except (json.JSONDecodeError, OSError):
        pass
    return resolve_harness(pinned or get_default_harness())


def start_session(
    *,
    wolt: str,
    prompt: str = "",
    creature: str = "",
    routing: dict = None,
    app: str = "",
    harness: str = "",
    workdir: str | Path | None = None,
    execution_policy: str | None = None,
) -> dict:
    """Start an agent session for a specific wolt.

    wolt: required — the target wolt name (e.g. "neowolt", "UXwolt").
    prompt: opening message for the session.
    creature: optional "raccoon"/"beaver"/"otter" to pick the model.
    routing: adapter routing info (adapter, chat_id, etc.) for notifications.
    app: optional app name — session runs in wolt/apps/{name}/.
    harness: optional harness override (per-session). Falls back to the wolt's
        wolt.json "harness" field, then the platform default (claude).

    Returns dict with session info: name, url, wolt, and optionally app/creature/model.
    Raises ValueError if the wolt directory doesn't exist.
    """
    wolt_home = WOLTS_DIR / wolt
    if not wolt_home.is_dir():
        raise ValueError(f"wolt '{wolt}' not found at {wolt_home}")

    # Always derive creature from the wolt's type — never let the caller override this.
    # The wolt.json may also carry a default harness for new sessions.
    wolt_json_path = wolt_home / "wolt" / "wolt.json"
    pinned_model = ""
    if wolt_json_path.exists():
        try:
            wolt_data = json.loads(wolt_json_path.read_text())
            wolt_type = wolt_data.get("type", "")
            if wolt_type in CREATURE_MODELS:
                creature = wolt_type
            if not harness:
                harness = wolt_data.get("harness", "")
            # per-wolt model pin (validated against the resolved harness below)
            pinned_model = wolt_data.get("model", "") or ""
        except (json.JSONDecodeError, OSError):
            pass
    # Resolution: explicit arg > wolt.json override > lodge default > "claude".
    if not harness:
        harness = get_default_harness()
    harness = resolve_harness(harness)

    if app:
        if workdir is not None:
            raise ValueError("workdir cannot be combined with an app session")
        apps_work_dir = wolt_home / "wolt" / "apps" / app
        apps_work_dir.mkdir(parents=True, exist_ok=True)
        workdir = apps_work_dir

    target = SessionTarget.resolve(
        wolt, workdir, wolts_dir=WOLTS_DIR
    )
    isolation = RuntimeContext.from_env().isolation
    problem = harness_install_problem(harness, isolation=isolation)
    if problem:
        raise HarnessNotInstalled(problem)
    policy, grant = resolve_execution_policy(
        execution_policy,
        isolation=isolation,
        target=target,
        grants=AutoGrantStore(WOLTS_DIR),
    )

    name = session_name(wolt)
    # pin wins if valid for the resolved harness, else the tier default (see resolve_model)
    model = resolve_model(harness, creature, pinned_model)

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={name}" if tunnel_url else ""

    registry = SessionRegistry()
    registry.create(
        name,
        wolt=wolt,
        creature=creature or "",
        model=model or "",
        harness=harness,
        dir=str(target.canonical_workdir),
        target=target,
        execution_policy=policy,
        auto_grant=grant.to_record() if grant else None,
        app=app or "",
        prompt=prompt,
        adapter=(routing or {}).get("adapter", ""),
        chat_id=str((routing or {}).get("chat_id", "")),
        user_id=str((routing or {}).get("user_id", "")),
        thread_ts=str((routing or {}).get("thread_ts", "")),
        session_url=session_url,
    )

    cmd = build_session_command(name, prompt)
    handle = _tmux_spawn(name, str(target.canonical_workdir), cmd)
    registry.update(name, wolt=wolt, runtime=handle.to_record())

    result = {
        "name": name,
        "url": session_url or None,
        "wolt": wolt,
        "wolt_id": target.wolt_id,
        "workdir": str(target.canonical_workdir),
        "target": target.to_record(),
        "execution_policy": policy.to_record(),
        "auto_grant": grant.to_record() if grant else None,
        "harness": harness,
    }
    if app:
        result["app"] = app
    if creature:
        result["creature"] = creature
        result["model"] = model

    # Set viewport: app subdomain URL if app session, otherwise wolt site.
    if app:
        try:
            # The browser reaches the app through *this* instance's subdomain
            # proxy; a second instance on :8080 served a viewport pointing at
            # whoever holds 7777.
            app_url = f"http://{app}.localhost:{os.environ.get('PORT', '7777')}/"
            registry.set_viewport(name, app_url, wolt=wolt)
            result["viewport_url"] = app_url
        except Exception as e:
            print(f"[sessions] failed to set app viewport for {app}: {e}")
    else:
        try:
            ensure_site(wolt)
            site_url = f"/wolt/{wolt}/site/"
            result["site_url"] = site_url
            # Store viewport URL in the session JSON itself.
            registry.set_viewport(name, site_url, wolt=wolt)
        except Exception as e:
            print(f"[sites] failed to ensure site for {wolt}: {e}")

    return result


# ---------------------------------------------------------------------------
# Session resume — shared entry point for bot + API
# ---------------------------------------------------------------------------

def _paste_into_live_agent(registry: "SessionRegistry", wolt: str, name: str,
                           seen_status: str, harness: str, prompt: str) -> bool:
    """Deliver a resume prompt to the agent found running.

    A rest claims the session `stopping` before it stops tmux; a paste after
    that sends the message into a dying pane and writes `running` over a
    stopped session. So find the pane first, then claim in one write: only
    while the record still has the status we saw and nobody used, held or
    rested it since, it goes back to running with this use on record, and a
    rest after it loses its own claim. Then paste. False when the session
    changed under us: a stop in progress is waited out, and the caller wakes it.
    """
    seen = registry.get(name, wolt=wolt, check_alive=False)
    if seen is None or seen.get("status") != seen_status:
        wait_out_stopping(registry, name)
        return False
    target = resolve_agent_handle(seen, harness, include_launching=False)
    if target is None:
        return False
    with registry.edit(name, wolt=wolt) as current:
        lost = not still_as_seen(current, seen)
        if not lost:
            current["status"] = "running"
            current["runtime"] = target.to_record()
            current["last_activity"] = int(time.time())  # a message is use
    if lost:
        wait_out_stopping(registry, name)
        return False
    if prompt:
        with _paste_lock(name, registry.db_dir):
            _tmux_paste(target, _guard_paste_text(harness, prompt),
                        settle=get_harness(harness).get("paste_settle", 0.0))
    return True


def resume_session(name: str, prompt: str = "") -> dict:
    """Resume an existing Claude Code session.

    Logic:
      1. Look up session in registry (scan all wolts).
      2. If tmux is alive and the agent is running, or still launching → wait
         for it to take input, then paste the prompt directly.
      3. If tmux and the saved pane live but the agent exited → resume in that pane.
      4. If tmux lives but the saved pane is gone → resume in a fresh window.
      5. If tmux is dead → create a new tmux session with run-session.sh --resume.
      6. Update status and the exact runtime handle on success.

    Every relaunch (3, 4, 5) is then *verified*: an agent has to appear in the
    session's process tree, or this raises. A stale agentless tmux session that
    refuses to revive in place is killed and respawned clean — never one with a
    live agent in it.

    Returns dict with resume info.
    Raises ValueError if session not found in registry, ResumeUnavailable if
    the session has no conversation id to replay, ResumeFailed if the agent
    did not come up — or, in step 2, did not take input within the wait.
    """
    registry = SessionRegistry()
    data = wait_out_stopping(registry, name)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    wolt = data.get("wolt", "")
    target = SessionTarget.from_record(data, wolts_dir=WOLTS_DIR, fallback_wolt=wolt)
    work_dir = str(target.canonical_workdir)
    # Resume with the harness the session was born on — old sessions have no
    # harness field, which resolves to claude.
    harness = resolve_harness(data.get("harness"))
    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={name}" if tunnel_url else ""

    tmux_alive = _tmux_alive(data)
    agent_running = False
    booting = False
    # Where the agent was actually found. Delivery targets this exact pane, so
    # a session detected in one window can never be pasted into another.
    target = _runtime_handle(data)

    if tmux_alive:
        # Launching doesn't count here — pasting into a half-booted TUI is lost.
        found = resolve_agent_handle(data, harness, include_launching=False)
        agent_running = found is not None
        if found is not None:
            target = found
        else:
            # Only the launching shim: the agent has not exited, it has not
            # started yet. Relaunching would paste `run-session.sh --resume`
            # into the pane it is booting in, so it is waited for instead.
            booting = resolve_agent_handle(data, harness, include_launching=True) is not None

    if tmux_alive and (agent_running or booting):
        # Agent is running — paste the prompt into the TUI as a single buffer paste.
        # A resume prompt is a single logical instruction, so we flatten \n here
        # for EVERY harness (pre-existing behavior): a bare \n can submit the
        # input early mid-message. This is deliberately different from the IWCL
        # deliver_message path, which preserves newlines for paste-aware TUIs so
        # the multi-line attributed message renders intact — hence the flatten
        # lives at the call site, not in _guard_paste_text.
        if booting and not prompt:
            # Nothing to paste, and nothing to relaunch: the agent is on its way.
            registry.update(name, wolt=wolt, status="running", runtime=target.to_record())
            return {"name": name, "url": session_url, "status": "delivered",
                    "detail": "agent booting, nothing to send"}
        # Wait for the agent to take input before any write, never inside one:
        # the wait can run INPUT_READY_TIMEOUT. _paste_into_live_agent finds
        # the pane again when it claims the session.
        if prompt and _await_input_ready(target, harness) is None:
            raise ResumeFailed(
                f"session '{name}': the {harness} agent is still booting — it "
                f"was not taking input after {INPUT_READY_TIMEOUT}s, so the "
                f"message was not sent"
            )
        if _paste_into_live_agent(registry, wolt, name, data.get("status"), harness,
                                  prompt.replace("\n", " ")):
            return {"name": name, "url": session_url, "status": "delivered",
                    "detail": "agent running, message sent"}
        # A rest (or a stop) took the lock between our look and the paste and
        # stopped the runtime. Look again, and wake the session the way a
        # resting one wakes: the message goes in as the resume prompt. If the
        # pane has an agent again, or a launching one (someone else woke it in
        # the meantime), leave it alone: relaunching would paste the resume
        # command into an agent that is booting.
        data = registry.get(name, wolt=wolt, check_alive=False)
        if data is None:
            raise ValueError(f"session '{name}' not found in registry")
        tmux_alive = _tmux_alive(data)
        target = _runtime_handle(data)
        agent_running = False
        if tmux_alive and resolve_agent_handle(data, harness, include_launching=True) is not None:
            raise ResumeFailed(
                f"session '{name}' changed while the message was on its way "
                f"(now {data.get('status')!r} with its agent running or "
                f"booting); nothing was sent — retry"
            )

    # A record whose workdir does not exist on this host was written by a
    # different runtime (container vs native share a migrated data root).
    # Its transcript and paths are not here — a --resume would die on arrival
    # while this function reported success, silently eating the message.
    if work_dir and not Path(work_dir).is_dir():
        raise ValueError(
            f"session '{name}' belongs to a different runtime — "
            f"workdir {work_dir} does not exist on this host"
        )

    # Nothing below this line can succeed without a conversation to replay, and
    # every harness quietly drops --resume when handed an empty id — which used
    # to spawn a fresh agent into the old session's slot and report success.
    # Refuse here, before any tmux is touched, so the caller gets the real
    # reason rather than a blank agent wearing the session's name.
    claim_resume_id(registry, data, lambda d: registry.update(
        name, wolt=wolt, harness_session_id=d["harness_session_id"]))
    if not stored_resume_id(data):
        guidance = (
            "the tmux session is still running and may be waiting for input — open it"
            if tmux_alive else
            "start a new session for this wolt instead"
        )
        raise ResumeUnavailable(
            f"session '{name}' has no {harness} conversation id on record "
            f"(harness_session_id was never stamped), so there is nothing to "
            f"resume — {guidance}"
        )

    # Both resume paths deliver run-session.sh — the single runtime wrapper.
    # It reads dir/model/harness from the registry, builds the agent command
    # via prepare_session_command, and closes out the lifecycle (finish status,
    # viewport reset) when the agent exits — which raw agent commands skipped.
    resume_cmd = build_session_command(name, prompt, resume=True)
    respawn_detail = "tmux was dead, created new tmux with --resume"

    if tmux_alive and not agent_running:
        if _runtime().handle_is_alive(target):
            # The exact dedicated pane survived; reuse it rather than creating
            # a new window on every ordinary agent exit.
            with _paste_lock(name, registry.db_dir):
                _tmux_paste(target, resume_cmd)
            registry.update(name, wolt=wolt, status="running")
            detail = "agent exited, restarted with --resume in existing pane"
        else:
            # The session survived only because unrelated user panes/windows
            # remain. Never commandeer one: add a detached dedicated window and
            # persist the exact pane tmux returns.
            target = _tmux_spawn_in_session(target, work_dir or "/workspace", resume_cmd)
            registry.update(
                name,
                wolt=wolt,
                status="running",
                runtime=target.to_record(),
            )
            # The pane on disk just moved. Keep the in-memory record in step so
            # nothing below this line addresses the pane we already replaced.
            data = {**data, "runtime": target.to_record()}
            detail = "agent pane was gone, restarted with --resume in a new window"
        if _await_agent(target, harness, _AGENT_REVIVE_TIMEOUT):
            return {"name": name, "url": session_url,
                    "status": "revived", "detail": detail}
        # In-place revival did not take. The pane we pasted into may be a
        # leftover login shell in a state that swallows the command, or the
        # wrapper died on arrival. Escalate to a clean tmux — but only after
        # re-confirming, at the session level, that no agent is running
        # anywhere in it. Killing a session with a live agent would destroy
        # exactly the conversation we were asked to rescue.
        # Ask about the whole tmux session, not the pane we just pasted into:
        # a handle carrying a pane_id narrows the walk to that pane while it
        # lives, and the pane living is precisely why we are here. Dropping the
        # pane is what makes this the session-level question the kill needs.
        whole_session = _session_scope(target)
        if session_has_agent_process(whole_session, harness) is not False:
            raise ResumeFailed(
                f"session '{name}': tried to restart the agent in its tmux "
                f"session and could not confirm it came up ({detail}); "
                f"something is running in there, so it was left alone — "
                f"attach and look before retrying"
            )
        _tmux_stop(whole_session)
        tmux_alive = False
        respawn_detail = (
            "stale tmux session held nothing but a shell and would not revive "
            "in place; killed it and created a new tmux with --resume"
        )

    # Tmux is dead (or was just cleared out from under a stale, agentless
    # session) — create a fresh tmux session running the wrapper.
    handle = _tmux_spawn(name, work_dir or "/workspace", resume_cmd)
    registry.update(name, wolt=wolt, status="running", runtime=handle.to_record())
    if not _await_agent(handle, harness):
        # run-session.sh may have exited before an agent appeared: a prepare
        # failure, a missing harness binary, an auth wall. Reporting
        # "respawned" here is what let the TUI attach to an empty shell and
        # call it a wake. But the wait running out is also just evidence about
        # our patience — the boot path is three python subprocesses deep before
        # the agent execs — so look once more, session-wide, before saying no.
        if session_has_agent_process(_session_scope(handle), harness) is True:
            return {"name": name, "url": session_url, "status": "respawned",
                    "detail": f"{respawn_detail}; the agent surfaced just past the wait"}
        # Still nothing. Report the failure, but do NOT stamp the record:
        # "failed" is terminal (list() only re-classifies records that are
        # "running"), so a slow-but-healthy agent stamped here would stay
        # hidden from the TUI forever with a live conversation inside it.
        # Left "running", list() tells the truth from the process table.
        raise ResumeFailed(
            f"session '{name}': respawned its tmux with --resume but no "
            f"{harness} process appeared within {_AGENT_APPEAR_TIMEOUT:.0f}s — "
            f"the launcher died on arrival. Run "
            f"`session-reg prepare {name} resume` to see why"
        )
    return {"name": name, "url": session_url, "status": "respawned",
            "detail": respawn_detail}


def stop_session(name: str) -> dict:
    """Stop a running session — kill tmux, mark as stopped.

    Returns dict with stop info.
    Raises ValueError if session not found in registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    wolt = data.get("wolt", "")
    # Session-level: kill whenever tmux still holds the session, even if the
    # persisted pane is long gone. Gating this on the pane would mark the
    # record stopped while the tmux session ran on, unreachable forever.
    tmux_alive = _tmux_alive(data)

    if tmux_alive:
        _tmux_stop(data)

    registry.update(name, wolt=wolt, status="stopped", finished_at=int(time.time()))
    return {"name": name, "status": "stopped", "was_alive": tmux_alive}


def archive_session(name: str) -> dict:
    """Archive a session — stop it if running, mark as archived.

    Returns dict with archive info.
    Raises ValueError if session not found in registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    wolt = data.get("wolt", "")

    # Stop tmux if still alive
    if _tmux_alive(data):
        _tmux_stop(data)

    registry.update(name, wolt=wolt, status="archived", finished_at=data.get("finished_at") or int(time.time()))
    return {"name": name, "status": "archived", "wolt": wolt}


def delete_session(name: str) -> dict:
    """Delete a session — stop it if running, remove the session file.

    Returns dict with delete info.
    Raises ValueError if session not found in registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    wolt = data.get("wolt", "")

    # Stop tmux if still alive
    if _tmux_alive(data):
        _tmux_stop(data)

    registry.delete(name, wolt=wolt)
    return {"name": name, "status": "deleted", "wolt": wolt}


# --- CLI interface (used by session-reg bash wrapper) ---

def cli():
    import sys
    args = sys.argv[1:]
    if not args:
        print("Usage: session-reg <command> [args]", file=sys.stderr)
        print("Commands: create, update, finish, get, get-field, list, touch, reconcile, prepare", file=sys.stderr)
        sys.exit(1)

    reg = SessionRegistry()
    cmd = args[0]

    if cmd == "create":
        # session-reg create <name> [key=value ...]
        if len(args) < 2:
            print("Usage: session-reg create <name> [key=value ...]", file=sys.stderr)
            sys.exit(1)
        name = args[1]
        kwargs = {}
        for kv in args[2:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                kwargs[k] = v
        data = reg.create(name, **kwargs)
        print(json.dumps(data))

    elif cmd == "update":
        # session-reg update <name> [key=value ...]
        if len(args) < 2:
            print("Usage: session-reg update <name> [key=value ...]", file=sys.stderr)
            sys.exit(1)
        name = args[1]
        fields = {}
        for kv in args[2:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                    v = int(v)
                fields[k] = v
        data = reg.update(name, **fields)
        if data:
            print(json.dumps(data))
        else:
            print(f"session not found: {name}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "finish":
        # session-reg finish <name> <exit_code>
        if len(args) < 3:
            print("Usage: session-reg finish <name> <exit_code>", file=sys.stderr)
            sys.exit(1)
        data = reg.finish(args[1], int(args[2]))
        if data:
            print(json.dumps(data))
        else:
            print(f"session not found: {args[1]}", file=sys.stderr)
            sys.exit(1)

    elif cmd == "get":
        # session-reg get <name>
        if len(args) < 2:
            print("Usage: session-reg get <name>", file=sys.stderr)
            sys.exit(1)
        data = reg.get(args[1])
        if data:
            print(json.dumps(data))
        else:
            sys.exit(1)

    elif cmd == "get-field":
        # session-reg get-field <name> <field>
        if len(args) < 3:
            print("Usage: session-reg get-field <name> <field>", file=sys.stderr)
            sys.exit(1)
        # Liveness now costs a ps snapshot on top of the tmux call, and this
        # runs on the session boot path (run-session.sh reads dir and wolt
        # through it, twice, before the agent even starts). Only pay for it
        # when a liveness field is what was asked for.
        data = reg.get(args[1], check_alive=args[2] in LIVENESS_FIELDS)
        if data and args[2] in data:
            print(data[args[2]] or "")
        else:
            sys.exit(1)

    elif cmd == "list":
        alive_only = "--alive" in args
        wolt = None
        for a in args[1:]:
            if a.startswith("--wolt="):
                wolt = a.split("=", 1)[1]
        sessions = reg.list(alive_only=alive_only, wolt=wolt)
        print(json.dumps(sessions))

    elif cmd == "touch":
        if len(args) < 2:
            sys.exit(1)
        reg.touch(args[1])

    elif cmd == "reconcile":
        orphaned = reg.reconcile()
        if orphaned:
            print(f"Marked orphaned: {', '.join(orphaned)}")
        else:
            print("All sessions in sync.")

    elif cmd == "prepare":
        # session-reg prepare <name> <spawn|resume> [prompt]
        # Prints the full agent command for run-session.sh to exec.
        if len(args) < 3:
            print("Usage: session-reg prepare <name> <spawn|resume> [prompt]", file=sys.stderr)
            sys.exit(1)
        try:
            print(prepare_session_command(args[1], args[2], args[3] if len(args) > 3 else ""))
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    elif cmd == "discover-id":
        # session-reg discover-id <name> — poll for the harness-assigned
        # session id and stamp it (background helper for run-session.sh)
        if len(args) < 2:
            print("Usage: session-reg discover-id <name>", file=sys.stderr)
            sys.exit(1)
        try:
            session_id = discover_session_id_for(args[1])
            print(session_id)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    elif cmd == "deliver-prompt":
        # session-reg deliver-prompt <name> — paste the pending boot prompt
        # once the TUI is ready (background helper for run-session.sh).
        # No-op for sessions without one (claude/codex take it on the CLI).
        if len(args) < 2:
            print("Usage: session-reg deliver-prompt <name>", file=sys.stderr)
            sys.exit(1)
        try:
            print("delivered" if deliver_boot_prompt(args[1]) else "no-op")
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
