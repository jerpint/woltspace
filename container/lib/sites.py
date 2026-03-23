"""
Wolt site management — each wolt gets a livereload-powered static site.

Per-wolt state model: site state lives at wolts/{wolt}/.state/site.json.
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

from paths import wolt_site_state_file, space_projects_dir

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

# Port range 4001-4999, shared with projects
PORT_MIN = 4001
PORT_MAX = 4999


def site_dir(wolt_name: str) -> Path:
    """Get the site directory for a wolt."""
    return WOLTS_DIR / wolt_name / "wolt" / "site"


def _state_file(wolt_name: str) -> Path:
    return wolt_site_state_file(wolt_name, WOLTS_DIR)


def _read_state(wolt_name: str) -> dict | None:
    f = _state_file(wolt_name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(wolt_name: str, state: dict) -> None:
    f = _state_file(wolt_name)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2) + "\n")


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
    # Sites — scan per-wolt state
    for wolt_dir in WOLTS_DIR.iterdir():
        if not wolt_dir.is_dir() or wolt_dir.name.startswith("."):
            continue
        site_state = wolt_dir / ".state" / "site.json"
        if site_state.exists():
            try:
                state = json.loads(site_state.read_text())
                if state.get("port"):
                    used.add(state["port"])
            except (json.JSONDecodeError, OSError):
                continue
    # Projects — check .space/projects/
    projects_dir = space_projects_dir(WOLTS_DIR)
    if projects_dir.exists():
        for f in projects_dir.iterdir():
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
    for wolt_dir in sorted(WOLTS_DIR.iterdir()):
        if not wolt_dir.is_dir() or wolt_dir.name.startswith("."):
            continue
        site_state = wolt_dir / ".state" / "site.json"
        if not site_state.exists():
            continue
        try:
            state = json.loads(site_state.read_text())
            pid = state.get("pid")
            if pid and _is_pid_alive(pid):
                state["alive"] = True
                running.append(state)
            else:
                # Stale state — process died
                site_state.unlink()
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
    existing = get_site_state(wolt_name)
    if existing:
        return existing

    sdir = site_dir(wolt_name)

    if not sdir.exists():
        sdir.mkdir(parents=True, exist_ok=True)

    if not (sdir / "index.html").exists():
        _write_default_index(wolt_name, sdir)

    port = _allocate_port()

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
    """Write a wakeup template index.html for a new wolt site."""
    from wolts import _wakeup_template, _get_wolt_type
    creature_type = _get_wolt_type(wolt_name)
    (sdir / "index.html").write_text(_wakeup_template(wolt_name, creature_type))
