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
import threading
import time
from functools import wraps
from pathlib import Path
from woltspace.app_gateway_port import resolve_app_gateway_port

from pydantic import BaseModel, Field, ValidationError

from env_compat import get_env
from paths import space_apps_dir
from tunnel import is_cloudflared, start_cloudflared, stop_cloudflared

WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
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
    # Ignored, kept so existing manifests still load. A manifest is editable by
    # any wolt, so it can never be what publishes an app.
    public: bool = Field(default=False, description="Ignored; share through the gateway or an opt-in quick tunnel")

    def can_start(self) -> bool:
        """App can only start if it has a start command."""
        return self.start is not None


def load_app(app_path: str | Path) -> WoltspaceApp | None:
    """Load a woltspace.json from an app directory. Returns None if missing or invalid."""
    manifest = Path(app_path) / MANIFEST
    if not manifest.exists():
        return None
    app, _error = load_app_result(app_path)
    return app


def load_app_result(app_path: str | Path) -> tuple[WoltspaceApp | None, str | None]:
    """Load an app and preserve a safe, human-readable validation failure."""
    manifest = Path(app_path) / MANIFEST
    if not manifest.exists():
        return None, None
    try:
        data = json.loads(manifest.read_text())
        return WoltspaceApp(**data), None
    except json.JSONDecodeError as exc:
        return None, f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
    except ValidationError as exc:
        details = []
        for error in exc.errors(include_url=False, include_context=False):
            field = ".".join(str(part) for part in error["loc"])
            details.append(f"{field}: {error['msg']}")
        return None, "; ".join(details)
    except OSError as exc:
        return None, f"Could not read woltspace.json: {exc.strerror or exc}"
    except Exception as exc:
        return None, f"Invalid woltspace.json: {exc}"


def discover_apps_with_errors() -> tuple[list[WoltspaceApp], list[dict[str, str]]]:
    """Discover valid apps and report manifests that need owner attention."""
    apps = []
    invalid = []
    seen_names: set[str] = set()
    for search_dir in (APPS_DIR, LEGACY_PROJECTS_DIR):
        if not search_dir.exists():
            continue
        for manifest in sorted(search_dir.glob("*/" + MANIFEST)):
            app, error = load_app_result(manifest.parent)
            if app and app.name not in seen_names:
                seen_names.add(app.name)
                apps.append(app)
            elif error and manifest.parent.name not in seen_names:
                seen_names.add(manifest.parent.name)
                invalid.append({"name": manifest.parent.name, "error": error})
    return apps, invalid


def discover_apps() -> list[WoltspaceApp]:
    """Scan app directories and return manifests that pass validation."""
    apps, _invalid = discover_apps_with_errors()
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


def _lodge_port() -> int:
    """The port the lodge itself listens on."""
    try:
        return int(os.environ.get("WOLTSPACE_PORT") or os.environ.get("PORT") or "7777")
    except ValueError:
        return 7777


def _enabled_app_gateway_port() -> int | None:
    """Return the always-reserved gateway port."""
    return resolve_app_gateway_port(WOLTS_DIR, _lodge_port())


# --- Per-app lifecycle lock ---
#
# Start, stop, share, unshare and keeper changes each read an app's state,
# act (spawn or stop a process), then write it back. Two at once could each
# launch a tunnel and leave only one recorded. One re-entrant lock per app
# serializes them (restore and restart call start/stop under it).

_APP_LOCKS: dict[str, threading.RLock] = {}
_APP_LOCKS_GUARD = threading.Lock()


def _app_lock(name: str) -> threading.RLock:
    with _APP_LOCKS_GUARD:
        return _APP_LOCKS.setdefault(name, threading.RLock())


def _locked_per_app(func):
    @wraps(func)
    def wrapper(name, *args, **kwargs):
        with _app_lock(name):
            return func(name, *args, **kwargs)
    return wrapper


# --- Running state ---


def _state_file(name: str) -> Path:
    return _RUNNING_STATE_DIR / f"{name}.json"


def app_log_file(name: str) -> Path:
    """Return the package-owned log path for an app process."""
    return _RUNNING_STATE_DIR / f"{name}.log"


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


