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

    # Create a session in registry with a UUID for claude_session_id
    reg = sessions.SessionRegistry(tmp_path)
    reg.create(
        "testwolt-chompy-dam-abc123",
        wolt="testwolt",
        creature="raccoon",
        dir=str(tmp_path / "testwolt"),
    )
    reg.update("testwolt-chompy-dam-abc123", wolt="testwolt",
               claude_session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")
    return tmp_path


class TestResumeSessionClaudeRunning:
    """Path 1: Claude is running in tmux — send keys directly."""

    @patch("sessions.subprocess.run")
    @patch("sessions.session_has_agent_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_pastes_when_claude_running(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "fix the bug")

        assert result["status"] == "delivered"
        assert result["name"] == "testwolt-chompy-dam-abc123"
        # Should have used set-buffer + paste-buffer (two calls)
        buf_calls = [c for c in mock_run.call_args_list if "set-buffer" in str(c) or "paste-buffer" in str(c)]
        assert len(buf_calls) == 2

    @patch("sessions.subprocess.run")
    @patch("sessions.session_has_agent_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_no_keys_without_prompt(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "")

        assert result["status"] == "delivered"
        # No tmux buffer calls when prompt is empty
        buf_calls = [c for c in mock_run.call_args_list if "set-buffer" in str(c) or "paste-buffer" in str(c)]
        assert len(buf_calls) == 0

    @patch("sessions.subprocess.run")
    @patch("sessions.session_has_agent_process", return_value=True)
    @patch("sessions._tmux_alive", return_value=True)
    def test_updates_status_to_running(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry
        resume_session("testwolt-chompy-dam-abc123", "hello")

        reg = SessionRegistry(wolt_env)
        data = reg.get("testwolt-chompy-dam-abc123", check_alive=False)
        assert data["status"] == "running"


class TestResumeSessionClaudeExited:
    """Path 2: Tmux alive but claude exited — restart with --resume."""

    @patch("sessions.subprocess.run")
    @patch("sessions.session_has_agent_process", return_value=False)
    @patch("sessions._tmux_alive", return_value=True)
    def test_revives_claude_with_resume(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "continue working")

        assert result["status"] == "revived"
        # Should have used set-buffer + paste-buffer (two calls)
        buf_calls = [c for c in mock_run.call_args_list if "set-buffer" in str(c) or "paste-buffer" in str(c)]
        assert len(buf_calls) == 2
        # The set-buffer call delivers the run-session.sh wrapper in resume mode
        set_call = [c for c in mock_run.call_args_list if "set-buffer" in str(c)][0]
        cmd_str = str(set_call)
        assert "run-session.sh" in cmd_str
        assert "--resume" in cmd_str
        assert "'continue working'" in cmd_str

    def test_prepare_resume_command_uses_stored_uuid(self, wolt_env):
        """The agent-level --resume UUID comes from prepare_session_command."""
        from sessions import prepare_session_command
        cmd = prepare_session_command("testwolt-chompy-dam-abc123", "resume", "continue")
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "wclaude" in cmd


class TestResumeSessionTmuxDead:
    """Path 3: Tmux is dead — create new tmux session with --resume."""

    @patch("sessions.subprocess.run")
    @patch("sessions.session_has_agent_process", return_value=False)
    @patch("sessions._tmux_alive", return_value=False)
    def test_respawns_tmux_with_resume(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "pick up where we left off")

        assert result["status"] == "respawned"
        # Should have called tmux new-session running the wrapper in resume mode
        new_session_calls = [c for c in mock_run.call_args_list if "new-session" in str(c)]
        assert len(new_session_calls) == 1
        cmd_str = str(new_session_calls[0])
        assert "run-session.sh" in cmd_str
        assert "--resume" in cmd_str

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


class TestStartSessionNoClaudeSessionId:
    """start_session() should NOT set claude_session_id — run-session.sh generates a UUID."""

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
    def test_start_session_does_not_set_claude_session_id(self, mock_popen, mock_run):
        from sessions import start_session, SessionRegistry
        mock_popen.return_value.pid = 12345
        result = start_session(wolt="testwolt", prompt="hello")

        reg = SessionRegistry(self.wolts_dir)
        data = reg.get(result["name"], check_alive=False)
        # claude_session_id is not set by start_session — run-session.sh does it
        assert "claude_session_id" not in data or not data.get("claude_session_id")
