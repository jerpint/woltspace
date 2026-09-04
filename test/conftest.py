"""Shared fixtures for woltspace integration tests."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def _server_up() -> bool:
    """Check if the woltspace server is running on localhost:7777."""
    import urllib.request
    try:
        urllib.request.urlopen("http://localhost:7777/", timeout=2)
        return True
    except Exception:
        return False


def _telegram_bot_configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN"))


def _test_chat_id() -> str | None:
    """Get the dedicated test chat ID (group where test notifies go).
    Set TEST_CHAT_ID in .env or fall back to None."""
    return os.environ.get("TEST_CHAT_ID")


def _tmux_available() -> bool:
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# Markers / skips
# ---------------------------------------------------------------------------

requires_server = pytest.mark.skipif(not _server_up(), reason="server not running on localhost:7777")
requires_telegram = pytest.mark.skipif(not _telegram_bot_configured(), reason="TELEGRAM_BOT_TOKEN not set")
requires_tmux = pytest.mark.skipif(not _tmux_available(), reason="tmux not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def server_post():
    """Helper to POST JSON to localhost:7777."""
    import urllib.request

    def _post(path: str, body: dict) -> dict:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"http://localhost:7777{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except Exception as e:
            return {"error": str(e)}

    return _post


@pytest.fixture
def server_get():
    """Helper to GET from localhost:7777."""
    import urllib.request

    def _get(path: str) -> dict | str:
        req = urllib.request.Request(f"http://localhost:7777{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                text = resp.read().decode()
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        except Exception as e:
            return {"error": str(e)}

    return _get


@pytest.fixture
def tmux_session():
    """Create a temporary tmux session, clean up after test."""
    name = f"test-{int(time.time()) % 100000}-{os.getpid()}"
    yield name
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


@pytest.fixture
def tmp_registry(tmp_path):
    """Create a temporary session registry with a temp wolts_dir."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    from sessions import SessionRegistry
    # Per-wolt model: registry uses WOLTS_DIR as root, sessions land in
    # wolts_dir/{wolt}/.state/sessions/{name}.json
    return SessionRegistry(tmp_path)


@pytest.fixture
def test_chat_id():
    """The dedicated test group chat ID. Skip if not configured."""
    chat_id = _test_chat_id()
    if not chat_id:
        pytest.skip("TEST_CHAT_ID not set — configure in .env for live tests")
    return chat_id


@pytest.fixture
def routed_test_session(test_chat_id):
    """Create a temporary session registered with TEST_CHAT_ID routing.

    Ensures notify probes go to the test group, never the main chat.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    from sessions import SessionRegistry

    reg = SessionRegistry(Path("/workspace/wolts"))
    name = f"test-probe-{int(time.time()) % 100000}-{os.getpid()}"
    reg.create(
        name=name,
        wolt="neowolt",
        creature="beaver",
        model="sonnet",
        dir="/workspace/wolts/neowolt",
        prompt="test probe session",
        adapter="telegram",
        chat_id=test_chat_id,
    )
    yield name
    reg.delete(name, wolt="neowolt")


@pytest.fixture
def tunnel_url():
    """Read the current tunnel URL."""
    import json
    try:
        state = json.loads(Path("/workspace/wolts/.space/platform/tunnel.json").read_text())
        url = state.get("url", "").strip().rstrip("/")
        return url if url else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Session runtime test double
# ---------------------------------------------------------------------------

class FakeSessionRuntime:
    """Recording stand-in for TmuxSessionRuntime.

    Sessions go through the session_runtime boundary rather than shelling out
    to tmux directly, so tests stub that boundary instead of `subprocess`.
    Every call is recorded so a test can still assert exactly what was
    delivered — which pane, which command, which text.
    """

    def __init__(self, *, alive: bool = True, capture_text: str = "", next_pane: str = "%1"):
        self._alive = alive
        self._capture_text = capture_text
        self._next_pane = next_pane
        self.spawns: list[tuple[str, str, str]] = []   # (session_id, cwd, command)
        self.pastes: list[tuple[str, str, float]] = []  # (target pane/session, text, settle)
        self.captures: list[tuple[str, str]] = []       # (target, start)
        self.stops: list[str] = []                      # tmux session names
        self.alive_checks: list[str] = []

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _target(handle) -> str:
        """The exact address a real tmux call would receive for this handle."""
        return handle.pane_id or handle.tmux_session_name

    @property
    def last_spawn(self) -> tuple[str, str, str]:
        assert self.spawns, "no session was spawned"
        return self.spawns[-1]

    @property
    def last_paste(self) -> tuple[str, str, float]:
        assert self.pastes, "nothing was pasted"
        return self.pastes[-1]

    # -- SessionRuntime protocol -------------------------------------------
    def spawn(self, session_id: str, cwd: str, command: str):
        from session_runtime import RuntimeHandle
        self.spawns.append((session_id, cwd, command))
        return RuntimeHandle(session_id, session_id, self._next_pane)

    def is_alive(self, handle) -> bool:
        self.alive_checks.append(self._target(handle))
        return self._alive

    def paste(self, handle, text: str, settle: float = 0.0) -> None:
        self.pastes.append((self._target(handle), text, settle))

    def capture(self, handle, start: str = "-30") -> str:
        self.captures.append((self._target(handle), start))
        return self._capture_text

    def stop(self, handle) -> bool:
        self.stops.append(handle.tmux_session_name)
        return True

    def list_session_names(self, include_main: bool = False) -> set[str]:
        return set()


@pytest.fixture
def fake_runtime(monkeypatch):
    """Patch sessions._runtime to a recording FakeSessionRuntime and return it."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    import sessions

    runtime = FakeSessionRuntime()
    monkeypatch.setattr(sessions, "_runtime", lambda: runtime)
    return runtime
