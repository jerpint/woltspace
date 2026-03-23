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
    for path in [
        Path("/workspace/wolts/.space/platform/tunnel-url"),
        Path("/workspace/wolts/.state/tunnel-url"),  # backwards compat
    ]:
        if path.exists():
            return path.read_text().strip().rstrip("/")
    return None
