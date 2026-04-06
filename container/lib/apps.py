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

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
APPS_DIR = WOLTS_DIR / "apps"

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
    """Scan wolts/apps/ for all apps with woltspace.json manifests."""
    apps = []
    if not APPS_DIR.exists():
        return apps
    for manifest in sorted(APPS_DIR.glob("*/" + MANIFEST)):
        app = load_app(manifest.parent)
        if app:
            apps.append(app)
    return apps


def app_dir(name: str) -> Path:
    """Get the directory for an app. Lives at wolts/apps/<name>/."""
    return APPS_DIR / name


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
    """List all currently running apps with their state."""
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


def stop_app(name: str) -> bool:
    """Stop a running app. Also kills any active tunnel. Returns True if it was running."""
    state = _read_state(name)
    if not state:
        return False
    # Kill tunnel first if running
    tunnel_pid = state.get("tunnel_pid")
    if tunnel_pid and _is_pid_alive(tunnel_pid):
        try:
            os.kill(tunnel_pid, signal.SIGTERM)
        except OSError:
            pass
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


def share_app(name: str) -> dict:
    """Start a cloudflared tunnel to the app port.

    Uses --http-host-header localhost so Vite 6+ allowedHosts checks pass
    without any app config changes.

    Stores tunnel_pid and tunnel_url in .space/apps/{name}.json.
    Also sets public=true in woltspace.json.
    Returns dict with tunnel_url and pid.
    Raises ValueError if app is not running.
    Raises RuntimeError if sharing is disabled or tunnel fails.
    """
    import re
    import tempfile
    import time

    _check_sharing_enabled()

    state = _read_state(name)
    if not state:
        raise ValueError(f"App {name} is not running")

    port = state["port"]

    # Return existing tunnel if still alive
    tunnel_pid = state.get("tunnel_pid")
    if tunnel_pid and _is_pid_alive(tunnel_pid):
        return {
            "tunnel_url": state.get("tunnel_url", ""),
            "pid": tunnel_pid,
        }

    # Start cloudflared tunnel with host header rewrite
    # --http-host-header localhost: rewrites Host header so dev servers
    # (Vite 6+, Next.js, etc.) see "localhost" and pass their allowedHosts check.
    log_file = tempfile.mktemp(suffix="-cloudflared.log")
    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            [
                "cloudflared", "tunnel",
                "--url", f"http://localhost:{port}",
                "--http-host-header", "localhost",
            ],
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Poll log for tunnel URL (up to 15s, checking every 0.5s)
    tunnel_url = ""
    for _ in range(30):
        time.sleep(0.5)
        try:
            with open(log_file) as f:
                content = f.read()
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
            if m:
                tunnel_url = m.group(0)
                break
        except Exception:
            pass

    if not tunnel_url:
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError("cloudflared tunnel failed to start — is cloudflared installed?")

    state["tunnel_pid"] = proc.pid
    state["tunnel_url"] = tunnel_url
    _write_state(name, state)

    # Persist public=true in woltspace.json
    _set_public(name, True)

    return {"tunnel_url": tunnel_url, "pid": proc.pid}


def unshare_app(name: str) -> bool:
    """Stop the cloudflared tunnel for an app.

    Sets public=false in woltspace.json.
    Returns True if a tunnel was running and stopped.
    """
    state = _read_state(name)
    if not state:
        return False

    tunnel_pid = state.get("tunnel_pid")
    stopped = False
    if tunnel_pid and _is_pid_alive(tunnel_pid):
        try:
            os.kill(tunnel_pid, signal.SIGTERM)
        except OSError:
            pass
        stopped = True

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
            if tunnel_pid and _is_pid_alive(tunnel_pid):
                try:
                    os.kill(tunnel_pid, signal.SIGTERM)
                except OSError:
                    pass
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
