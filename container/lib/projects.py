"""
Woltspace project schema — defines the woltspace.json manifest.

Usage:
    from projects import WoltspaceProject, load_project, discover_projects
    from projects import project_dir, start_project, stop_project, running_projects

    project = load_project("/workspace/wolts/neowolt/wolt/projects/forj")
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

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Port range for project dev servers
PORT_MIN = 4001
PORT_MAX = 4999
MAX_RUNNING = 2

# Running project state — keyed by "keeper/name"
_RUNNING_STATE_DIR = WOLTS_DIR / ".state" / "projects"

# Wolt type emojis — reserved, never used as random defaults
WOLT_EMOJIS = {"🦫", "🦝", "🦦", "🐺", "🐶", "🕷️", "🐻", "🐼"}

# Forest creatures — used as random defaults for new projects
FOREST_EMOJIS = ["🦅", "🦉", "🐿️", "🦊", "🐝", "🦌", "🐾", "🐸", "🦋", "🐛", "🪲", "🐞"]


def random_emoji() -> str:
    """Pick a random forest creature emoji for a new project."""
    return random.choice(FOREST_EMOJIS)


class WoltspaceProject(BaseModel):
    """v0.1 woltspace.json schema.

    Every project lives in wolts/<wolt>/wolt/projects/<name>/woltspace.json.
    Wolt populates the manifest, platform enforces completeness.
    Null fields = explicit todos. Can't start a project with start=None.
    """

    woltspace_version: str = Field(default="0.1", description="Schema version")
    name: str = Field(description="Project name (matches directory name)")
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


def load_project(project_dir: str | Path) -> WoltspaceProject | None:
    """Load a woltspace.json from a project directory. Returns None if missing or invalid."""
    manifest = Path(project_dir) / MANIFEST
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        return WoltspaceProject(**data)
    except (json.JSONDecodeError, Exception):
        return None


def discover_projects() -> list[WoltspaceProject]:
    """Scan all wolts for projects with woltspace.json manifests."""
    projects = []
    for manifest in sorted(WOLTS_DIR.glob("*/wolt/projects/*/" + MANIFEST)):
        project = load_project(manifest.parent)
        if project:
            projects.append(project)
    return projects


def project_dir(keeper: str, name: str) -> Path:
    """Get the directory for a project."""
    return WOLTS_DIR / keeper / "wolt" / "projects" / name


def get_project(keeper: str, name: str) -> WoltspaceProject | None:
    """Load a specific project by keeper and name."""
    return load_project(project_dir(keeper, name))


# --- Running state ---


def _state_file(keeper: str, name: str) -> Path:
    return _RUNNING_STATE_DIR / f"{keeper}--{name}.json"


def _read_state(keeper: str, name: str) -> dict | None:
    f = _state_file(keeper, name)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(keeper: str, name: str, state: dict) -> None:
    _RUNNING_STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_file(keeper, name).write_text(json.dumps(state, indent=2) + "\n")


def _clear_state(keeper: str, name: str) -> None:
    f = _state_file(keeper, name)
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


def start_project(keeper: str, name: str) -> dict:
    """Start a project's dev server. Returns state dict with port and pid."""
    project = get_project(keeper, name)
    if not project:
        raise ValueError(f"Project {keeper}/{name} not found")
    if not project.can_start():
        raise ValueError(f"Project {keeper}/{name} has no start command")

    # Check if already running
    existing = _read_state(keeper, name)
    if existing and _is_pid_alive(existing.get("pid", 0)):
        return existing

    # Check concurrency limit
    current = running_projects()
    if len(current) >= MAX_RUNNING:
        names = [f"{r['keeper']}/{r['name']}" for r in current]
        raise RuntimeError(f"Max {MAX_RUNNING} running projects. Stop one first: {', '.join(names)}")

    port = _allocate_port()
    work_dir = project_dir(keeper, name)

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
        "keeper": keeper,
        "name": name,
        "port": port,
        "pid": proc.pid,
        "start_command": project.start,
    }
    _write_state(keeper, name, state)
    return state


def stop_project(keeper: str, name: str) -> bool:
    """Stop a running project. Returns True if it was running."""
    state = _read_state(keeper, name)
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
    _clear_state(keeper, name)
    return True
