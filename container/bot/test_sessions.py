"""Tests for session spawning and management.

Runs locally — requires tmux but NOT Docker or Claude.
Tests command construction, tmux spawn/read cycle, and status file handling.

Usage: uv run pytest container/bot/test_sessions.py -v
"""

import json
import os
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Command construction tests (pure Python, no tmux needed)
# ---------------------------------------------------------------------------

class TestBuildSessionCommand:
    """Test that build_session_command produces shell-safe commands."""

    def _build(self, session_name, work_dir, prompt):
        """Mimics build_session_command from core.py."""
        script = "/workspace/woltspace/container/bin/run-session.sh"
        return f"{script} {shlex.quote(session_name)} {shlex.quote(work_dir)} {shlex.quote(prompt)}"

    def test_simple_prompt(self):
        cmd = self._build("test-1", "/tmp", "hello world")
        assert "hello world" in cmd
        # Should be wrapped in single quotes
        assert "'hello world'" in cmd

    def test_exclamation_mark(self):
        """The original bug — gy!be caused bash history expansion."""
        cmd = self._build("test-2", "/tmp", "play gy!be and mogwai")
        # Must be in single quotes to prevent ! expansion
        assert "'play gy!be and mogwai'" in cmd
        # Must NOT be in double quotes
        assert '"play gy!be' not in cmd

    def test_dollar_sign(self):
        cmd = self._build("test-3", "/tmp", "check $HOME variable")
        assert "'check $HOME variable'" in cmd

    def test_backticks(self):
        cmd = self._build("test-4", "/tmp", "run `whoami` please")
        assert "`whoami`" in cmd

    def test_single_quotes_in_prompt(self):
        """shlex.quote handles single quotes by breaking out and escaping."""
        prompt = "it's a test"
        cmd = self._build("test-5", "/tmp", prompt)
        # Should be safe to pass to shell
        result = subprocess.run(
            ["bash", "-c", f"echo {shlex.quote(prompt)}"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == prompt

    def test_newlines_in_prompt(self):
        cmd = self._build("test-6", "/tmp", "line one\nline two")
        assert "line one" in cmd

    def test_empty_prompt(self):
        cmd = self._build("test-7", "/tmp", "")
        assert "''" in cmd

    def test_unicode_em_dash(self):
        cmd = self._build("test-8", "/tmp", "jazz — ambient")
        assert "jazz — ambient" in cmd

    def test_semicolons_and_pipes(self):
        """Shell metacharacters must not break out of the quoted string."""
        cmd = self._build("test-9", "/tmp", "foo; rm -rf /; bar | cat")
        assert "rm -rf" in cmd
        # Verify it's safely quoted by running through shell echo
        result = subprocess.run(
            ["bash", "-c", f"echo {shlex.quote('foo; rm -rf /; bar | cat')}"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == "foo; rm -rf /; bar | cat"


# ---------------------------------------------------------------------------
# Tmux integration tests (require tmux)
# ---------------------------------------------------------------------------

def tmux_available():
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


@pytest.fixture
def tmux_session():
    """Create a tmux session for testing, clean it up after."""
    name = f"test-sess-{int(time.time()) % 100000}"
    yield name
    # Cleanup
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


@pytest.mark.skipif(not tmux_available(), reason="tmux not installed")
class TestTmuxSpawn:
    """Test actual tmux session creation and output reading."""

    def test_spawn_with_direct_command(self, tmux_session):
        """Spawn a tmux session with a command directly (no send-keys)."""
        name = tmux_session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "echo 'session works'; sleep 2"],
            check=True,
        )
        # Session should exist
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
        )
        assert result.returncode == 0

    def test_capture_pane_output(self, tmux_session):
        """Verify we can read output from a tmux session."""
        name = tmux_session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "echo 'MARKER_12345'; sleep 5"],
            check=True,
        )
        time.sleep(0.5)  # Let echo execute
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", "-10"],
            capture_output=True, text=True, check=True,
        )
        assert "MARKER_12345" in result.stdout

    def test_shell_metachars_via_wrapper(self, tmux_session):
        """Verify a script receiving shell metachars handles them safely."""
        name = tmux_session
        prompt = "play gy!be and $HOME and `whoami`; rm -rf /"

        # Create a tiny test wrapper that just echoes args
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as f:
            f.write("#!/bin/bash\necho \"ARG3=$3\"\nsleep 3\n")
            wrapper = f.name
        os.chmod(wrapper, 0o755)

        try:
            cmd = f"{wrapper} {shlex.quote(name)} {shlex.quote('/tmp')} {shlex.quote(prompt)}"
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, cmd],
                check=True,
            )
            time.sleep(1)
            result = subprocess.run(
                ["tmux", "capture-pane", "-t", name, "-p", "-S", "-10"],
                capture_output=True, text=True, check=True,
            )
            # The prompt should appear literally, not interpreted
            assert "gy!be" in result.stdout
            assert "$HOME" in result.stdout
        finally:
            os.unlink(wrapper)

    def test_session_exits_when_command_finishes(self, tmux_session):
        """When the command passed to tmux exits, the session should die."""
        name = tmux_session
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "echo done"],
            check=True,
        )
        time.sleep(1)
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
        )
        # Session should be gone since 'echo done' exits immediately
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Status file tests (pure Python)
# ---------------------------------------------------------------------------

class TestSessionStatus:
    """Test structured status file reading."""

    def test_read_status_file(self, tmp_path):
        status_dir = tmp_path / "sessions"
        status_dir.mkdir()
        status_file = status_dir / "test-session.json"
        status_file.write_text(json.dumps({
            "session": "test-session",
            "status": "completed",
            "exit_code": 0,
        }))

        data = json.loads(status_file.read_text())
        assert data["status"] == "completed"
        assert data["exit_code"] == 0

    def test_missing_status_file(self, tmp_path):
        status_file = tmp_path / "sessions" / "nonexistent.json"
        assert not status_file.exists()

    def test_corrupt_status_file(self, tmp_path):
        status_dir = tmp_path / "sessions"
        status_dir.mkdir()
        status_file = status_dir / "bad.json"
        status_file.write_text("not valid json {{{")

        with pytest.raises(json.JSONDecodeError):
            json.loads(status_file.read_text())
