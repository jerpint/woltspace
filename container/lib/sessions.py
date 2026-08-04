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

import json
import os
import random
import re
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from paths import (
    WOLTS_DIR as _PATHS_WOLTS_DIR,
    wolt_sessions_dir,
    tunnel_state_file,
    space_dir,
)
from harnesses import (
    HARNESSES,
    DEFAULT_HARNESS,
    resolve_harness,
    creature_model,
    resolve_model,
    get_harness,
    get_default_harness,
    build_command,
    session_has_agent_process,
)
from sites import ensure_site

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
# Resolved relative to this file so the dev clone drives its own script —
# a hardcoded production path pairs new sessions.py with old run-session.sh.
RUN_SESSION_SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "run-session.sh"


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
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, wolt: str, name: str, data: dict):
        path = self._path(wolt, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
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
    ) -> dict:
        """Create a new session entry. Returns the full session dict."""
        if not wolt:
            raise ValueError("wolt is required for session creation")
        now = int(time.time())
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
            "dir": dir,
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

    def update(self, name: str, *, wolt: str = None, **fields) -> dict | None:
        """Update fields on an existing session. Returns updated dict or None."""
        if not wolt:
            wolt = self._find_wolt(name)
        if not wolt:
            return None
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
            data["alive"] = _tmux_alive(name)
            if data["status"] == "running" and not data["alive"]:
                data["status"] = "orphaned"
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
                    data = json.loads(path.read_text())
                except (json.JSONDecodeError, OSError):
                    continue
                name = data.get("name", path.stem)
                alive = name in live_sessions
                data["alive"] = alive
                if data["status"] == "running" and not alive:
                    data["status"] = "orphaned"
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
                    data["last_activity"] = int(time.time())
                    wolt_name = data.get("wolt", w)
                    self._write(wolt_name, data["name"], data)
                    orphaned.append(data["name"])
        return orphaned

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
    """Get set of live tmux session names."""
    try:
        raw = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return {name for name in raw.split("\n") if name and name != "main"}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return set()


def _tmux_alive(name: str) -> bool:
    """Check if a specific tmux session is alive."""
    try:
        subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


_TMUX_TIMEOUT = 10  # seconds — safety net so a stuck tmux call never freezes the bot


def _tmux_paste(target: str, text: str, settle: float = 0.0):
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
    buf_name = f"paste-{target}"
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "-X", "cancel"],
        check=False, timeout=_TMUX_TIMEOUT,
    )
    subprocess.run(
        ["tmux", "set-buffer", "-b", buf_name, text],
        check=True, timeout=_TMUX_TIMEOUT,
    )
    subprocess.run(
        ["tmux", "paste-buffer", "-b", buf_name, "-d", "-t", target],
        check=True, timeout=_TMUX_TIMEOUT,
    )
    if settle > 0:
        time.sleep(settle)
    subprocess.run(
        ["tmux", "send-keys", "-t", target, "Enter"],
        check=True, timeout=_TMUX_TIMEOUT,
    )


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
    _tmux_paste(session_id, body, settle=settle)
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


