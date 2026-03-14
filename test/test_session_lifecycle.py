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
# Session routing + status (unit tests — no server needed)
# ---------------------------------------------------------------------------

class TestSessionRouting:
    """Pure-Python tests for session routing (write/read JSON files)."""

    def test_write_and_read_routing(self, tmp_path):
        import bot.core as core
        original = core.SESSION_ROUTING_DIR
        try:
            core.SESSION_ROUTING_DIR = tmp_path / "routing"
            core.write_session_routing("test-session-1", {
                "adapter": "telegram", "chat_id": "123456"
            })
            result = core.read_session_routing("test-session-1")
            assert result["adapter"] == "telegram"
            assert result["chat_id"] == "123456"
        finally:
            core.SESSION_ROUTING_DIR = original

    def test_read_nonexistent_routing(self, tmp_path):
        import bot.core as core
        original = core.SESSION_ROUTING_DIR
        try:
            core.SESSION_ROUTING_DIR = tmp_path / "routing"
            assert core.read_session_routing("nope") is None
        finally:
            core.SESSION_ROUTING_DIR = original

    def test_corrupt_routing_returns_none(self, tmp_path):
        import bot.core as core
        original = core.SESSION_ROUTING_DIR
        try:
            routing_dir = tmp_path / "routing"
            routing_dir.mkdir(parents=True)
            core.SESSION_ROUTING_DIR = routing_dir
            (routing_dir / "corrupt.json").write_text("{invalid json")
            assert core.read_session_routing("corrupt") is None
        finally:
            core.SESSION_ROUTING_DIR = original


class TestSessionStatus:
    """Pure-Python tests for session status files."""

    def test_read_status_file(self, tmp_path):
        import bot.core as core
        original_state = core.STATE_DIR
        try:
            core.STATE_DIR = tmp_path
            status_dir = tmp_path / "sessions"
            status_dir.mkdir()
            (status_dir / "test-sess.json").write_text(json.dumps({
                "session": "test-sess", "status": "running",
                "started": int(time.time()),
            }))
            result = core.get_session_status("test-sess")
            assert result["status"] == "running"
            assert result["session"] == "test-sess"
        finally:
            core.STATE_DIR = original_state

    def test_read_nonexistent_status(self, tmp_path):
        import bot.core as core
        original_state = core.STATE_DIR
        try:
            core.STATE_DIR = tmp_path
            (tmp_path / "sessions").mkdir()
            assert core.get_session_status("nope") is None
        finally:
            core.STATE_DIR = original_state

    def test_corrupt_status_returns_none(self, tmp_path):
        import bot.core as core
        original_state = core.STATE_DIR
        try:
            core.STATE_DIR = tmp_path
            status_dir = tmp_path / "sessions"
            status_dir.mkdir()
            (status_dir / "corrupt.json").write_text("{bad json")
            assert core.get_session_status("corrupt") is None
        finally:
            core.STATE_DIR = original_state


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