@_locked_per_app
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
        return _revoke_disallowed_tunnel(name, existing)
    # A dead app's recorded tunnel points at nothing, and writing the new state
    # would forget it. Close it first; refuse to start while one survives.
    if existing and existing.get("tunnel_pid"):
        if not _close_recorded_tunnel(name, existing["tunnel_pid"], "the app stopped"):
            raise RuntimeError(
                f"could not stop the old quick tunnel for {name} (pid {existing['tunnel_pid']})"
            )

    # Use port from manifest — check for conflicts with running apps
    port = app.port
    if port == _lodge_port():
        raise RuntimeError(f"port {port} is the lodge's own port; change the app's port")
    gateway_port = _enabled_app_gateway_port()
    if port == gateway_port:
        raise RuntimeError(
            f"port {port} is used by the app gateway; "
            "change the app's port or the gateway port in Settings"
        )
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
    # HOST/HOSTNAME ask the common dev servers (Vite, Next.js, many others) to
    # bind loopback only: the gateway is the app's only public face.
    env = {**os.environ, "PORT": str(port), "HOST": "127.0.0.1", "HOSTNAME": "127.0.0.1"}
    _RUNNING_STATE_DIR.mkdir(parents=True, exist_ok=True)
    log_handle = app_log_file(name).open("ab", buffering=0)
    try:
        proc = subprocess.Popen(
            app.start,
            shell=True,
            cwd=str(work_dir),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_handle.close()

    state = {
        "name": name,
        "keeper": app.keeper,
        "port": port,
        "pid": proc.pid,
        "start_command": app.start,
    }
    _write_state(name, state)
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
        with _app_lock(f.stem):
            _restore_one(f, actions)
    return actions


def _restore_one(f: Path, actions: list[dict]) -> None:
    """Restore one app's recorded state. Runs under that app's lock."""
    name = f.stem
    try:
        state = json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(state, dict):
        return
    # A quick tunnel recorded by an earlier lodge (or before the owner
    # switched them off) is closed if today's policy would refuse it.
    state = _revoke_disallowed_tunnel(name, state)

    # Manifest gone — app was removed while container was down. Its tunnel
    # goes first: once the record is deleted nothing could find it again.
    if get_app(name) is None:
        if state.get("tunnel_pid") and not _close_recorded_tunnel(
            name, state["tunnel_pid"], "the app was removed",
        ):
            actions.append({"name": name, "action": "orphan-kept", "reason": "tunnel survived"})
            return
        try:
            f.unlink()
            actions.append({"name": name, "action": "orphan-cleaned"})
            print(f"[apps] orphan {name} cleaned (no manifest)")
        except OSError:
            pass
        return

    pid = state.get("pid")
    if pid and _is_pid_alive(pid):
        actions.append({"name": name, "action": "survived", "pid": pid})
        print(f"[apps] {name} survived (pid {pid})")
        return

    # Dead PID — respawn (start_app closes the dead app's recorded tunnel).
    try:
        new_state = start_app(name)
        actions.append({"name": name, "action": "restored", "pid": new_state["pid"]})
        print(f"[apps] restored {name} on port {new_state['port']} (new pid {new_state['pid']})")
    except Exception as e:
        actions.append({"name": name, "action": "restore-failed", "error": str(e)})
        print(f"[apps] restore failed for {name}: {e}")


@_locked_per_app
def stop_app(name: str) -> bool:
    """Stop a running app. Also kills any active tunnel. Returns True if it was running."""
    state = _read_state(name)
    if not state:
        return False
    # Close the tunnel first. If it survives, its record must survive too.
    tunnel_pid = state.get("tunnel_pid")
    tunnel_gone = not tunnel_pid or _close_recorded_tunnel(name, tunnel_pid, "the app was stopped")
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
    if tunnel_gone:
        _clear_state(name)
    # Otherwise the record stays (app dead, tunnel alive): start_app refuses
    # to replace it until the tunnel is gone, and unshare can still find it.
    return True


@_locked_per_app
def restart_app(name: str) -> dict:
    """Restart an app through the same lifecycle primitives as start/stop."""
    if not get_app(name):
        raise ValueError(f"App {name} not found")
    previous = _read_state(name) or {}
    previous_pid = previous.get("pid", 0)
    stop_app(name)
    deadline = time.monotonic() + 5
    while previous_pid and _is_pid_alive(previous_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if previous_pid and _is_pid_alive(previous_pid):
        raise RuntimeError(f"App {name} is still stopping")
    state = start_app(name)
    if not _is_pid_alive(state.get("pid", 0)):
        raise RuntimeError(f"App {name} failed to stay running")
    return state


@_locked_per_app
def set_app_keeper(name: str, keeper: str) -> WoltspaceApp:
    """Atomically update the keeper in an app manifest."""
    path = app_dir(name) / MANIFEST
    app = get_app(name)
    if not app:
        raise ValueError(f"App {name} not found")
    data = json.loads(path.read_text())
    data["keeper"] = keeper
    updated = WoltspaceApp(**data)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(updated.model_dump(), indent=2) + "\n")
    os.replace(tmp, path)
    state = _read_state(name)
    if state:
        state["keeper"] = keeper
        _write_state(name, state)
    return updated


# --- Quick tunnels (opt-in, apps only) ---
#
# A quick tunnel gives anyone with its random link the app, with no login and
# no share list. It is never for the lodge, and it is off unless the human who
# owns the lodge switches it on in the lodge's .env. A manifest (which any
# wolt can edit) can't turn it on. Normal sharing goes through the gateway's
# share list.

QUICK_TUNNELS_ENV = "WOLTSPACE_APP_QUICK_TUNNELS"


class ShareRefused(RuntimeError):
    """The lodge will not open a quick tunnel for this request."""


class QuickTunnelsOff(ShareRefused):
    """App quick tunnels are not switched on for this lodge."""


def quick_tunnels_enabled() -> bool:
    return os.environ.get(QUICK_TUNNELS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _valid_port(value) -> int | None:
    """An integer TCP port, or None. App state is plain JSON: never trust it."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
        return value
    return None


def _tunnel_refusal(state: dict) -> str | None:
    """Why the current policy refuses a quick tunnel for this state, if it does."""
    port = _valid_port(state.get("port"))
    if port is None:
        return "the app has no valid port"
    if port in (_lodge_port(), _enabled_app_gateway_port()):
        return f"port {port} belongs to the lodge"
    if not quick_tunnels_enabled():
        return "quick tunnels are off"
    return None


TUNNEL_EXIT_TIMEOUT = 3.0


def _close_recorded_tunnel(name: str, tunnel_pid, reason: str) -> bool:
    """Stop a recorded quick tunnel. True once it is confirmed gone.

    False when a live cloudflared with that pid is still running (it could not
    be signalled, or did not exit in time): the caller must then keep the
    record, so the tunnel can still be found.
    """
    if stop_cloudflared(tunnel_pid):
        # Signalled is not exited: wait, bounded, for it to go.
        deadline = time.monotonic() + TUNNEL_EXIT_TIMEOUT
        while is_cloudflared(tunnel_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not is_cloudflared(tunnel_pid):
            print(f"[apps] closed quick tunnel for {name} (pid {tunnel_pid}, {reason})")
            return True
        print(f"[apps] WARNING: quick tunnel for {name} (pid {tunnel_pid}, {reason}) "
              "did not exit; record kept")
        return False
    if is_cloudflared(tunnel_pid):
        print(f"[apps] WARNING: could not stop quick tunnel for {name} "
              f"(pid {tunnel_pid}, {reason}); record kept")
        return False
    print(f"[apps] dropped stale quick tunnel record for {name} ({reason})")
    return True


def _revoke_disallowed_tunnel(name: str, state: dict) -> dict:
    """Close a recorded quick tunnel the current policy no longer allows."""
    tunnel_pid = state.get("tunnel_pid")
    if not tunnel_pid:
        return state
    reason = _tunnel_refusal(state)
    if reason is None:
        return state
    if not _close_recorded_tunnel(name, tunnel_pid, reason):
        return state
    state = {**state, "tunnel_pid": None, "tunnel_url": None}
    _write_state(name, state)
    return state


@_locked_per_app
def share_app(name: str) -> dict:
    """Open a quick tunnel to a running app, if the lodge owner allows them.

    Stores tunnel_pid and tunnel_url in .space/apps/{name}.json.
    Returns dict with tunnel_url and pid.
    Raises ValueError if app is not running, QuickTunnelsOff if quick tunnels
    are not switched on, RuntimeError if the tunnel fails.
    """
    state = _read_state(name)
    if not state:
        raise ValueError(f"App {name} is not running")
    if not quick_tunnels_enabled():
        raise QuickTunnelsOff(
            f"Quick tunnels are off. They give anyone with the link the app, with no login. "
            f"The lodge owner can allow them with {QUICK_TUNNELS_ENV}=1 in the lodge's .env."
        )

    # Never a tunnel to the lodge or the gateway, whatever an app's state says:
    # the lodge is only ever published through a named, Access-gated tunnel.
    port = _valid_port(state.get("port"))
    if port is None:
        raise ShareRefused(f"Refusing a quick tunnel for {name}: it has no valid port")
    if port in (_lodge_port(), _enabled_app_gateway_port()):
        raise ShareRefused(f"Refusing a quick tunnel to port {port}: it belongs to the lodge")

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
    return {"tunnel_url": result["url"], "pid": result["pid"]}


@_locked_per_app
def unshare_app(name: str) -> bool:
    """Stop the cloudflared tunnel for an app.

    Returns True if a tunnel was running and stopped.
    """
    state = _read_state(name)
    if not state:
        return False

    tunnel_pid = state.get("tunnel_pid")
    if not tunnel_pid:
        return False
    if not _close_recorded_tunnel(name, tunnel_pid, "unshared"):
        raise RuntimeError(f"could not stop the quick tunnel for {name} (pid {tunnel_pid})")
    state["tunnel_pid"] = None
    state["tunnel_url"] = None
    _write_state(name, state)
    return True


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
        name = f.stem
        with _app_lock(name):
            state = _read_state(name)
            tunnel_pid = state.get("tunnel_pid") if state else None
            if tunnel_pid and _close_recorded_tunnel(name, tunnel_pid, "unshare all"):
                state["tunnel_pid"] = None
                state["tunnel_url"] = None
                _write_state(name, state)
                unshared.append(name)
    return unshared