def _assemble_spawn_prompt(data: dict, prompt: str, harness: str) -> str:
    """Full opening prompt: user's task + adapter context + start-chat invocation.

    Skips start-chat if the prompt already invokes a woltspace skill
    (e.g. create-wolt). The skill invocation syntax comes from the harness
    table — claude spells it /name, codex @name.
    """
    skill_invoke = get_harness(harness)["skill_invoke"]
    context = _adapter_context(data)
    if skill_invoke.format(name="woltspace-") in prompt:
        return f"{prompt}{context}"
    adapter = data.get("adapter") or "lodge"
    wolt = data.get("wolt") or "wolt"
    start_chat = skill_invoke.format(name="woltspace-start-chat")
    return f"{prompt}{context} {start_chat} {adapter} {wolt}"


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

    if mode == "spawn":
        # Harnesses that accept a preset session id (claude --session-id) get
        # one generated and stamped now. Others (codex) assign their own —
        # run-session.sh discovers it after launch via discover-id.
        session_id = ""
        updates = {"title": _title_from_prompt(prompt)}
        if get_harness(harness).get("preset_session_id"):
            session_id = str(uuid.uuid4())
            updates["harness_session_id"] = session_id
        registry.update(name, wolt=wolt, **updates)
        full_prompt = _assemble_spawn_prompt(data, prompt, harness)
        return build_command(
            harness, "spawn",
            session_id=session_id, session_name=name,
            model=model, prompt=full_prompt,
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
        return build_command(harness, "resume", resume_id=resume_id, model=model, prompt=prompt)

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


# ---------------------------------------------------------------------------
# Session spawning — shared entry point for all adapters
# ---------------------------------------------------------------------------

def start_session(
    *,
    wolt: str,
    prompt: str = "",
    creature: str = "",
    routing: dict = None,
    app: str = "",
    harness: str = "",
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
    target_dir = WOLTS_DIR / wolt
    if not target_dir.is_dir():
        raise ValueError(f"wolt '{wolt}' not found at {target_dir}")

    # Always derive creature from the wolt's type — never let the caller override this.
    # The wolt.json may also carry a default harness for new sessions.
    wolt_json_path = target_dir / "wolt" / "wolt.json"
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
        apps_work_dir = target_dir / "wolt" / "apps" / app
        apps_work_dir.mkdir(parents=True, exist_ok=True)
        target_dir = apps_work_dir

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
        dir=str(target_dir),
        app=app or "",
        prompt=prompt,
        adapter=(routing or {}).get("adapter", ""),
        chat_id=str((routing or {}).get("chat_id", "")),
        user_id=str((routing or {}).get("user_id", "")),
        thread_ts=str((routing or {}).get("thread_ts", "")),
        session_url=session_url,
    )

    cmd = build_session_command(name, prompt)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(target_dir), cmd],
        check=True,
    )

    result = {"name": name, "url": session_url or None, "wolt": wolt, "harness": harness}
    if app:
        result["app"] = app
    if creature:
        result["creature"] = creature
        result["model"] = model

    # Set viewport: app subdomain URL if app session, otherwise wolt site.
    if app:
        try:
            app_url = f"http://{app}.localhost:7777/"
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
      3. If tmux is alive but the agent exited → run-session.sh --resume in the pane.
      4. If tmux is dead → create a new tmux session with run-session.sh --resume.
      5. Update status back to "running" on success.

    Returns dict with resume info.
    Raises ValueError if session not found in registry.
    """
    registry = SessionRegistry()
    data = registry.get(name, check_alive=False)
    if data is None:
        raise ValueError(f"session '{name}' not found in registry")

    wolt = data.get("wolt", "")
    session_dir = data.get("dir", "")
    # Resume with the harness the session was born on — old sessions have no
    # harness field, which resolves to claude.
    harness = resolve_harness(data.get("harness"))
    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={name}" if tunnel_url else ""

    tmux_alive = _tmux_alive(name)
    agent_running = False

    if tmux_alive:
        # Launching doesn't count here — pasting into a half-booted TUI is lost.
        agent_running = session_has_agent_process(name, harness, include_launching=False)

    if tmux_alive and agent_running:
        # Agent is running — paste the prompt into the TUI as a single buffer paste.
        # Flatten newlines: a bare \n would submit the input early in the TUI.
        if prompt:
            flat_prompt = prompt.replace("\n", " ")
            _tmux_paste(name, flat_prompt, settle=get_harness(harness).get("paste_settle", 0.0))
        registry.update(name, wolt=wolt, status="running")
        return {"name": name, "url": session_url, "status": "delivered", "detail": "agent running, message sent"}

    # Both resume paths deliver run-session.sh — the single runtime wrapper.
    # It reads dir/model/harness from the registry, builds the agent command
    # via prepare_session_command, and closes out the lifecycle (finish status,
    # viewport reset) when the agent exits — which raw agent commands skipped.
    resume_cmd = build_session_command(name, prompt, resume=True)

    if tmux_alive and not agent_running:
        # Tmux alive but the agent exited — run the wrapper inside the pane
        _tmux_paste(name, resume_cmd)
        registry.update(name, wolt=wolt, status="running")
        return {"name": name, "url": session_url, "status": "revived", "detail": "agent exited, restarted with --resume in existing tmux"}

    # Tmux is dead — create a fresh tmux session running the wrapper
    work_dir = session_dir or str(WOLTS_DIR / wolt) if wolt else "/workspace"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", work_dir or "/workspace", resume_cmd],
        check=True, timeout=_TMUX_TIMEOUT,
    )
    registry.update(name, wolt=wolt, status="running")
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
    tmux_alive = _tmux_alive(name)

    if tmux_alive:
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

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
    if _tmux_alive(name):
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

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
    if _tmux_alive(name):
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True, check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

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

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
