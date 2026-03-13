from __future__ import annotations

"""
Session Registry — single source of truth for session metadata.

One JSON file per session in .state/registry/{session_name}.json.
Replaces the old scattered system (sessions/*.json + session-routing/*.json + current-url-*.json).

Usage:
    from sessions import SessionRegistry
    reg = SessionRegistry()
    reg.create("neowolt-chompy-dam-a3f1e2", wolt="neowolt", creature="beaver", ...)
    reg.update("neowolt-chompy-dam-a3f1e2", status="completed", exit_code=0)
    s = reg.get("neowolt-chompy-dam-a3f1e2")
    all_sessions = reg.list()
"""

import json
import os
import subprocess
import time
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
DEFAULT_REGISTRY_DIR = WOLTS_DIR / ".state" / "registry"


class SessionRegistry:
    def __init__(self, registry_dir: str | Path = None):
        self.dir = Path(registry_dir or DEFAULT_REGISTRY_DIR)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def _read(self, name: str) -> dict | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write(self, name: str, data: dict):
        path = self._path(name)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.rename(path)

    # --- Core API ---

    def create(
        self,
        name: str,
        *,
        wolt: str = "",
        creature: str = "",
        model: str = "",
        dir: str = "",
        title: str = "",
        prompt: str = "",
        adapter: str = "",
        chat_id: str = "",
        user_id: str = "",
        thread_ts: str = "",
        session_url: str = "",
    ) -> dict:
        """Create a new session entry. Returns the full session dict."""
        now = int(time.time())
        data = {
            "name": name,
            "wolt": wolt,
            "creature": creature,
            "model": model,
            "status": "running",
            "created_at": now,
            "finished_at": None,
            "exit_code": None,
            "dir": dir,
            "title": title,
            "prompt": prompt[:500],
            "last_activity": now,
            # routing
            "adapter": adapter,
            "chat_id": chat_id,
            "user_id": user_id,
            "thread_ts": thread_ts,
            # viewport
            "viewport_url": "",
            "session_url": session_url,
        }
        self._write(name, data)
        return data

    def update(self, name: str, **fields) -> dict | None:
        """Update fields on an existing session. Returns updated dict or None."""
        data = self._read(name)
        if data is None:
            return None
        data.update(fields)
        data["last_activity"] = int(time.time())
        self._write(name, data)
        return data

    def touch(self, name: str) -> bool:
        """Bump last_activity timestamp. Returns True if session exists."""
        data = self._read(name)
        if data is None:
            return False
        data["last_activity"] = int(time.time())
        self._write(name, data)
        return True

    def finish(self, name: str, exit_code: int) -> dict | None:
        """Mark a session as completed/failed."""
        status = "completed" if exit_code == 0 else "failed"
        return self.update(name, status=status, exit_code=exit_code, finished_at=int(time.time()))

    def get(self, name: str, *, check_alive: bool = True) -> dict | None:
        """Get session data, optionally checking tmux for liveness."""
        data = self._read(name)
        if data is None:
            return None
        if check_alive:
            data["alive"] = _tmux_alive(name)
            # correct status if tmux disagrees
            if data["status"] == "running" and not data["alive"]:
                data["status"] = "orphaned"
        return data

    def list(self, *, alive_only: bool = False, wolt: str = None) -> list[dict]:
        """List all sessions, sorted by created_at desc."""
        live_sessions = _tmux_sessions()
        results = []
        for path in self.dir.glob("*.json"):
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
            if wolt and data.get("wolt") != wolt:
                continue
            results.append(data)
        return sorted(results, key=lambda s: s.get("created_at") or 0, reverse=True)

    def reconcile(self) -> list[str]:
        """Check all 'running' sessions against tmux. Mark dead ones as orphaned.
        Returns list of session names that were marked orphaned."""
        live_sessions = _tmux_sessions()
        orphaned = []
        for path in self.dir.glob("*.json"):
            if path.suffix == ".tmp":
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") == "running" and data.get("name") not in live_sessions:
                data["status"] = "orphaned"
                data["last_activity"] = int(time.time())
                self._write(data["name"], data)
                orphaned.append(data["name"])
        return orphaned

    def delete(self, name: str) -> bool:
        """Remove a session file. Returns True if it existed."""
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        return False


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
                # auto-convert numeric values
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
