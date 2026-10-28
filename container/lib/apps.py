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
import time
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel, Field

import lodge_db
from env_compat import get_env
from paths import space_apps_dir
from tunnel import stop_cloudflared

WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
APPS_DIR = WOLTS_DIR / "apps"
LEGACY_PROJECTS_DIR = WOLTS_DIR / "projects"  # deprecated — still discovered for backwards compat

VALID_STACKS = {"python", "vite", "node", "html"}

MANIFEST = "woltspace.json"

# Running app state is the apps_running table; app logs stay in .space/apps/
_RUNNING_STATE_DIR = space_apps_dir(WOLTS_DIR)

# Credentials of the lodge's own services: the chat adapters, the GitHub bot,
# the Cloudflare tunnel and API. They are loaded into the lodge's environment
# for those services, and an app would otherwise inherit them all. An app that
# really needs one keeps its own copy in its own .env.
LODGE_ONLY_ENV_PREFIXES = ("TELEGRAM_", "SLACK_", "GITHUB_APP_", "CLOUDFLARE_")
# The owner's own Claude Code login, for wolt sessions only, and a lodge API
# key handed to a script.
LODGE_ONLY_ENV_KEYS = frozenset({"CLAUDE_CODE_OAUTH_TOKEN", "WOLTSPACE_LODGE_KEY"})


def app_environment(**extra: str) -> dict[str, str]:
    """The environment an app process starts with: the lodge's, minus its
    service credentials, plus `extra`."""
    env = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith(LODGE_ONLY_ENV_PREFIXES)
        and key.upper() not in LODGE_ONLY_ENV_KEYS
    }
    env.update(extra)
    return env


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


def app_log_file(name: str) -> Path:
    """Return the package-owned log path for an app process."""
    return _RUNNING_STATE_DIR / f"{name}.log"


def _all_states() -> list[dict]:
    with closing(lodge_db.connect(WOLTS_DIR)) as conn:
        rows = conn.execute("SELECT name, record FROM apps_running ORDER BY name").fetchall()
    # The row's name wins: an old record without one must not stop a loop.
    return [{**json.loads(row["record"]), "name": row["name"]} for row in rows]


def _read_state(name: str) -> dict | None:
    with closing(lodge_db.connect(WOLTS_DIR)) as conn:
        row = conn.execute("SELECT record FROM apps_running WHERE name=?", (name,)).fetchone()
    return json.loads(row["record"]) if row else None


_INSERT_STATE = ("INSERT{} INTO apps_running (name, pid, port, tunnel_pid, tunnel_url, keeper,"
                 " started_at, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?)")


def _state_row(name: str, state: dict) -> tuple:
    return (name, state.get("pid"), state.get("port"), state.get("tunnel_pid"),
            state.get("tunnel_url"), state.get("keeper"), int(time.time()), json.dumps(state))


def _write_state(name: str, state: dict) -> None:
    with lodge_db.write(WOLTS_DIR) as conn:
        conn.execute(
            _INSERT_STATE.format("") +
            " ON CONFLICT(name) DO UPDATE SET pid=excluded.pid, port=excluded.port,"
            " tunnel_pid=excluded.tunnel_pid, tunnel_url=excluded.tunnel_url,"
            " keeper=excluded.keeper, record=excluded.record",
            _state_row(name, state),
        )


def import_apps(conn, wolts_dir) -> tuple[dict, list[Path]]:
    """Import .space/apps/<name>.json from older releases; a row already here wins.

    lodge_db.run_imports retires the files it returns; the .log files stay.
    """
    counts = {"imported": 0, "skipped": 0}
    files = sorted(space_apps_dir(Path(wolts_dir)).glob("*.json"))
    for path in files:
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"[apps] import: skipping unreadable {path}: {exc}")
            counts["skipped"] += 1
            continue
        cursor = conn.execute(_INSERT_STATE.format(" OR IGNORE"), _state_row(path.stem, state))
        counts["imported" if cursor.rowcount else "skipped"] += 1
    return counts, files


def _clear_state(name: str) -> None:
    with lodge_db.write(WOLTS_DIR) as conn:
        conn.execute("DELETE FROM apps_running WHERE name=?", (name,))


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_apps() -> list[dict]:
    """List all currently running apps with their state.

    State row semantics (intent model):
    - row present + PID alive = running
    - row present + PID dead  = wanted but down (apps_restore will respawn at next boot)
    - no row                  = explicitly off

    We do NOT delete stale rows here. Deletion only happens on explicit stop_app()
    or when the manifest is gone (cleaned up by apps_restore).
    """
    running = []
    for state in _all_states():
        pid = state.get("pid")
        if pid and _is_pid_alive(pid):
            state["alive"] = True
            running.append(state)
    return running


def intended_apps() -> list[dict]:
    """List all apps the user has expressed intent to run (state row present).

    Returns state dicts with an added `alive` bool indicating actual running status.
    Used by apps_restore() and by callers that want to show stale-intent apps.
    """
    intended = []
    for state in _all_states():
        pid = state.get("pid")
        state["alive"] = bool(pid and _is_pid_alive(pid))
        intended.append(state)
    return intended


def start_app(name: str) -> dict:
    """Start an app's dev server. Returns state dict with port and pid.

    The app gets PORT, its manifest's port, and HOST=127.0.0.1: the Mill
    reaches it on loopback, and nothing else needs to.
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
            subprocess.run(
                app.install, shell=True, cwd=str(work_dir), check=True,
                env=app_environment(),
            )

    # Start the process with PORT and HOST: commands that pass `--host $HOST`
    # stay on loopback.
    env = app_environment(PORT=str(port), HOST="127.0.0.1")
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

    For each apps_running row:
    - Manifest missing  → orphan, delete the row
    - PID alive         → leave it (survived the restart)
    - PID dead          → respawn via start_app()

    Returns a summary list of actions for logging.
    """
    actions = []
    for state in _all_states():
        name = state["name"]

        # Manifest gone — app was removed while container was down
        if get_app(name) is None:
            _clear_state(name)
            actions.append({"name": name, "action": "orphan-cleaned"})
            print(f"[apps] orphan {name} cleaned (no manifest)")
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
