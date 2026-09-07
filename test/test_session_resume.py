"""Session resume tests — unit tests for resume_session().

Tests the four resume paths:
  1. Claude running in tmux → send keys directly
  2. Tmux alive, claude exited → restart wclaude --resume in pane
  3. Tmux alive, saved pane gone → create a dedicated replacement window
  4. Tmux dead → create new tmux with wclaude --resume

Usage: uv run --project server --with pytest pytest test/test_session_resume.py -v
"""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from conftest import requires_tmux


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


def _agent_in(pane_id: str):
    """The handle resolve_agent_handle would return for an agent in pane_id."""
    from session_runtime import RuntimeHandle
    return RuntimeHandle("testwolt-chompy-dam-abc123",
                         "testwolt-chompy-dam-abc123", pane_id)


class TestResumeSessionClaudeRunning:
    """Path 1: Claude is running in tmux — send keys directly."""

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=_agent_in("%7"))
    @patch("sessions._tmux_alive", return_value=True)
    def test_pastes_when_claude_running(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "fix the bug")

        assert result["status"] == "delivered"
        assert result["name"] == "testwolt-chompy-dam-abc123"
        # Should have used set-buffer + paste-buffer (two calls)
        buf_calls = [c for c in mock_run.call_args_list if "set-buffer" in str(c) or "paste-buffer" in str(c)]
        assert len(buf_calls) == 2
        # ...and aimed at the pane the agent was actually found in, not the
        # session's active pane (which may be a window the human opened).
        targets = [c.args[0][-1] for c in mock_run.call_args_list if "paste-buffer" in str(c)]
        assert targets == ["%7"]

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=_agent_in("%7"))
    @patch("sessions._tmux_alive", return_value=True)
    def test_no_keys_without_prompt(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session
        result = resume_session("testwolt-chompy-dam-abc123", "")

        assert result["status"] == "delivered"
        # No tmux buffer calls when prompt is empty
        buf_calls = [c for c in mock_run.call_args_list if "set-buffer" in str(c) or "paste-buffer" in str(c)]
        assert len(buf_calls) == 0

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=_agent_in("%7"))
    @patch("sessions._tmux_alive", return_value=True)
    def test_updates_status_to_running(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry
        resume_session("testwolt-chompy-dam-abc123", "hello")

        reg = SessionRegistry(wolt_env)
        data = reg.get("testwolt-chompy-dam-abc123", check_alive=False)
        assert data["status"] == "running"
        assert data["runtime"]["pane_id"] == "%7"


class TestResumeSessionClaudeExited:
    """Path 2: Tmux alive but claude exited — restart with --resume."""

    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=True)
    def test_revives_claude_with_resume(
        self, mock_alive, mock_agent, wolt_env, fake_runtime
    ):
        from sessions import resume_session, SessionRegistry
        from session_runtime import RuntimeHandle

        reg = SessionRegistry(wolt_env)
        reg.update(
            "testwolt-chompy-dam-abc123",
            wolt="testwolt",
            runtime=RuntimeHandle(
                "testwolt-chompy-dam-abc123",
                "testwolt-chompy-dam-abc123",
                "%1",
            ).to_record(),
        )

        result = resume_session("testwolt-chompy-dam-abc123", "continue working")

        assert result["status"] == "revived"
        assert fake_runtime.in_session_spawns == []
        pane_id, command, settle = fake_runtime.last_paste
        assert pane_id == "%1"
        assert settle == 0.0
        cmd_str = str(command)
        assert "run-session.sh" in cmd_str
        assert "--resume" in cmd_str
        assert "'continue working'" in cmd_str

    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=True)
    def test_missing_saved_pane_gets_a_dedicated_window(
        self, mock_alive, mock_agent, wolt_env, fake_runtime
    ):
        from sessions import resume_session, SessionRegistry
        from session_runtime import RuntimeHandle

        name = "testwolt-chompy-dam-abc123"
        reg = SessionRegistry(wolt_env)
        reg.update(
            name,
            wolt="testwolt",
            runtime=RuntimeHandle(name, name, "%99").to_record(),
        )
        fake_runtime._next_pane = "%8"

        result = resume_session(name, "continue working")

        assert result["status"] == "revived"
        assert "new window" in result["detail"]
        assert fake_runtime.pastes == []
        assert len(fake_runtime.in_session_spawns) == 1
        session_name, cwd, command = fake_runtime.in_session_spawns[0]
        assert session_name == name
        assert cwd == str(wolt_env / "testwolt")
        assert "run-session.sh" in command
        assert "--resume" in command
        stored = reg.get(name, check_alive=False)
        assert stored["runtime"]["pane_id"] == "%8"

    def test_prepare_resume_command_uses_stored_uuid(self, wolt_env):
        """The agent-level --resume UUID comes from prepare_session_command."""
        from sessions import prepare_session_command
        cmd = prepare_session_command("testwolt-chompy-dam-abc123", "resume", "continue")
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "wclaude" in cmd


