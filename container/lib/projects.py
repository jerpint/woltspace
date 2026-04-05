"""
Woltspace project schema — defines the woltspace.json manifest.

Usage:
    from projects import WoltspaceProject, load_project, discover_projects
    from projects import project_dir, start_project, stop_project, running_projects

    project = load_project("/workspace/wolts/projects/forj")
    all_projects = discover_projects()
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from paths import space_projects_dir

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
PROJECTS_DIR = WOLTS_DIR / "projects"

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Running project state — now at .space/projects/
_RUNNING_STATE_DIR = space_projects_dir(WOLTS_DIR)

# Wolt type emojis — reserved, never used as random defaults
WOLT_EMOJIS = {"🦫", "🦝", "🦦", "🐺", "🐶", "🕷️", "🐻", "🐼"}

# Forest creatures — used as random defaults for new projects
FOREST_EMOJIS = ["🦅", "🦉", "🐿️", "🦊", "🐝", "🦌", "🐾", "🐸", "🦋", "🐛", "🪲", "🐞"]


def random_emoji() -> str:
    """Pick a random forest creature emoji for a new project."""
    import random
    return random.choice(FOREST_EMOJIS)


class WoltspaceProject(BaseModel):
    """v0.1 woltspace.json schema.

    Every project lives in wolts/projects/<name>/woltspace.json.
    Project names are globally unique. Keeper tracks ownership.
    Null fields = explicit todos. Can't start a project with start=None.
    """

    woltspace_version: str = Field(default="0.1", description="Schema version")
    name: str = Field(description="Project name (globally unique, matches directory name)")
    description: str | None = Field(default=None, description="What the project does")
    stack: str | None = Field(default=None, description="Tech stack: python, vite, node, html")
    install: str | None = Field(default=None, description="Install command (e.g. 'npm install', 'uv sync')")
    start: str | None = Field(default=None, description="Start command (e.g. 'node server.js'). Null = can't start.")
    port: int = Field(description="Fixed port for this project's dev server")
    source: str | None = Field(default=None, description="Origin wolt if cloned/forked, null if created locally")
    keeper: str = Field(description="Owning wolt name")
    emoji: str = Field(default_factory=random_emoji, description="Project emoji for display")
    public: bool = Field(default=False, description="Whether this project should be publicly shared via tunnel")

    def can_start(self) -> bool:
        """Project can only start if it has a start command."""
        return self.start is not None


def load_project(proj_dir: str | Path) -> WoltspaceProject | None:
    """Load a woltspace.json from a project directory. Returns None if missing or invalid."""
    manifest = Path(proj_dir) / MANIFEST
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        return WoltspaceProject(**data)
    except (json.JSONDecodeError, Exception):
        return None


def discover_projects() -> list[WoltspaceProject]:
    """Scan wolts/projects/ for all projects with woltspace.json manifests."""
    projects = []
    if not PROJECTS_DIR.exists():
        return projects
    for manifest in sorted(PROJECTS_DIR.glob("*/" + MANIFEST)):
        project = load_project(manifest.parent)
        if project:
            projects.append(project)
    return projects


def project_dir(name: str) -> Path:
    """Get the directory for a project. Lives at wolts/projects/<name>/."""
    return PROJECTS_DIR / name


def get_project(name: str) -> WoltspaceProject | None:
    """Load a specific project by name."""
    return load_project(project_dir(name))


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


def running_projects() -> list[dict]:
    """List all currently running projects with their state."""
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


def start_project(name: str) -> dict:
    """Start a project's dev server. Returns state dict with port and pid.

    Uses the port declared in woltspace.json — no dynamic allocation.
    """
    project = get_project(name)
    if not project:
        raise ValueError(f"Project {name} not found")
    if not project.can_start():
        raise ValueError(f"Project {name} has no start command")

    # Check if already running
    existing = _read_state(name)
    if existing and _is_pid_alive(existing.get("pid", 0)):
        return existing

    # Use port from manifest — check for conflicts with running projects
    port = project.port
    for r in running_projects():
        if r["port"] == port:
            raise RuntimeError(f"Port {port} already in use by running project '{r['name']}'")

    work_dir = project_dir(name)

    # Start the process with PORT env var
    env = {**os.environ, "PORT": str(port)}
    proc = subprocess.Popen(
        project.start,
        shell=True,
        cwd=str(work_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    state = {
        "name": name,
        "keeper": project.keeper,
        "port": port,
        "pid": proc.pid,
        "start_command": project.start,
    }
    _write_state(name, state)

    # Auto-share if public=true and sharing is enabled
    if project.public and SHARING_ENABLED:
        try:
            share_result = share_project(name)
            state["tunnel_url"] = share_result.get("tunnel_url")
            state["tunnel_pid"] = share_result.get("pid")
        except Exception:
            pass  # Non-fatal — project starts even if tunnel fails

    return state


def stop_project(name: str) -> bool:
    """Stop a running project. Also kills any active tunnel. Returns True if it was running."""
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
    # Kill the project process
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


def share_project(name: str) -> dict:
    """Start a cloudflared tunnel to the project port.

    Uses --http-host-header localhost so Vite 6+ allowedHosts checks pass
    without any project config changes.

    Stores tunnel_pid and tunnel_url in .space/projects/{name}.json.
    Also sets public=true in woltspace.json.
    Returns dict with tunnel_url and pid.
    Raises ValueError if project is not running.
    Raises RuntimeError if sharing is disabled or tunnel fails.
    """
    import re
    import tempfile
    import time

    _check_sharing_enabled()

    state = _read_state(name)
    if not state:
        raise ValueError(f"Project {name} is not running")

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


def unshare_project(name: str) -> bool:
    """Stop the cloudflared tunnel for a project.

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


def unshare_all_projects() -> list[str]:
    """Panic button — stop ALL cloudflared tunnels across all projects.

    Returns list of project names that were unshared.
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
    """Update the public field in a project's woltspace.json."""
    manifest = project_dir(name) / MANIFEST
    if not manifest.exists():
        return
    try:
        data = json.loads(manifest.read_text())
        data["public"] = public
        manifest.write_text(json.dumps(data, indent=2) + "\n")
    except (json.JSONDecodeError, OSError):
        pass
