"""
Woltspace app schema — defines the woltspace.json manifest.

Usage:
    from apps import WoltspaceApp, load_app, discover_apps
    from apps import app_dir, start_app, stop_app, running_apps

    app = load_app("/workspace/wolts/apps/forj")
    all_apps = discover_apps()
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from paths import space_apps_dir
from tunnel import start_cloudflared, stop_cloudflared

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
APPS_DIR = WOLTS_DIR / "apps"
LEGACY_PROJECTS_DIR = WOLTS_DIR / "projects"  # deprecated — still discovered for backwards compat

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Running app state — now at .space/apps/
_RUNNING_STATE_DIR = space_apps_dir(WOLTS_DIR)

# Wolt type emojis — reserved, never used as random defaults
WOLT_EMOJIS = {"🦫", "🦝", "🦦", "🐺", "🐶", "🕷️", "🐻", "🐼"}

# Forest creatures — used as random defaults for new apps
FOREST_EMOJIS = ["🦅", "🦉", "🐿️", "🦊", "🐝", "🦌", "🐾", "🐸", "🦋", "🐛", "🪲", "🐞"]


def random_emoji() -> str:
    """Pick a random forest creature emoji for a new app."""
    import random
    return random.choice(FOREST_EMOJIS)


class WoltspaceApp(BaseModel):
    """v0.1 woltspace.json schema.

    Every app lives in wolts/apps/<name>/woltspace.json.
    App names are globally unique. Keeper tracks ownership.
    Null fields = explicit todos. Can't start an app with start=None.
    """

    woltspace_version: str = Field(default="0.1", description="Schema version")
    name: str = Field(description="App name (globally unique, matches directory name)")
    description: str | None = Field(default=None, description="What the app does")
    stack: str | None = Field(default=None, description="Tech stack: python, vite, node, html")
    install: str | None = Field(default=None, description="Install command (e.g. 'npm install', 'uv sync')")
    start: str | None = Field(default=None, description="Start command (e.g. 'node server.js'). Null = can't start.")
    port: int = Field(description="Fixed port for this app's dev server")
    source: str | None = Field(default=None, description="Origin wolt if cloned/forked, null if created locally")
    keeper: str = Field(description="Owning wolt name")
    emoji: str = Field(default_factory=random_emoji, description="App emoji for display")
    public: bool = Field(default=False, description="Whether this app should be publicly shared via tunnel")

    def can_start(self) -> bool:
        """App can only start if it has a start command."""
        return self.start is not None


def load_app(app_path: str | Path) -> WoltspaceApp | None:
    """Load a woltspace.json from an app directory. Returns None if missing or invalid."""
    manifest = Path(app_path) / MANIFEST
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        return WoltspaceApp(**data)
    except (json.JSONDecodeError, Exception):
        return None


def discover_apps() -> list[WoltspaceApp]:
    """Scan wolts/apps/ and wolts/projects/ (legacy) for all apps with woltspace.json manifests."""
    apps = []
    seen_names: set[str] = set()
    # Primary: wolts/apps/
    for search_dir in (APPS_DIR, LEGACY_PROJECTS_DIR):
        if not search_dir.exists():
            continue
        for manifest in sorted(search_dir.glob("*/" + MANIFEST)):
            app = load_app(manifest.parent)
            if app and app.name not in seen_names:
                seen_names.add(app.name)
                apps.append(app)
    return apps


def app_dir(name: str) -> Path:
    """Get the directory for an app. Checks wolts/apps/ first, falls back to wolts/projects/."""
    primary = APPS_DIR / name
    if primary.exists():
        return primary
    legacy = LEGACY_PROJECTS_DIR / name
    if legacy.exists():
        return legacy
    # Default to the new location for new apps
    return primary


def get_app(name: str) -> WoltspaceApp | None:
    """Load a specific app by name."""
    return load_app(app_dir(name))


# --- Running state ---


def _state_file(name: str) -> Path:
    return _RUNNING_STATE_DIR / f"{name}.json"


def _read_state(name: str) -> dict | None:
    f = _state_file(name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(name: str, state: dict) -> None:
    _RUNNING_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(name).write_text(json.dumps(state, indent=2) + "\n")


def _clear_state(name: str) -> None:
    f = _state_file(name)
    if f.exists():
        f.unlink()


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_apps() -> list[dict]:
    """List all currently running apps with their state.

    State file semantics (intent model):
    - file present + PID alive = running
    - file present + PID dead  = wanted but down (apps_restore will respawn at next boot)
    - no file                  = explicitly off

    We do NOT delete stale files here. Deletion only happens on explicit stop_app()
    or when the manifest is gone (cleaned up by apps_restore).
    """
    running = []
    if not _RUNNING_STATE_DIR.exists():
        return running
    for f in sorted(_RUNNING_STATE_DIR.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            state = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = state.get("pid")
        if pid and _is_pid_alive(pid):
            state["alive"] = True
            running.append(state)
    return running


def intended_apps() -> list[dict]:
    """List all apps the user has expressed intent to run (state file present).

    Returns state dicts with an added `alive` bool indicating actual running status.
    Used by apps_restore() and by callers that want to show stale-intent apps.
    """
    intended = []
    if not _RUNNING_STATE_DIR.exists():
        return intended
    for f in sorted(_RUNNING_STATE_DIR.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            state = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        pid = state.get("pid")
        state["alive"] = bool(pid and _is_pid_alive(pid))
        intended.append(state)
    return intended


def start_app(name: str) -> dict:
    """Start an app's dev server. Returns state dict with port and pid.

    Uses the port declared in woltspace.json — no dynamic allocation.
    """
    app = get_app(name)
    if not app:
        raise ValueError(f"App {name} not found")
    if not app.can_start():
        raise ValueError(f"App {name} has no start command")

    # Check if already running
    existing = _read_state(name)
    if existing and _is_pid_alive(existing.get("pid", 0)):
        return existing

    # Use port from manifest — check for conflicts with running apps
    port = app.port
    for r in running_apps():
        if r["port"] == port:
            raise RuntimeError(f"Port {port} already in use by running app '{r['name']}'")

    work_dir = app_dir(name)

    # Auto-install if node_modules is missing and install command is defined
    # Covers first-start after a container rebuild (node_modules cleared in entrypoint)
    if app.install and app.stack in ("node", "vite"):
        nm = work_dir / "node_modules"
        if not nm.exists():
            subprocess.run(app.install, shell=True, cwd=str(work_dir), check=True)

    # Start the process with PORT env var
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        app.start,
        shell=True,
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    state = {
        "name": name,
        "keeper": app.keeper,
        "port": port,
        "pid": proc.pid,
        "start_command": app.start,
    }
    _write_state(name, state)

    # Auto-share if public=true and sharing is enabled
    if app.public and SHARING_ENABLED:
        try:
            share_result = share_app(name)
            state["tunnel_url"] = share_result.get("tunnel_url")
            state["tunnel_pid"] = share_result.get("pid")
        except Exception:
            pass  # Non-fatal — app starts even if tunnel fails

    return state


def apps_restore() -> list[dict]:
    """Restore apps on container boot.

    For each state file in .space/apps/:
    - Manifest missing  → orphan, delete state file
    - PID alive         → leave it (survived the restart)
    - PID dead          → respawn via start_app()

    Returns a summary list of actions for logging.
    """
    actions = []
    if not _RUNNING_STATE_DIR.exists():
        return actions
    for f in sorted(_RUNNING_STATE_DIR.iterdir()):
        if not f.name.endswith(".json"):
            continue
        name = f.stem
        try:
            state = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Manifest gone — app was removed while container was down
        if get_app(name) is None:
            try:
                f.unlink()
                actions.append({"name": name, "action": "orphan-cleaned"})
                print(f"[apps] orphan {name} cleaned (no manifest)")
            except OSError:
                pass
            continue

        pid = state.get("pid")
        if pid and _is_pid_alive(pid):
            actions.append({"name": name, "action": "survived", "pid": pid})
            print(f"[apps] {name} survived (pid {pid})")
            continue

        # Dead PID — respawn
        try:
            new_state = start_app(name)
            actions.append({"name": name, "action": "restored", "pid": new_state["pid"]})
            print(f"[apps] restored {name} on port {new_state['port']} (new pid {new_state['pid']})")
        except Exception as e:
            actions.append({"name": name, "action": "restore-failed", "error": str(e)})
            print(f"[apps] restore failed for {name}: {e}")
    return actions


def stop_app(name: str) -> bool:
    """Stop a running app. Also kills any active tunnel. Returns True if it was running."""
    state = _read_state(name)
    if not state:
        return False
    # Kill tunnel first if running
    tunnel_pid = state.get("tunnel_pid")
    if tunnel_pid:
        stop_cloudflared(tunnel_pid)
    # Kill the app process
    pid = state.get("pid")
    if pid and _is_pid_alive(pid):
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    _clear_state(name)
    return True


# --- Sharing (cloudflared tunnels) ---

# Env var kill switch — if set to anything falsy, sharing is disabled entirely.
# Default: sharing is enabled (no env var needed).
SHARING_ENABLED = os.environ.get("WOLTSPACE_SHARING_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def _check_sharing_enabled():
    """Raise if sharing is disabled by env var."""
    if not SHARING_ENABLED:
        raise RuntimeError("Sharing is disabled (WOLTSPACE_SHARING_ENABLED=0)")


def _get_subdomain_url(app_name: str) -> str | None:
    """Return the wildcard subdomain URL for an app, or None if not available."""
    tunnel_url = os.environ.get("CLOUDFLARE_TUNNEL_URL", "")
    if not tunnel_url:
        return None
    from urllib.parse import urlparse
    parsed = urlparse(tunnel_url)
    hostname = parsed.hostname or ""
    parts = hostname.split(".", 1)
    if len(parts) != 2:
        return None
    return f"{parsed.scheme}://{app_name}.{parts[1]}"


def share_app(name: str) -> dict:
    """Share an app publicly.

    With a named tunnel + custom domain: returns the subdomain URL
    (e.g. corework.woltspace.com) — no per-app tunnel needed.

    Without a custom domain: falls back to a per-app quick tunnel
    with --http-host-header localhost for Vite 6+ compatibility.

    Stores tunnel_pid and tunnel_url in .space/apps/{name}.json.
    Also sets public=true in woltspace.json.
    Returns dict with tunnel_url and pid.
    Raises ValueError if app is not running.
    Raises RuntimeError if sharing is disabled or tunnel fails.
    """
    _check_sharing_enabled()

    state = _read_state(name)
    if not state:
        raise ValueError(f"App {name} is not running")

    # Subdomain routing: stable URL, no per-app tunnel needed
    subdomain_url = _get_subdomain_url(name)
    if subdomain_url:
        state["tunnel_url"] = subdomain_url
        state["tunnel_pid"] = None
        _write_state(name, state)
        _set_public(name, True)
        return {"tunnel_url": subdomain_url, "pid": None}

    # Fallback: per-app quick tunnel
    port = state["port"]

    # Return existing tunnel if still alive
    tunnel_pid = state.get("tunnel_pid")
    if tunnel_pid and _is_pid_alive(tunnel_pid):
        return {
            "tunnel_url": state.get("tunnel_url", ""),
            "pid": tunnel_pid,
        }

    result = start_cloudflared(port=port, host_header="localhost")

    state["tunnel_pid"] = result["pid"]
    state["tunnel_url"] = result["url"]
    _write_state(name, state)

    # Persist public=true in woltspace.json
    _set_public(name, True)

    return {"tunnel_url": result["url"], "pid": result["pid"]}


def unshare_app(name: str) -> bool:
    """Stop the cloudflared tunnel for an app.

    Sets public=false in woltspace.json.
    Returns True if a tunnel was running and stopped.
    """
    state = _read_state(name)
    if not state:
        return False

    tunnel_pid = state.get("tunnel_pid")
    stopped = stop_cloudflared(tunnel_pid) if tunnel_pid else False

    state["tunnel_pid"] = None
    state["tunnel_url"] = None
    _write_state(name, state)

    # Persist public=false in woltspace.json
    _set_public(name, False)

    return stopped or tunnel_pid is not None


def unshare_all_apps() -> list[str]:
    """Panic button — stop ALL cloudflared tunnels across all apps.

    Returns list of app names that were unshared.
    """
    unshared = []
    if not _RUNNING_STATE_DIR.exists():
        return unshared
    for f in sorted(_RUNNING_STATE_DIR.iterdir()):
        if not f.name.endswith(".json"):
            continue
        try:
            state = json.loads(f.read_text())
            name = state.get("name", f.stem)
            tunnel_pid = state.get("tunnel_pid")
            if tunnel_pid and stop_cloudflared(tunnel_pid):
                state["tunnel_pid"] = None
                state["tunnel_url"] = None
                _write_state(name, state)
                _set_public(name, False)
                unshared.append(name)
        except (json.JSONDecodeError, OSError):
            continue
    return unshared


def _set_public(name: str, public: bool) -> None:
    """Update the public field in an app's woltspace.json."""
    manifest = app_dir(name) / MANIFEST
    if not manifest.exists():
        return
    try:
        data = json.loads(manifest.read_text())
        data["public"] = public
        manifest.write_text(json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError):
        pass
