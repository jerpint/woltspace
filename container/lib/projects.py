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
import random
import signal
import subprocess
from pathlib import Path

from pydantic import BaseModel, Field

from paths import space_projects_dir

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
PROJECTS_DIR = WOLTS_DIR / "projects"

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Port range for project dev servers
PORT_MIN = 4001
PORT_MAX = 4999
MAX_RUNNING = 2

# Running project state — now at .space/projects/
_RUNNING_STATE_DIR = space_projects_dir(WOLTS_DIR)

# Wolt type emojis — reserved, never used as random defaults
WOLT_EMOJIS = {"🦫", "🦝", "🦦", "🐺", "🐶", "🕷️", "🐻", "🐼"}

# Forest creatures — used as random defaults for new projects
FOREST_EMOJIS = ["🦅", "🦉", "🐿️", "🦊", "🐝", "🦌", "🐾", "🐸", "🦋", "🐛", "🪲", "🐞"]


def random_emoji() -> str:
    """Pick a random forest creature emoji for a new project."""
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
    source: str | None = Field(default=None, description="Origin wolt if cloned/forked, null if created locally")
    keeper: str = Field(description="Owning wolt name")
    emoji: str = Field(default_factory=random_emoji, description="Project emoji for display")

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


def _allocate_port() -> int:
    """Find the next available port in the project range."""
    used = {r["port"] for r in running_projects()}
    for port in range(PORT_MIN, PORT_MAX + 1):
        if port not in used:
            return port
    raise RuntimeError("No available ports in project range")


def start_project(name: str) -> dict:
    """Start a project's dev server. Returns state dict with port and pid."""
    project = get_project(name)
    if not project:
        raise ValueError(f"Project {name} not found")
    if not project.can_start():
        raise ValueError(f"Project {name} has no start command")

    # Check if already running
    existing = _read_state(name)
    if existing and _is_pid_alive(existing.get("pid", 0)):
        return existing

    # Check concurrency limit
    current = running_projects()
    if len(current) >= MAX_RUNNING:
        names = [r["name"] for r in current]
        raise RuntimeError(f"Max {MAX_RUNNING} running projects. Stop one first: {', '.join(names)}")

    port = _allocate_port()
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
    return state


def stop_project(name: str) -> bool:
    """Stop a running project. Returns True if it was running."""
    state = _read_state(name)
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
    _clear_state(name)
    return True
