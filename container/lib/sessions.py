from __future__ import annotations

"""
Session Registry & Spawning — single source of truth for session lifecycle.

Wolt-centric model: each session lives at wolts/{wolt}/.state/sessions/{name}.json.
Viewport URL is stored in the session JSON itself — no separate current-url files.

Usage:
    from sessions import SessionRegistry, start_session
    reg = SessionRegistry()
    reg.create("neowolt-chompy-dam-a3f1e2", wolt="neowolt", creature="beaver", ...)
"""

import fcntl
import json
import os
import random
import re
import shlex
import subprocess
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from paths import (
    WOLTS_DIR as _PATHS_WOLTS_DIR,
    wolt_sessions_dir,
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
    build_command,
    session_has_agent_process,
    platform_skill_invoke,
    PLATFORM_SKILL_NAMESPACE,
    LEGACY_PLATFORM_SKILL_PREFIX,
)
from sites import ensure_site
from session_runtime import RuntimeHandle, get_runtime
from session_targets import SessionTarget, normalize_session_target
from execution_policy import (
    AutoGrantStore,
    ExecutionPolicy,
    resolve_execution_policy,
)
from runtime_context import RuntimeContext
from trust import ensure_claude_dir_trusted, ensure_codex_dir_trusted

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
# Resolved relative to this file so the dev clone drives its own script —
# a hardcoded production path pairs new sessions.py with old run-session.sh.
RUN_SESSION_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "run-session.sh"


# Why a session stopped being `running`. Recorded on the record itself, because
# "orphaned" alone cannot tell a crashed agent from a machine that went away —
# and the adoption report that knew the difference is overwritten by the next
# reconcile.
ORPHAN_TMUX_MISSING = "tmux-session-missing"
ORPHAN_RUNTIME_GONE = "runtime-gone"