class TestResumeSessionTmuxDead:
    """Path 3: Tmux is dead — create new tmux session with --resume."""

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=None)
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


class TestResumeSessionForeignRuntime:
    """A record whose workdir does not exist on this host (written by the
    other runtime across a migrated data root) must refuse to resume loudly
    instead of respawning a dead-on-arrival tmux and reporting success."""

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=False)
    def test_refuses_resume_when_workdir_missing(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry

        reg = SessionRegistry(wolt_env)
        foreign = "/workspace/wolts/testwolt-does-not-exist-here"
        reg.update("testwolt-chompy-dam-abc123", wolt="testwolt",
                   dir=foreign, workdir=foreign,
                   target={"wolt_id": "testwolt", "canonical_workdir": foreign})

        with pytest.raises(ValueError, match="different runtime"):
            resume_session("testwolt-chompy-dam-abc123", "hello")

        # And it never spawned anything.
        new_session_calls = [c for c in mock_run.call_args_list if "new-session" in str(c)]
        assert new_session_calls == []


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
    def test_start_session_does_not_set_claude_session_id(self, mock_run):
        from sessions import start_session, SessionRegistry
        result = start_session(wolt="testwolt", prompt="hello")

        reg = SessionRegistry(self.wolts_dir)
        data = reg.get(result["name"], check_alive=False)
        # claude_session_id is not set by start_session — run-session.sh does it
        assert "claude_session_id" not in data or not data.get("claude_session_id")


@requires_tmux
def test_resume_replaces_a_missing_agent_pane_without_touching_user_layout(
    tmp_path, monkeypatch
):
    """A surviving tmux session gets a new agent window, never a user pane."""
    import paths
    import sessions
    from session_runtime import RuntimeHandle, TmuxSessionRuntime

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

    wolt_dir = tmp_path / "testwolt" / "wolt"
    wolt_dir.mkdir(parents=True)

    name = f"test-stale-layout-{uuid.uuid4().hex[:10]}"
    runtime = TmuxSessionRuntime()
    original = runtime.spawn(name, str(tmp_path), "cat")

    reg = sessions.SessionRegistry(tmp_path)
    reg.create(name, wolt="testwolt", dir=str(tmp_path / "testwolt"))
    reg.update(name, wolt="testwolt", runtime=original.to_record())

    try:
        subprocess.run(
            ["tmux", "new-window", "-d", "-t", f"={name}", "bash"],
            check=True,
        )
        before = runtime.panes_for_session(name)
        user_pane = next(p.pane_id for p in before if p.pane_id != original.pane_id)

        subprocess.run(["tmux", "kill-pane", "-t", original.pane_id], check=True)
        assert runtime.is_alive(original) is True
        assert runtime.handle_is_alive(original) is False
        current_before = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert current_before == user_pane

        monkeypatch.setattr(sessions, "build_session_command", lambda *args, **kwargs: "cat")
        result = sessions.resume_session(name, "continue")

        stored = reg.get(name, check_alive=False)
        replacement = RuntimeHandle.from_record(stored)
        after_ids = {p.pane_id for p in runtime.panes_for_session(name)}
        current_after = subprocess.run(
            ["tmux", "display-message", "-p", "-t", name, "#{pane_id}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        assert result["status"] == "revived"
        assert "new window" in result["detail"]
        assert replacement.pane_id not in {"", original.pane_id, user_pane}
        assert after_ids == {user_pane, replacement.pane_id}
        assert current_after == user_pane
        assert runtime.handle_is_alive(replacement) is True
        assert runtime.has_descendant_process(replacement, {"cat"}) is True
    finally:
        runtime.stop(RuntimeHandle(name, name))
