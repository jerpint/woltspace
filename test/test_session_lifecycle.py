"""Session lifecycle tests — create, check, kill sessions via the server.

Tests the full session flow that a Telegram user would trigger:
1. Create a session (via tools API or direct tmux)
2. Verify it shows up in session list
3. Send a message to it
4. Kill it
5. Verify it's gone

Usage: uv run pytest test/test_session_lifecycle.py -v
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import requires_server, requires_tmux


# ---------------------------------------------------------------------------
# Session registry (unit tests — no server needed)
# ---------------------------------------------------------------------------

class TestSessionRegistry:
    """Pure-Python tests for the SessionRegistry class."""

    def test_create_and_get(self, tmp_registry):
        reg = tmp_registry
        data = reg.create("test-session-1", wolt="neowolt", creature="beaver")
        assert data["name"] == "test-session-1"
        assert data["wolt"] == "neowolt"
        assert data["creature"] == "beaver"
        assert data["status"] == "running"

        fetched = reg.get("test-session-1", check_alive=False)
        assert fetched["name"] == "test-session-1"

    def test_update_fields(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-2", wolt="neowolt")
        updated = reg.update("test-session-2", viewport_url="http://example.com")
        assert updated["viewport_url"] == "http://example.com"

    def test_finish_success(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-3")
        finished = reg.finish("test-session-3", exit_code=0)
        assert finished["status"] == "completed"
        assert finished["exit_code"] == 0
        assert finished["finished_at"] is not None

    def test_finish_failure(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-4")
        finished = reg.finish("test-session-4", exit_code=1)
        assert finished["status"] == "failed"
        assert finished["exit_code"] == 1

    def test_list_sessions(self, tmp_registry):
        reg = tmp_registry
        reg.create("sess-a", wolt="neowolt")
        reg.create("sess-b", wolt="blabo")
        reg.create("sess-c", wolt="neowolt")

        all_sessions = reg.list()
        assert len(all_sessions) == 3

        neowolt_only = reg.list(wolt="neowolt")
        assert len(neowolt_only) == 2
        assert all(s["wolt"] == "neowolt" for s in neowolt_only)

    def test_delete_session(self, tmp_registry):
        reg = tmp_registry
        reg.create("to-delete")
        assert reg.delete("to-delete") is True
        assert reg.get("to-delete", check_alive=False) is None
        assert reg.delete("to-delete") is False

    def test_touch_updates_activity(self, tmp_registry):
        reg = tmp_registry
        reg.create("touch-me")
        before = reg.get("touch-me", check_alive=False)["last_activity"]
        time.sleep(1.1)
        reg.touch("touch-me")
        after = reg.get("touch-me", check_alive=False)["last_activity"]
        assert after > before

    def test_get_nonexistent(self, tmp_registry):
        assert tmp_registry.get("nope", check_alive=False) is None

    def test_update_nonexistent(self, tmp_registry):
        assert tmp_registry.update("nope", status="done") is None

    def test_prompt_truncated(self, tmp_registry):
        reg = tmp_registry
        long_prompt = "x" * 1000
        data = reg.create("truncate-test", prompt=long_prompt)
        assert len(data["prompt"]) == 500

    def test_atomic_write(self, tmp_registry):
        """Write uses tmp + rename for atomicity — no partial reads."""
        reg = tmp_registry
        reg.create("atomic-test", wolt="neowolt")
        # The .tmp file should not linger
        tmp_files = list(reg.dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_corrupt_json_handled(self, tmp_registry):
        """Corrupt JSON in registry should not crash."""
        reg = tmp_registry
        bad_file = reg.dir / "corrupt.json"
        bad_file.write_text("{invalid json")
        # get should return None
        assert reg.get("corrupt", check_alive=False) is None
        # list should skip it
        sessions = reg.list()
        assert all(s["name"] != "corrupt" for s in sessions)


# ---------------------------------------------------------------------------
# Tmux session management (requires tmux)
# ---------------------------------------------------------------------------

@requires_tmux
class TestTmuxSessionManagement:
    """Test creating and destroying tmux sessions."""

    def test_create_session(self, tmux_session):
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        result = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
        assert result.returncode == 0

    def test_kill_session(self, tmux_session):
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 30"], check=True)
        subprocess.run(["tmux", "kill-session", "-t", name], check=True)
        result = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
        assert result.returncode != 0

    def test_send_keys(self, tmux_session):
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
        time.sleep(0.3)
        marker = f"MARKER_{int(time.time())}"
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", marker], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert marker in result.stdout

    def test_session_env_persists(self, tmux_session):
        """WOLT_SESSION env var should persist within the session."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "-l",
             f"export WOLT_SESSION={name} && echo WOLT_SESSION=$WOLT_SESSION"],
            check=True,
        )
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert f"WOLT_SESSION={name}" in result.stdout


# ---------------------------------------------------------------------------
# Full lifecycle (requires server + tmux)
# ---------------------------------------------------------------------------

@requires_server
@requires_tmux
class TestFullSessionLifecycle:
    """End-to-end: create session via tmux, verify via server, kill it."""

    def test_session_appears_in_list(self, tmux_session, server_get):
        """A tmux session should appear in the session list."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.5)

        # Check via tmux directly
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, check=True,
        )
        assert name in result.stdout

    def test_notify_to_live_session(self, routed_test_session, server_post):
        """Messages sent via /notify to a routed session should go to the test group."""
        result = server_post("/notify", {"session": routed_test_session, "message": "probe"})
        assert isinstance(result, dict)
        assert result.get("adapter") == "telegram"