class SessionRegistry:
    """Per-wolt session registry.

    Sessions are stored at wolts/{wolt}/.state/sessions/{name}.json.
    The wolt field is required for create. For lookups by name only,
    the registry scans all wolts.
    """

    def __init__(self, wolts_dir: str | Path = None):
        self.wolts_dir = Path(wolts_dir or WOLTS_DIR)

    def _sessions_dir(self, wolt: str) -> Path:
        d = wolt_sessions_dir(wolt, self.wolts_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, wolt: str, name: str) -> Path:
        return self._sessions_dir(wolt) / f"{name}.json"

    def _read(self, wolt: str, name: str) -> dict | None:
        path = self._path(wolt, name)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return normalize_session_target(
                data, wolts_dir=self.wolts_dir, fallback_wolt=wolt
            )
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, wolt: str, name: str, data: dict):
        path = self._path(wolt, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        normalized = normalize_session_target(
            data, wolts_dir=self.wolts_dir, fallback_wolt=wolt
        )
        # The reason belongs to the orphaned state and dies with it. Enforced
        # here rather than at each writer because there are many ways back to
        # running — adoption, three resume branches — and a record that says
        # `running` while still carrying `orphaned_reason` is a record that
        # lies to whoever reads it next.
        if normalized.get("status") != "orphaned":
            normalized.pop("orphaned_reason", None)
        tmp.write_text(json.dumps(normalized, indent=2) + "\n")
        tmp.rename(path)

    def _find_wolt(self, name: str) -> str | None:
        """Find which wolt owns a session by scanning all wolts."""
        for entry in self.wolts_dir.iterdir():
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            session_file = entry / ".state" / "sessions" / f"{name}.json"
            if session_file.exists():
                return entry.name
        return None

    # --- Core API ---

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

        self._write(wolt, name, data)
        return data

    @contextmanager
    def _lock(self, wolt: str, name: str):
        """Serialize one session's read-modify-write across processes.

        start_session writes the runtime handle immediately after spawning,
        while the spawned run-session.sh has already started and writes
        harness_session_id via `session-reg prepare` — two processes, same
        file. Unserialized, whichever reads first wins and the other's field
        vanishes: a lost runtime handle is cosmetic, a lost harness_session_id
        breaks --resume. Best-effort — a filesystem without flock just falls
        through to the old unlocked behavior rather than failing the write.
        """
        path = self._path(wolt, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = open(path.with_suffix(".lock"), "w")
        except OSError:
            yield
            return
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except OSError:
                pass
            yield
        finally:
            handle.close()

    def update(self, name: str, *, wolt: str = None, **fields) -> dict | None:
        """Update fields on an existing session. Returns updated dict or None.

        Merges into whatever is on disk at write time, under a per-session
        lock, so concurrent writers touching different fields don't clobber
        each other.
        """
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return None
        with self._lock(wolt, name):
            data = self._read(wolt, name)
            if data is None:
                return None
            data.update(fields)
            data["last_activity"] = int(time.time())
            self._write(wolt, name, data)
        return data

    def touch(self, name: str, *, wolt: str = None) -> bool:
        """Bump last_activity timestamp. Returns True if session exists."""
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return False
        data = self._read(wolt, name)
        if data is None:
            return False
        data["last_activity"] = int(time.time())
        self._write(wolt, name, data)
        return True

    def finish(self, name: str, exit_code: int, *, wolt: str = None) -> dict | None:
        """Mark a session as completed/failed."""
        status = "completed" if exit_code == 0 else "failed"
        return self.update(name, wolt=wolt, status=status, exit_code=exit_code, finished_at=int(time.time()))

    def get(self, name: str, *, wolt: str = None, check_alive: bool = True) -> dict | None:
        """Get session data, optionally checking tmux for liveness."""
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return None
        data = self._read(wolt, name)
        if data is None:
            return None
        if check_alive:
            data["alive"] = _tmux_alive(data)
            if data["status"] == "running" and not data["alive"]:
                data["status"] = "orphaned"
                data["orphaned_reason"] = ORPHAN_TMUX_MISSING
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
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return None
        data = self._read(wolt, name)
        if not data or not data.get("redirect_to"):
            return None
        target = data["redirect_to"]
        data["redirect_to"] = None
        data["last_activity"] = int(time.time())
        self._write(wolt, name, data)
        return target

    def list(self, *, alive_only: bool = False, wolt: str = None) -> list[dict]:
        """List sessions, sorted by created_at desc.

        If wolt is given, only list that wolt's sessions.
        Otherwise, scan all wolts.
        """
        live_sessions = _tmux_sessions()
        results = []

        wolts_to_scan = [wolt] if wolt else self._all_wolts()
        for w in wolts_to_scan:
            sessions_dir = self.wolts_dir / w / ".state" / "sessions"
            if not sessions_dir.exists():
                continue
            for path in sessions_dir.glob("*.json"):
                if path.suffix == ".tmp":
                    continue
                try:
                    data = normalize_session_target(
                        json.loads(path.read_text()),
                        wolts_dir=self.wolts_dir,
                        fallback_wolt=w,
                    )
                except (json.JSONDecodeError, OSError):
                    continue
                name = data.get("name", path.stem)
                alive = name in live_sessions
                data["alive"] = alive
                if data["status"] == "running" and not alive:
                    data["status"] = "orphaned"
                    data["orphaned_reason"] = ORPHAN_TMUX_MISSING
                if alive_only and not alive:
                    continue
                results.append(data)

        return sorted(results, key=lambda s: s.get("created_at") or 0, reverse=True)

    def reconcile(self) -> list[str]:
        """Check all 'running' sessions against tmux. Mark dead ones as orphaned."""
        live_sessions = _tmux_sessions()
        orphaned = []
        for w in self._all_wolts():
            sessions_dir = self.wolts_dir / w / ".state" / "sessions"
            if not sessions_dir.exists():
                continue
            for path in sessions_dir.glob("*.json"):
                if path.suffix == ".tmp":
                    continue
                try:
                    data = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                if data.get("status") == "running" and data.get("name") not in live_sessions:
                    data["status"] = "orphaned"
                    data["orphaned_reason"] = ORPHAN_TMUX_MISSING
                    data["last_activity"] = int(time.time())
                    wolt_name = data.get("wolt", w)
                    self._write(wolt_name, data["name"], data)
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
        }
        runtime = _runtime()
        for wolt in self._all_wolts():
            sessions_dir = self.wolts_dir / wolt / ".state" / "sessions"
            if not sessions_dir.exists():
                continue
            for path in sorted(sessions_dir.glob("*.json")):
                if path.suffix == ".tmp":
                    continue
                try:
                    data = normalize_session_target(
                        json.loads(path.read_text()),
                        wolts_dir=self.wolts_dir,
                        fallback_wolt=wolt,
                    )
                except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
                    report["unchanged"].append({
                        "session": path.stem,
                        "wolt": wolt,
                        "reason": f"unreadable registry record: {type(exc).__name__}",
                    })
                    continue

                name = data.get("name") or path.stem
                previous = data.get("status") or ""
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
                        with self._lock(wolt, name):
                            current = self._read(wolt, name)
                            if current is not None:
                                current["status"] = "orphaned"
                                current["orphaned_reason"] = ORPHAN_RUNTIME_GONE
                                self._write(wolt, name, current)
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
                    with self._lock(wolt, name):
                        current = self._read(wolt, name)
                        if current is not None:
                            current["status"] = "running"
                            current["runtime"] = adopted_handle.to_record()
                            self._write(wolt, name, current)
                report["adopted"].append({
                    "session": name,
                    "wolt": wolt,
                    "previous_status": previous,
                    "runtime": adopted_handle.to_record(),
                    "handle_refreshed": adopted_handle != handle,
                })
        return report

    def delete(self, name: str, *, wolt: str = None) -> bool:
        """Remove a session file. Returns True if it existed."""
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return False
        path = self._path(wolt, name)
        if path.exists():
            path.unlink()
            return True
        return False

    def _all_wolts(self) -> list[str]:
        """List all wolt directory names."""
        if not self.wolts_dir.exists():
            return []
        return [
            d.name for d in sorted(self.wolts_dir.iterdir())
            if d.is_dir() and not d.name.startswith(".")
        ]


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

    settle: seconds to wait between the paste and the Enter keystroke.
    Codex's TUI folds an immediate Enter into the paste (message stays in
    the composer) — its harness entry sets paste_settle=0.5. Claude takes 0.
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
    reply = (
        f'\nReply with: woltspace session send {from_session} "your reply"'
        if from_session else ""
    )
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
    reply = (
        f'\nReply with: woltspace session send {from_session} "your reply"'
        if from_session else ""
    )
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
      delivered   — pasted into a live session
      session-dead — session exists in the registry but tmux/agent is gone
      no-session   — no such session

    Delivery is harness-aware: it reuses _tmux_paste with the target harness's
    paste_settle (codex needs a settle before Enter; claude takes 0).
    """
    reg = registry or SessionRegistry()
    data = reg.get(session_id)
    if data is None:
        return {"status": "no-session", "session": session_id}
    if not data.get("alive"):
        return {"status": "session-dead", "session": session_id}
    harness = resolve_harness(data.get("harness"))
    settle = get_harness(harness).get("paste_settle", 0.0)
    body = format_attributed_message(text, from_wolt, from_session)
    _tmux_paste(data, _guard_paste_text(harness, body), settle=settle)
    reg.touch(session_id)
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
    """Adapter-specific context so the wolt knows how to communicate from the start."""
    adapter = data.get("adapter", "")
    session_url = data.get("session_url", "")
    if adapter == "slack":
        channel = data.get("chat_id", "")
        thread_ts = data.get("thread_ts", "")
        if channel and thread_ts:
            return (
                f"\nThis session was started from Slack. Send messages with: "
                f'notify --slack {channel} {thread_ts} "your message"\n'
                f"Session link: {session_url}"
            )
    elif adapter == "telegram":
        chat_id = data.get("chat_id", "")
        if chat_id:
            return (
                f"\nThis session was started from Telegram. Send messages with: "
                f'notify --telegram {chat_id} "your message"\n'
                f"Session link: {session_url}"
            )
    return ""


def _wolt_boot_context(data: dict) -> str:
    """Load the owning wolt's lean boot memory without touching the repo.

    Harnesses run in arbitrary project directories, so cwd-based instruction
    discovery cannot establish wolt identity. The opening message carries the
    three documented boot files directly; HOME and the repository stay intact.
    """
    wolt = data.get("wolt_id") or data.get("wolt") or ""
    if not wolt:
        return ""
    memory_dir = WOLTS_DIR / wolt / "wolt" / "memory"
    sections = []
    for filename in ("identity.md", "context.md", "learnings.md"):
        path = memory_dir / filename
        try:
            content = path.read_text().strip()
        except OSError:
            continue
        if content:
            sections.append(f"## {filename}\n{content}")
    if not sections:
        return ""
    target = SessionTarget.from_record(data, wolts_dir=WOLTS_DIR, fallback_wolt=wolt)
    return (
        "[Woltspace boot context]\n"
        f"Wolt: {target.wolt_id}\n"
        f"Working directory: {target.canonical_workdir}\n"
        "Persistent boot memory follows; treat it as the owning wolt's context.\n\n"
        + "\n\n".join(sections)
        + "\n[/Woltspace boot context]"
    )


def _invokes_platform_skill(prompt: str) -> bool:
    """True if `prompt` already asks for a platform skill by name.

    Two shapes are live at once during the plugin ratchet: the namespaced one
    every harness gets from the plugin / `.claude-plugin/` tree
    (`/woltspace:notify`, `@woltspace:notify`) and the pre-plugin copy-sync
    spelling a wolt that has not been ratcheted still uses
    (`/woltspace-notify`). Matched on the name itself rather than a
    harness-formatted string, because a prompt is written by whoever called us
    and may carry either.
    """
    return (
        f"{PLATFORM_SKILL_NAMESPACE}:" in prompt
        or LEGACY_PLATFORM_SKILL_PREFIX in prompt
    )


def _assemble_spawn_prompt(data: dict, prompt: str, harness: str) -> str:
    """Full opening prompt: user's task + adapter context + start-chat invocation.

    Skips start-chat if the prompt already invokes a woltspace skill
    (e.g. create-wolt). The invocation syntax comes from the harness table —
    claude spells a platform skill /woltspace:name, codex @woltspace:name.
    """
    context = _adapter_context(data)
    boot_context = _wolt_boot_context(data)
    prefix = f"{boot_context}\n\n" if boot_context else ""
    if _invokes_platform_skill(prompt):
        return f"{prefix}{prompt}{context}"
    adapter = data.get("adapter") or "lodge"
    wolt = data.get("wolt") or "wolt"
    start_chat = platform_skill_invoke(harness, "start-chat")
    return f"{prefix}{prompt}{context} {start_chat} {adapter} {wolt}"


def prepare_session_command(name: str, mode: str, prompt: str = "") -> str:
    """Build the full agent command for a session — the run-session.sh backend.

    Everything comes from the registry. Spawn also stamps harness_session_id
    (used later for --resume) and a title derived from the prompt.

    Raises ValueError if the session isn't in the registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    harness = resolve_harness(data.get("harness"))
    wolt = data.get("wolt", "")
    model = data.get("model", "")

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
        updates = {"title": _title_from_prompt(prompt)}
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
        # harness_session_id is the generic field, written by us at spawn.
        # claude_session_id is the pre-harness spelling — old sessions resume
        # through the fallback, UUID-validated because some legacy sessions
        # stored non-UUID values there.
        resume_id = data.get("harness_session_id") or ""
        if not resume_id:
            legacy = data.get("claude_session_id") or ""
            resume_id = legacy if _UUID_RE.match(legacy) else ""
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
        session_id = discover(data, since)
        if session_id:
            registry.update(name, wolt=data.get("wolt", ""), harness_session_id=session_id)
            return session_id
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

