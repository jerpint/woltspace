"""Shared fixtures for woltspace integration tests."""

import json
import os
import shutil
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
    """The dedicated test group chat. Never discovered — only configured.

    Anything that would send a real message must be told exactly where to send
    it. Finding a chat id lying around in the registry means messaging whoever
    happens to be using this woltspace.
    """
    return os.environ.get("TEST_CHAT_ID") or None


def _live_send_enabled() -> bool:
    """Sending to Telegram reaches a real person's phone. Opt in explicitly."""
    return os.environ.get("WOLTSPACE_TEST_LIVE_SEND", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _tmux_available() -> bool:
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


# ---------------------------------------------------------------------------
# Markers / skips
# ---------------------------------------------------------------------------

def _real_spawn_enabled() -> bool:
    """Real spawns boot an agent process (~300MB) that outlives the test run."""
    return os.environ.get("WOLTSPACE_TEST_REAL_SPAWN", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


SHADOW_PREFIX = "test-shadow"
SHADOW_WOLT = f"{SHADOW_PREFIX}-{os.getpid()}"
SHADOW_MARKER = ".created-by-tests"


def shadow_is_reusable(home: Path) -> bool:
    """Only ever delete a directory *this run* created.

    The marker records the pid that wrote it, so two concurrent suite runs
    cannot delete each other's shadow wolt mid-test, and a directory belonging
    to a live run is left alone.
    """
    if not home.exists():
        return True
    marker = home / SHADOW_MARKER
    if not marker.is_file():
        return False
    try:
        owner = int(marker.read_text().split("pid=", 1)[1].split()[0])
    except (OSError, IndexError, ValueError):
        return False
    if owner == os.getpid():
        return True
    try:
        os.kill(owner, 0)
    except ProcessLookupError:
        return True  # its run is over; the leftovers are ours to clear
    except PermissionError:
        return False
    return False


requires_server = pytest.mark.skipif(not _server_up(), reason="server not running on localhost:7777")
# getUpdates is not the read-only call it looks like: it is exclusive, so a
# second caller either gets 409 or wins the race and takes updates the real bot
# then never sees. Touching the live bot at all is opt-in.
requires_live_telegram = pytest.mark.skipif(
    not _live_send_enabled(),
    reason="talks to the live bot; set WOLTSPACE_TEST_LIVE_SEND=1",
)
requires_live_send = pytest.mark.skipif(
    not (_live_send_enabled() and _test_chat_id()),
    reason="sends a real message; set WOLTSPACE_TEST_LIVE_SEND=1 and TEST_CHAT_ID",
)
requires_real_spawn = pytest.mark.skipif(
    not _real_spawn_enabled(),
    reason="boots a real agent process; set WOLTSPACE_TEST_REAL_SPAWN=1 to opt in",
)

requires_telegram = pytest.mark.skipif(not _telegram_bot_configured(), reason="TELEGRAM_BOT_TOKEN not set")
requires_tmux = pytest.mark.skipif(not _tmux_available(), reason="tmux not installed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def claude_trust_home(tmp_path_factory, monkeypatch):
    """Point Claude's trust file at a throwaway home for every test.

    Preparing a claude session auto-trusts its workdir in ~/.claude.json.
    Tests spawn sessions in tmp dirs by the dozen; none of that may land in
    the developer's real Claude state. Yields the fake home so trust tests
    can read the file back.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    import trust

    home = tmp_path_factory.mktemp("claude-home")
    monkeypatch.setattr(trust, "claude_config_path", lambda: home / ".claude.json")
    return home


@pytest.fixture(autouse=True)
def codex_trust_home(tmp_path_factory, monkeypatch):
    """Point codex's config at a throwaway CODEX_HOME for every test.

    Same reason as `claude_trust_home`: preparing a codex session appends a
    trust block to $CODEX_HOME/config.toml, and no test may append to the
    developer's real ~/.codex. Yields the fake CODEX_HOME.
    """
    home = tmp_path_factory.mktemp("codex-home")
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


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
    """The dedicated test group chat ID. Skip unless explicitly opted in."""
    chat_id = _test_chat_id()
    if not chat_id:
        pytest.skip("TEST_CHAT_ID not set — configure in .env for live tests")
    if not _live_send_enabled():
        pytest.skip("live sends are opt-in — set WOLTSPACE_TEST_LIVE_SEND=1")
    return chat_id


@pytest.fixture
def shadow_wolt():
    """A throwaway wolt that exists only for the duration of one test.

    Live-server tests need a real wolt on disk, and borrowing someone's actual
    wolt means spawning agents into their directory and leaving debris behind.
    This is the only way a test gets a wolt name, so there is no real name to
    hardcode. Teardown stops every session it spawned, then removes it — and
    refuses to remove anything it did not create.
    """
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    home = wolts_dir / SHADOW_WOLT
    marker = home / SHADOW_MARKER
    if not shadow_is_reusable(home):
        pytest.skip(f"{home} exists but was not created by the tests; refusing to touch it")

    # Marker first: if anything below fails, the directory is still
    # recognisably ours rather than a permanent skip for every later run.
    home.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        f"Created by test/conftest.py::shadow_wolt pid={os.getpid()}. Safe to delete.\n"
    )
    (home / "wolt" / "site").mkdir(parents=True, exist_ok=True)
    (home / "wolt" / "memory").mkdir(parents=True, exist_ok=True)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": SHADOW_WOLT,
        "type": "rodent",
        "emoji": "🫥",
        "description": "throwaway wolt owned by the test suite",
        "test_fixture": True,
    }, indent=2) + "\n")

    try:
        yield SHADOW_WOLT
    finally:
        _stop_shadow_sessions(wolts_dir)
        if marker.exists():
            shutil.rmtree(home, ignore_errors=True)


def _server_endpoint() -> str:
    """Where this suite's server lives — not always :7777."""
    host = os.environ.get("WOLTSPACE_HOST", "localhost")
    port = os.environ.get("WOLTSPACE_PORT") or os.environ.get("PORT") or "7777"
    return f"http://{host}:{port}"


def _stop_shadow_sessions(wolts_dir: Path):
    """Stop every session the shadow wolt spawned — through the server if it is up."""
    import urllib.request

    sessions_dir = wolts_dir / SHADOW_WOLT / ".state" / "sessions"
    names = []
    if sessions_dir.is_dir():
        names = [path.stem for path in sessions_dir.glob("*.json")]
    for name in names:
        body = json.dumps({}).encode()
        request = urllib.request.Request(
            f"{_server_endpoint()}/sessions/{name}/stop",
            data=body, headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=15).read()
        except Exception:
            # Server down or already gone — fall back to killing tmux directly so
            # a failed run still cannot leak an agent process.
            subprocess.run(["tmux", "kill-session", "-t", name],
                           capture_output=True, check=False)


@pytest.fixture
def routed_test_session(test_chat_id, shadow_wolt):
    """Create a temporary session registered with TEST_CHAT_ID routing.

    Ensures notify probes go to the test group, never the main chat.
    """
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    from sessions import SessionRegistry

    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    wolt = shadow_wolt
    reg = SessionRegistry(wolts_dir)
    name = f"test-probe-{int(time.time()) % 100000}-{os.getpid()}"
    reg.create(
        name=name,
        wolt=wolt,
        creature="beaver",
        model="sonnet",
        dir=str(wolts_dir / wolt),
        prompt="test probe session",
        adapter="telegram",
        chat_id=test_chat_id,
    )
    yield name
    reg.delete(name, wolt=wolt)


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
        self.in_session_spawns: list[tuple[str, str, str]] = []
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

    def spawn_in_session(self, handle, cwd: str, command: str):
        self.in_session_spawns.append((handle.tmux_session_name, cwd, command))
        return handle.at_pane(self._next_pane)

    def panes_for_session(self, session_name: str):
        return self._pane_objects(session_name) if self._alive else []

    def is_alive(self, handle) -> bool:
        self.alive_checks.append(handle.tmux_session_name)
        return self._alive

    def handle_is_alive(self, handle) -> bool:
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
