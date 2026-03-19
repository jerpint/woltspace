"""
Wolt site management — each wolt gets a livereload-powered static site.

Sites are the wolt's persistent space: scratch files, mockups, diagrams.
Served via `livereload` (one process per wolt), auto-started when a session
begins outside a project context, stopped when no sessions are active.

Uses the same port pool as projects (4001-4999).

Usage:
    from sites import start_site, stop_site, running_sites, site_dir
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

# Running site state — keyed by wolt name
_RUNNING_STATE_DIR = WOLTS_DIR / ".state" / "sites"

# Reuse the same port pool as projects — import the allocator constants
# Port range 4001-4999, shared with projects
PORT_MIN = 4001
PORT_MAX = 4999


def site_dir(wolt_name: str) -> Path:
    """Get the site directory for a wolt."""
    return WOLTS_DIR / wolt_name / "wolt" / "site"


def _state_file(wolt_name: str) -> Path:
    return _RUNNING_STATE_DIR / f"{wolt_name}.json"


def _read_state(wolt_name: str) -> dict | None:
    f = _state_file(wolt_name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(wolt_name: str, state: dict) -> None:
    _RUNNING_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(wolt_name).write_text(json.dumps(state, indent=2) + "\n")


def _clear_state(wolt_name: str) -> None:
    f = _state_file(wolt_name)
    if f.exists():
        f.unlink()


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _used_ports() -> set[int]:
    """Collect all ports used by sites AND projects."""
    used = set()
    # Sites
    if _RUNNING_STATE_DIR.exists():
        for f in _RUNNING_STATE_DIR.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                state = json.loads(f.read_text())
                if state.get("port"):
                    used.add(state["port"])
            except (json.JSONDecodeError, OSError):
                continue
    # Projects — check their state dir too
    projects_state_dir = WOLTS_DIR / ".state" / "projects"
    if projects_state_dir.exists():
        for f in projects_state_dir.iterdir():
            if not f.name.endswith(".json"):
                continue
            try:
                state = json.loads(f.read_text())
                if state.get("port"):
                    used.add(state["port"])
            except (json.JSONDecodeError, OSError):
                continue
    return used


def _allocate_port() -> int:
    """Find the next available port in the shared range."""
    used = _used_ports()
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("No available ports in range")


def running_sites() -> list[dict]:
    """List all currently running wolt sites."""
    running = []
    if not _RUNNING_STATE_DIR.exists():
        return running
    for f in sorted(_RUNNING_STATE_DIR.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            state = json.loads(f.read_text())
            pid = state.get("pid")
            if pid and _is_pid_alive(pid):
                state["alive"] = True
                running.append(state)
            else:
                # Stale state — process died
                f.unlink()
        except (json.JSONDecodeError, OSError):
            continue
    return running


def get_site_state(wolt_name: str) -> dict | None:
    """Get the running state for a wolt's site, or None if not running."""
    state = _read_state(wolt_name)
    if not state:
        return None
    pid = state.get("pid")
    if pid and _is_pid_alive(pid):
        return state
    # Stale — clean up
    _clear_state(wolt_name)
    return None


def start_site(wolt_name: str) -> dict:
    """Start a livereload server for a wolt's site. Returns state dict.

    Idempotent — if already running, returns existing state.
    Creates the site dir if it doesn't exist (with a default index.html).
    """
    # Already running?
    existing = get_site_state(wolt_name)
    if existing:
        return existing

    sdir = site_dir(wolt_name)

    # Create site dir + default index if needed
    if not sdir.exists():
        sdir.mkdir(parents=True, exist_ok=True)

    if not (sdir / "index.html").exists():
        _write_default_index(wolt_name, sdir)

    port = _allocate_port()

    # Start livereload as a subprocess
    # livereload serves from the directory, watches for changes, injects WS reload script
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            f"from livereload import Server; s = Server(); "
            f"s.watch('{sdir}'); "
            f"s.serve(port={port}, host='127.0.0.1', root='{sdir}')",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    state = {
        "wolt": wolt_name,
        "port": port,
        "pid": proc.pid,
        "dir": str(sdir),
    }
    _write_state(wolt_name, state)
    return state


def stop_site(wolt_name: str) -> bool:
    """Stop a wolt's site livereload server. Returns True if it was running."""
    state = _read_state(wolt_name)
    if not state:
        return False
    pid = state.get("pid")
    if pid and _is_pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    _clear_state(wolt_name)
    return True


def _write_default_index(wolt_name: str, sdir: Path) -> None:
    """Write a simple default index.html for a new wolt site."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{wolt_name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #1a1a1a; color: #c8b89a;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .home {{
    max-width: 480px; width: 100%; text-align: center;
  }}
  h1 {{ font-size: 1.5rem; color: #e8d8b8; margin-bottom: 0.5rem; }}
  .sub {{ font-size: 0.85rem; color: #8a8060; margin-bottom: 2rem; }}
  .hint {{ font-size: 0.75rem; color: #5a5a4a; }}
</style>
</head>
<body>
<div class="home">
  <h1>{wolt_name}</h1>
  <p class="sub">Nothing here yet. Start a conversation and I'll build something.</p>
  <p class="hint">Pages I create will appear here as links.</p>
</div>
</body>
</html>"""
    (sdir / "index.html").write_text(html)