def resume_session(name: str, prompt: str = "") -> dict:
    """Resume an existing Claude Code session.

    Logic:
      1. Look up session in registry (scan all wolts).
      2. If tmux is alive and the agent is running → paste the prompt directly.
      3. If tmux and the saved pane live but the agent exited → resume in that pane.
      4. If tmux lives but the saved pane is gone → resume in a fresh window.
      5. If tmux is dead → create a new tmux session with run-session.sh --resume.
      6. Update status and the exact runtime handle on success.

    Returns dict with resume info.
    Raises ValueError if session not found in registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
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
    # Where the agent was actually found. Delivery targets this exact pane, so
    # a session detected in one window can never be pasted into another.
    target = _runtime_handle(data)

    if tmux_alive:
        # Launching doesn't count here — pasting into a half-booted TUI is lost.
        found = resolve_agent_handle(data, harness, include_launching=False)
        agent_running = found is not None
        if found is not None:
            target = found

    if tmux_alive and agent_running:
        # Agent is running — paste the prompt into the TUI as a single buffer paste.
        # A resume prompt is a single logical instruction, so we flatten \n here
        # for EVERY harness (pre-existing behavior): a bare \n can submit the
        # input early mid-message. This is deliberately different from the IWCL
        # deliver_message path, which preserves newlines for paste-aware TUIs so
        # the multi-line attributed message renders intact — hence the flatten
        # lives at the call site, not in _guard_paste_text.
        if prompt:
            _tmux_paste(target, _guard_paste_text(harness, prompt.replace("\n", " ")),
                        settle=get_harness(harness).get("paste_settle", 0.0))
        registry.update(
            name,
            wolt=wolt,
            status="running",
            runtime=target.to_record(),
        )
        return {"name": name, "url": session_url, "status": "delivered", "detail": "agent running, message sent"}

    # A record whose workdir does not exist on this host was written by a
    # different runtime (container vs native share a migrated data root).
    # Its transcript and paths are not here — a --resume would die on arrival
    # while this function reported success, silently eating the message.
    if work_dir and not Path(work_dir).is_dir():
        raise ValueError(
            f"session '{name}' belongs to a different runtime — "
            f"workdir {work_dir} does not exist on this host"
        )

    # Both resume paths deliver run-session.sh — the single runtime wrapper.
    # It reads dir/model/harness from the registry, builds the agent command
    # via prepare_session_command, and closes out the lifecycle (finish status,
    # viewport reset) when the agent exits — which raw agent commands skipped.
    resume_cmd = build_session_command(name, prompt, resume=True)

    if tmux_alive and not agent_running:
        if _runtime().handle_is_alive(target):
            # The exact dedicated pane survived; reuse it rather than creating
            # a new window on every ordinary agent exit.
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
            detail = "agent pane was gone, restarted with --resume in a new window"
        return {"name": name, "url": session_url, "status": "revived", "detail": detail}

    # Tmux is dead — create a fresh tmux session running the wrapper
    handle = _tmux_spawn(name, work_dir or "/workspace", resume_cmd)
    registry.update(name, wolt=wolt, status="running", runtime=handle.to_record())
    return {"name": name, "url": session_url, "status": "respawned", "detail": "tmux was dead, created new tmux with --resume"}


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
        data = reg.get(args[1])
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
