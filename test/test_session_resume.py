"""Session resume tests — unit tests for resume_session().

Tests the three resume paths:
  1. Claude running in tmux → send keys directly
  2. Tmux alive, claude exited → restart wclaude --resume in pane
  3. Tmux dead → create new tmux with wclaude --resume

Usage: uv run --project server --with pytest pytest test/test_session_resume.py -v
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


@pytest.fixture
def wolt_env(tmp_path, monkeypatch):
    """Set up a minimal wolt environment with a registered session."""
    import sessions
    import paths

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

    # Create wolt dir
    wolt_dir = tmp_path / "testwolt" / "wolt"
    wolt_dir.mkdir(parents=True)

    # Create a session in registry
    reg = sessions.SessionRegistry(tmp_path)
    reg.create(
        "testwolt-chompy-dam-abc123",
        wolt="testwolt",
        creature="raccoon",
        dir=str(tmp_path / "testwolt"),
    )
    return tmp_path


class TestResumeSessionClaudeRunning:
    """Path 1: Claude is running in tmux — send keys directly."""

    @patch("sessions.subprocess.run")
    @patch("sessions._session_has_claude_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_sends_keys_when_claude_running(self, mock_alive, mock_claude, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "fix the bug")

        assert result["status"] == "delivered"
        assert result["name"] == "testwolt-chompy-dam-abc123"
        # Should have sent keys (two calls: send-keys -l, send-keys Enter)
        send_calls = [c for c in mock_run.call_args_list if "send-keys" in str(c)]
        assert len(send_calls) == 2

    @patch("sessions.subprocess.run")
    @patch("sessions._session_has_claude_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_no_keys_without_prompt(self, mock_alive, mock_claude, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "")

        assert result["status"] == "delivered"
        # No send-keys calls when prompt is empty
        send_calls = [c for c in mock_run.call_args_list if "send-keys" in str(c)]
        assert len(send_calls) == 0

    @patch("sessions.subprocess.run")
    @patch("sessions._session_has_claude_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_updates_status_to_running(self, mock_alive, mock_claude, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry
        resume_session("testwolt-chompy-dam-abc123", "hello")

        reg = SessionRegistry(wolt_env)
        data = reg.get("testwolt-chompy-dam-abc123", check_alive=False)
        assert data["status"] == "running"


class TestResumeSessionClaudeExited:
    """Path 2: Tmux alive but claude exited — restart with --resume."""

    @patch("sessions.subprocess.run")
    @patch("sessions._session_has_claude_process", return_value=False)
    @patch("sessions._tmux_alive", return_value=True)
    def test_revives_claude_with_resume(self, mock_alive, mock_claude, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "continue working")

        assert result["status"] == "revived"
        # Should have sent resume command via send-keys
        send_calls = [c for c in mock_run.call_args_list if "send-keys" in str(c)]
        assert len(send_calls) == 2
        # The command should include --resume with the session name
        cmd_call = send_calls[0]
        cmd_str = str(cmd_call)
        assert "--resume" in cmd_str
        assert "testwolt-chompy-dam-abc123" in cmd_str


class TestResumeSessionTmuxDead:
    """Path 3: Tmux is dead — create new tmux session with --resume."""

    @patch("sessions.subprocess.run")
    @patch("sessions._session_has_claude_process", return_value=False)
    @patch("sessions._tmux_alive", return_value=False)
    def test_respawns_tmux_with_resume(self, mock_alive, mock_claude, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "pick up where we left off")

        assert result["status"] == "respawned"
        # Should have called tmux new-session
        new_session_calls = [c for c in mock_run.call_args_list if "new-session" in str(c)]
        assert len(new_session_calls) == 1
        cmd_str = str(new_session_calls[0])
        assert "--resume" in cmd_str
        assert "testwolt-chompy-dam-abc123" in cmd_str

    @patch("sessions.subprocess.run")
    @patch("sessions._tmux_alive", return_value=False)
    def test_updates_status_to_running(self, mock_alive, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry

        # Mark session as orphaned first
        reg = SessionRegistry(wolt_env)
        reg.update("testwolt-chompy-dam-abc123", wolt="testwolt", status="orphaned")

        resume_session("testwolt-chompy-dam-abc123", "hello")

        data = reg.get("testwolt-chompy-dam-abc123", check_alive=False)
        assert data["status"] == "running"


class TestResumeSessionNotFound:
    """Resume should raise ValueError for unknown sessions."""

    def test_raises_for_missing_session(self, wolt_env):
        from sessions import resume_session
        with pytest.raises(ValueError, match="not found"):
            resume_session("nonexistent-session-abc123", "hello")


class TestStartSessionSetsClaudeSessionId:
    """start_session() should set claude_session_id = session name."""

    @pytest.fixture(autouse=True)
    def setup_wolt(self, tmp_path, monkeypatch):
        import sessions
        import sites
        import paths

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
        monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

        wolt_dir = tmp_path / "testwolt" / "wolt"
        wolt_dir.mkdir(parents=True)
        site_dir = wolt_dir / "site"
        site_dir.mkdir()
        (site_dir / "index.html").write_text("<h1>test</h1>")
        (wolt_dir / "wolt.json").write_text(json.dumps({
            "name": "testwolt", "type": "raccoon",
        }))
        self.wolts_dir = tmp_path

    @patch("sessions.subprocess.run")
    @patch("sites.subprocess.Popen")
    def test_claude_session_id_equals_session_name(self, mock_popen, mock_run):
        from sessions import start_session, SessionRegistry
        mock_popen.return_value.pid = 12345
        result = start_session(wolt="testwolt", prompt="hello")

        reg = SessionRegistry(self.wolts_dir)
        data = reg.get(result["name"], check_alive=False)
        assert data["claude_session_id"] == result["name"]
