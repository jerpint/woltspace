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
import shlex
import subprocess
import time
from pathlib import Path

from paths import (
    WOLTS_DIR as _PATHS_WOLTS_DIR,
    wolt_sessions_dir,
    tunnel_url_file,
    space_dir,
)
from sites import start_site

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
RUN_SESSION_SCRIPT = Path("/workspace/woltspace/container/bin/run-session.sh")


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
        dir: str = "",
        project: str = "",
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
            "project": project,
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

        For project URLs (/project/{name}/...), viewport_port is the primary
        field — split.html uses it to connect directly to the project port,
        bypassing the FastAPI proxy. If port is not explicitly given, the
        running project's port is looked up automatically.
        """
        import re
        if port == 7777:
            proj_match = re.match(r"^/project/([^/]+)", url)
            if proj_match:
                try:
                    from projects import running_projects
                    proj_name = proj_match.group(1)
                    running = {r["name"]: r for r in running_projects()}
                    if proj_name in running:
                        port = running[proj_name]["port"]
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

CREATURE_MODELS = {
    "raccoon": "opus",
    "beaver": "sonnet",
    "otter": "haiku",
    "rodent": "opus",  # legacy type — treated as raccoon
    "wolf": "sonnet",
}


def session_name(prefix: str) -> str:
    """Generate a session name: prefix-adjective-noun-6hex."""
    adj = random.choice(SESSION_ADJECTIVES)
    noun = random.choice(SESSION_NOUNS)
    hex6 = f"{random.randint(0, 0xFFFFFF):06x}"
    return f"{prefix}-{adj}-{noun}-{hex6}"


def get_tunnel_url(wolts_dir: Path = None) -> str:
    """Read the tunnel URL from .space/platform/tunnel-url."""
    f = tunnel_url_file(wolts_dir)
    if f.exists():
        return f.read_text().strip().rstrip("/")
    # Backwards compat: check old location
    old = (wolts_dir or WOLTS_DIR) / ".state" / "tunnel-url"
    if old.exists():
        return old.read_text().strip().rstrip("/")
    return ""


def build_session_command(name: str, work_dir: str, prompt: str, model: str = None) -> str:
    """Build the shell command that tmux will execute."""
    cmd = f"{RUN_SESSION_SCRIPT} {shlex.quote(name)} {shlex.quote(work_dir)} {shlex.quote(prompt)}"
    if model:
        cmd += f" {shlex.quote(model)}"
    return cmd


# ---------------------------------------------------------------------------
# Session spawning — shared entry point for all adapters
# ---------------------------------------------------------------------------

def start_session(
    *,
    wolt: str,
    prompt: str = "",
    creature: str = "",
    routing: dict = None,
    project: str = "",
) -> dict:
    """Start a Claude Code session for a specific wolt.

    wolt: required — the target wolt name (e.g. "neowolt", "UXwolt").
    prompt: opening message for the session.
    creature: optional "raccoon"/"beaver"/"otter" to pick the model.
    routing: adapter routing info (adapter, chat_id, etc.) for notifications.
    project: optional project name — session runs in wolt/projects/{name}/.

    Returns dict with session info: name, url, wolt, and optionally project/creature/model.
    Raises ValueError if the wolt directory doesn't exist.
    """
    target_dir = WOLTS_DIR / wolt
    if not target_dir.is_dir():
        raise ValueError(f"wolt '{wolt}' not found at {target_dir}")

    # Always derive creature from the wolt's type — never let the caller override this.
    wolt_json_path = target_dir / "wolt" / "wolt.json"
    if wolt_json_path.exists():
        try:
            wolt_data = json.loads(wolt_json_path.read_text())
            wolt_type = wolt_data.get("type", "")
            if wolt_type in CREATURE_MODELS:
                creature = wolt_type
        except (json.JSONDecodeError, OSError):
            pass

    if project:
        project_dir = target_dir / "wolt" / "projects" / project
        project_dir.mkdir(parents=True, exist_ok=True)
        target_dir = project_dir

    name = session_name(wolt)
    model = CREATURE_MODELS.get(creature) if creature else None

    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={name}" if tunnel_url else ""

    registry = SessionRegistry()
    registry.create(
        name,
        wolt=wolt,
        creature=creature or "",
        model=model or "",
        dir=str(target_dir),
        project=project or "",
        prompt=prompt,
        adapter=(routing or {}).get("adapter", ""),
        chat_id=str((routing or {}).get("chat_id", "")),
        user_id=str((routing or {}).get("user_id", "")),
        thread_ts=str((routing or {}).get("thread_ts", "")),
        session_url=session_url,
    )

    cmd = build_session_command(name, str(target_dir), prompt, model=model)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", str(target_dir), cmd],
        check=True,
    )

    result = {"name": name, "url": session_url or None, "wolt": wolt}
    if project:
        result["project"] = project
    if creature:
        result["creature"] = creature
        result["model"] = model

    # Set viewport: project subdomain URL if project session, otherwise wolt site.
    if project:
        try:
            project_url = f"http://{project}.localhost:7777/"
            registry.set_viewport(name, project_url, wolt=wolt)
            result["viewport_url"] = project_url
        except Exception as e:
            print(f"[sessions] failed to set project viewport for {project}: {e}")
    else:
        try:
            site_state = start_site(wolt)
            site_url = f"/wolt/{wolt}/site/"
            result["site_url"] = site_url
            result["site_port"] = site_state["port"]
            # Store viewport URL in the session JSON itself.
            registry.set_viewport(name, site_url, wolt=wolt)
        except Exception as e:
            print(f"[sites] failed to auto-start for {wolt}: {e}")

    return result


# ---------------------------------------------------------------------------
# Session resume — shared entry point for bot + API
# ---------------------------------------------------------------------------

def resume_session(name: str, prompt: str = "") -> dict:
    """Resume an existing Claude Code session.

    Logic:
      1. Look up session in registry (scan all wolts).
      2. If tmux is alive and claude is running → send keys directly.
      3. If tmux is alive but claude exited → start wclaude --resume in the pane.
      4. If tmux is dead → create a new tmux session with wclaude --resume.
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
    claude_session_id = data.get("claude_session_id", "")
    tunnel_url = get_tunnel_url()
    session_url = f"{tunnel_url}/tui?session={name}" if tunnel_url else ""

    tmux_alive = _tmux_alive(name)
    claude_running = False

    if tmux_alive:
        claude_running = _session_has_claude_process(name)

    safe_prompt = shlex.quote(prompt) if prompt else ""

    if tmux_alive and claude_running:
        # Claude is running — send the prompt as keystrokes
        if prompt:
            subprocess.run(["tmux", "send-keys", "-t", name, "-l", prompt], check=True)
            subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        registry.update(name, wolt=wolt, status="running")
        return {"name": name, "url": session_url, "status": "delivered", "detail": "claude running, message sent"}

    # Build --resume flag using the stored UUID (claude_session_id), not the session name
    resume_flag = f"--resume {shlex.quote(claude_session_id)}" if claude_session_id else ""

    if tmux_alive and not claude_running:
        # Tmux alive but claude exited — restart claude with --resume inside the pane
        cd_prefix = f"cd {shlex.quote(session_dir)} && " if session_dir else ""
        resume_cmd = f"{cd_prefix}export WOLT_SESSION={shlex.quote(name)} && wclaude --dangerously-skip-permissions {resume_flag} {safe_prompt}"
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", resume_cmd], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        registry.update(name, wolt=wolt, status="running")
        return {"name": name, "url": session_url, "status": "revived", "detail": "claude exited, restarted with --resume in existing tmux"}

    # Tmux is dead — create a fresh tmux session with wclaude --resume
    work_dir = session_dir or str(WOLTS_DIR / wolt) if wolt else "/workspace"
    cd_prefix = f"cd {shlex.quote(work_dir)} && " if work_dir else ""
    resume_cmd = f"{cd_prefix}export WOLT_SESSION={shlex.quote(name)} && wclaude --dangerously-skip-permissions {resume_flag} {safe_prompt}"
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-c", work_dir or "/workspace", resume_cmd],
        check=True,
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


def _session_has_claude_process(name: str) -> bool:
    """Check if a tmux session has a claude process running in its pane."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", name, "-F", "#{pane_pid}"],
            capture_output=True, text=True, check=True,
        )
        pane_pid = result.stdout.strip()
        if not pane_pid:
            return False
        ps_result = subprocess.run(
            ["ps", "--ppid", pane_pid, "-o", "comm=", "--no-headers"],
            capture_output=True, text=True,
        )
        for child in ps_result.stdout.strip().split("\n"):
            child = child.strip()
            if child in ("claude", "run-session.sh", "run-session"):
                return True
        return False
    except subprocess.CalledProcessError:
        return False


# --- CLI interface (used by session-reg bash wrapper) ---

def cli():
    import sys
    args = sys.argv[1:]
    if not args:
        print("Usage: session-reg <command> [args]", file=sys.stderr)
        print("Commands: create, update, finish, get, get-field, list, touch, reconcile", file=sys.stderr)
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

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    cli()
