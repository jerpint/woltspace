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
    delivered — which pane, which command, which text, which capture window.

    panes: the pane inventory this fake reports for any session, as
    (pane_id, pane_pid, active) tuples. agents: the pane_ids whose process
    tree should count as carrying an agent.
    """

    def __init__(self, *, alive: bool = True, capture_text: str = "",
                 next_pane: str = "%1", panes=None, agents=()):
        self._alive = alive
        self._capture_text = capture_text
        self._next_pane = next_pane
        self._panes = list(panes) if panes is not None else []
        self._agents = set(agents)
        self.spawns: list[tuple[str, str, str]] = []   # (session_id, cwd, command)
        self.pastes: list[tuple[str, str, float]] = []  # (target pane, text, settle)
        self.captures: list[tuple[str, str | None]] = []  # (target pane, start)
        self.stops: list[str] = []                      # tmux session names
        self.alive_checks: list[str] = []

    # -- helpers -----------------------------------------------------------
    def _pane_objects(self, session_name):
        from session_runtime import TmuxPane
        return [TmuxPane(session_name, pane_id, pid, active)
                for pane_id, pid, active in self._panes]

    @property
    def last_spawn(self) -> tuple[str, str, str]:
        assert self.spawns, "no session was spawned"
        return self.spawns[-1]

    @property
    def last_paste(self) -> tuple[str, str, float]:
        assert self.pastes, "nothing was pasted"
        return self.pastes[-1]

    @property
    def last_capture(self) -> tuple[str, str | None]:
        assert self.captures, "nothing was captured"
        return self.captures[-1]

    def feed_capture(self, text) -> None:
        """Set what capture() returns — a string, or a list of successive frames."""
        self._capture_text = list(text) if isinstance(text, list) else text

    # -- SessionRuntime protocol -------------------------------------------
    def spawn(self, session_id: str, cwd: str, command: str):
        from session_runtime import RuntimeHandle
        self.spawns.append((session_id, cwd, command))
        return RuntimeHandle(session_id, session_id, self._next_pane)

    def panes_for_session(self, session_name: str):
        return self._pane_objects(session_name) if self._alive else []

    def is_alive(self, handle) -> bool:
        self.alive_checks.append(handle.tmux_session_name)
        return self._alive

    def pane_is_live(self, handle) -> bool:
        return bool(handle.pane_id) and any(
            p.pane_id == handle.pane_id for p in self.panes_for_session(handle.tmux_session_name)
        )

    def resolve_delivery_pane(self, handle, process_names=None):
        if handle.pane_id:
            return handle
        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            return handle.at_pane(handle.tmux_session_name)
        if len(panes) == 1:
            return handle.at_pane(panes[0].pane_id)
        if process_names:
            for pane in panes:
                if pane.pane_id in self._agents:
                    return handle.at_pane(pane.pane_id)
        return handle.at_pane(handle.tmux_session_name)

    def paste(self, handle, text: str, settle: float = 0.0, *, process_names=None) -> None:
        target = self.resolve_delivery_pane(handle, process_names)
        self.pastes.append((target.pane_id, text, settle))

    def capture(self, handle, start: str | None = "-30") -> str:
        target = self.resolve_delivery_pane(handle)
        self.captures.append((target.pane_id, start))
        if isinstance(self._capture_text, list):
            # A scripted sequence of successive pane contents; the last frame
            # repeats once the script runs out.
            if len(self._capture_text) > 1:
                return self._capture_text.pop(0)
            return self._capture_text[0] if self._capture_text else ""
        return self._capture_text

    def stop(self, handle) -> bool:
        self.stops.append(handle.tmux_session_name)
        return True

    def list_session_names(self, include_main: bool = False) -> set[str]:
        return set()

    def _matching_panes_for_handle(self, handle, process_names):
        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            return None
        if handle.pane_id:
            scoped = [p for p in panes if p.pane_id == handle.pane_id]
            if scoped:
                panes = scoped
        return [p for p in panes if p.pane_id in self._agents]

    def resolve_process_handle(self, handle, process_names):
        matched = self._matching_panes_for_handle(handle, process_names)
        if not matched:
            return None
        return handle.at_pane(matched[0].pane_id)

    def has_descendant_process(self, handle, process_names):
        matched = self._matching_panes_for_handle(handle, process_names)
        return None if matched is None else bool(matched)


@pytest.fixture
def fake_runtime(monkeypatch):
    """Install a recording FakeSessionRuntime at the one shared seam.

    Patches session_runtime.set_runtime, so EVERY call site — sessions,
    harnesses, vulture, server — gets the fake. Patching only one module's
    accessor would leave a faked paste sitting next to a real tmux process
    walk in the same call.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    import session_runtime

    runtime = FakeSessionRuntime(panes=[("%1", "100", True)], agents={"%1"})
    assert isinstance(runtime, session_runtime.SessionRuntime)
    session_runtime.set_runtime(runtime)
    yield runtime
    session_runtime.set_runtime(None)
