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


def _absent_then_present(pane_id: str = "%1", absences: int = 2):
    """resolve_agent_handle stand-in: no agent now, an agent after the relaunch.

    resume_session asks at least three times on a revive — once to pick the
    path (nothing running), once to rule out a launching shim (a session still
    booting is waited for, never relaunched), and once to verify the relaunch
    actually produced an agent. A stub that always answers None describes a
    resume that failed, not one that worked. `absences` covers paths that
    relaunch twice (revive in place, then escalate to a clean respawn).
    """
    remaining = [None] * absences

    def _resolve(*args, **kwargs):
        if remaining:
            remaining.pop()
            return None
        return _agent_in(pane_id)

    return _resolve


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

    @patch("sessions.resolve_agent_handle", side_effect=_absent_then_present("%1"))
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

    @patch("sessions.resolve_agent_handle", side_effect=_absent_then_present("%8"))
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
    def test_respawns_tmux_with_resume(self, mock_alive, mock_agent, mock_run,
                                       wolt_env, agent_comes_up):
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
    def test_updates_status_to_running(self, mock_alive, mock_run, wolt_env,
                                       agent_comes_up):
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


class TestResumeNeedsAConversation:
    """A resume with no conversation id must refuse, loudly, before touching tmux.

    Every harness silently drops its resume flag when handed an empty id
    (_claude_command, _codex_command, _opencode_command all guard on
    `if resume_id`). So an unstamped session used to launch a BRAND NEW agent
    into the old session's slot: tmux alive, agent alive, prompt delivered,
    conversation gone — and the caller was told "respawned".
    """

    @pytest.fixture
    def unstamped(self, tmp_path, monkeypatch):
        import paths
        import sessions

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)
        (tmp_path / "testwolt" / "wolt").mkdir(parents=True)
        reg = sessions.SessionRegistry(tmp_path)
        reg.create("testwolt-blank-dam-abc123", wolt="testwolt",
                   dir=str(tmp_path / "testwolt"))
        return tmp_path

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=False)
    def test_resume_refuses_and_spawns_nothing(
        self, mock_alive, mock_agent, mock_run, unstamped
    ):
        from sessions import ResumeUnavailable, resume_session

        with pytest.raises(ResumeUnavailable, match="nothing to resume"):
            resume_session("testwolt-blank-dam-abc123", "hello")

        assert [c for c in mock_run.call_args_list if "new-session" in str(c)] == []

    def test_prepare_refuses_to_build_a_resume_command(self, unstamped):
        """The run-session.sh seam refuses too — the CLI prints the reason."""
        from sessions import prepare_session_command

        with pytest.raises(ValueError, match="no harness_session_id"):
            prepare_session_command("testwolt-blank-dam-abc123", "resume", "hi")

    @patch("sessions._tmux_alive", return_value=True)
    def test_prepare_points_to_live_tmux_instead_of_new_session(self, mock_alive, unstamped):
        from sessions import prepare_session_command

        with pytest.raises(ValueError, match="still running.*waiting for input.*open it"):
            prepare_session_command("testwolt-blank-dam-abc123", "resume", "hi")

    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=True)
    def test_resume_points_to_live_tmux_instead_of_new_session(
        self, mock_alive, mock_agent, unstamped
    ):
        from sessions import ResumeUnavailable, resume_session

        with pytest.raises(ResumeUnavailable, match="still running.*waiting for input.*open it"):
            resume_session("testwolt-blank-dam-abc123", "hello")

    def test_a_non_uuid_legacy_id_does_not_count(self, unstamped):
        """Some legacy records stored the session NAME in claude_session_id.

        That is not a conversation id; resuming on it would start a fresh
        agent. The UUID check already existed in prepare — this makes it a
        refusal rather than a silent downgrade.
        """
        from sessions import ResumeUnavailable, SessionRegistry, resume_session

        SessionRegistry(unstamped).update(
            "testwolt-blank-dam-abc123", wolt="testwolt",
            claude_session_id="testwolt-blank-dam-abc123",
        )
        with pytest.raises(ResumeUnavailable):
            resume_session("testwolt-blank-dam-abc123", "hello")


class TestResumeVerification:
    """A relaunch is not a resume until an agent actually shows up.

    tmux returns the instant the pane exists — long before run-session.sh has
    decided whether it can start anything. Reporting success in that gap is
    what let the TUI attach to a dying shell and call it a wake.
    """

    @patch("sessions.session_has_agent_process", return_value=False)
    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=False)
    def test_respawn_that_never_boots_an_agent_raises(
        self, mock_alive, mock_agent, mock_has_agent,
        wolt_env, fake_runtime, monkeypatch
    ):
        import sessions
        from sessions import ResumeFailed, SessionRegistry, resume_session

        monkeypatch.setattr(sessions, "_AGENT_APPEAR_TIMEOUT", 0.0)
        monkeypatch.setattr(sessions, "_AGENT_REVIVE_TIMEOUT", 0.0)

        with pytest.raises(ResumeFailed, match="no claude process appeared"):
            resume_session("testwolt-chompy-dam-abc123", "hello")

        # It spawned — and then said so honestly instead of claiming a wake.
        assert len(fake_runtime.spawns) == 1
        stored = SessionRegistry(wolt_env).get(
            "testwolt-chompy-dam-abc123", check_alive=False)
        # ...without stamping the record. "failed" is terminal — list() only
        # re-classifies "running" records — so a record stamped here on nothing
        # but a timeout could hide a live agent forever.
        assert stored["status"] == "running"

    @patch("sessions.session_has_agent_process", return_value=True)
    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=False)
    def test_an_agent_that_lands_just_past_the_wait_is_a_wake(
        self, mock_alive, mock_agent, mock_has_agent,
        wolt_env, fake_runtime, monkeypatch
    ):
        """The boot path is three python subprocesses deep before the agent
        execs. On a loaded host that can outlast our patience, and the session
        is then perfectly healthy — so the wait running out asks once more,
        session-wide, rather than declaring a failure it cannot see."""
        import sessions
        from sessions import SessionRegistry, resume_session

        monkeypatch.setattr(sessions, "_AGENT_APPEAR_TIMEOUT", 0.0)
        monkeypatch.setattr(sessions, "_AGENT_REVIVE_TIMEOUT", 0.0)

        result = resume_session("testwolt-chompy-dam-abc123", "hello")

        assert result["status"] == "respawned"
        assert "just past the wait" in result["detail"]
        stored = SessionRegistry(wolt_env).get(
            "testwolt-chompy-dam-abc123", check_alive=False)
        assert stored["status"] == "running"

    @patch("sessions.session_has_agent_process", return_value=False)
    @patch("sessions.resolve_agent_handle",
           side_effect=_absent_then_present("%1", absences=3))
    @patch("sessions._tmux_alive", return_value=True)
    def test_a_stale_agentless_tmux_session_is_killed_and_respawned(
        self, mock_alive, mock_agent, mock_has_agent,
        wolt_env, fake_runtime, monkeypatch
    ):
        """The live bug: tmux holds the session, only a login shell inside.

        Reviving in place is tried first (cheap, keeps the pane). When that
        produces no agent, the husk is killed and the session respawns clean —
        which is the path that actually gets the human their conversation back.
        """
        import sessions
        from sessions import SessionRegistry, resume_session
        from session_runtime import RuntimeHandle

        name = "testwolt-chompy-dam-abc123"
        monkeypatch.setattr(sessions, "_AGENT_APPEAR_TIMEOUT", 0.0)
        monkeypatch.setattr(sessions, "_AGENT_REVIVE_TIMEOUT", 0.0)
        SessionRegistry(wolt_env).update(
            name, wolt="testwolt",
            runtime=RuntimeHandle(name, name, "%1").to_record(),
        )

        result = resume_session(name, "continue working")

        assert result["status"] == "respawned"
        # Tried the cheap in-place revival first...
        assert len(fake_runtime.pastes) == 1
        # ...then cleared the husk and built a fresh session.
        assert fake_runtime.stops == [name]
        assert len(fake_runtime.spawns) == 1
        session_id, cwd, command = fake_runtime.last_spawn
        assert session_id == name
        assert "--resume" in command

    @patch("sessions.session_has_agent_process", return_value=True)
    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=True)
    def test_it_never_kills_a_session_that_has_a_live_agent(
        self, mock_alive, mock_agent, mock_has_agent,
        wolt_env, fake_runtime, monkeypatch
    ):
        """The escalation must not become a foot-gun.

        resolve_agent_handle can miss (a pane scoped wrong, a harness the
        session-level walk still sees). Killing on that would destroy exactly
        the conversation the resume was asked to rescue — so the session-level
        check gets the last word, and anything but a definite False is left
        alone.
        """
        import sessions
        from sessions import ResumeFailed, SessionRegistry, resume_session
        from session_runtime import RuntimeHandle

        name = "testwolt-chompy-dam-abc123"
        monkeypatch.setattr(sessions, "_AGENT_APPEAR_TIMEOUT", 0.0)
        monkeypatch.setattr(sessions, "_AGENT_REVIVE_TIMEOUT", 0.0)
        SessionRegistry(wolt_env).update(
            name, wolt="testwolt",
            runtime=RuntimeHandle(name, name, "%1").to_record(),
        )

        with pytest.raises(ResumeFailed, match="left alone"):
            resume_session(name, "continue working")

        assert fake_runtime.stops == []
        assert fake_runtime.spawns == []


    @patch("sessions.resolve_agent_handle", return_value=None)
    @patch("sessions._tmux_alive", return_value=True)
    def test_the_kill_check_asks_about_the_session_not_the_pasted_pane(
        self, mock_alive, mock_agent, wolt_env, fake_runtime, monkeypatch
    ):
        """The pane we pasted into is the *last* place to ask.

        A session reaches the escalation exactly when its dedicated pane is a
        leftover login shell — and the reason that session still exists is
        usually a second window with the human's own agent in it. A handle
        carrying a pane_id narrows the liveness walk to that one pane, so the
        "no agent anywhere" check answered False and the kill took the live
        agent with it.
        """
        import sessions
        from sessions import ResumeFailed, resume_session
        from session_runtime import RuntimeHandle

        name = "testwolt-chompy-dam-abc123"
        monkeypatch.setattr(sessions, "_AGENT_APPEAR_TIMEOUT", 0.0)
        monkeypatch.setattr(sessions, "_AGENT_REVIVE_TIMEOUT", 0.0)
        sessions.SessionRegistry(wolt_env).update(
            name, wolt="testwolt",
            runtime=RuntimeHandle(name, name, "%1").to_record(),
        )
        # Two panes: the dedicated one we paste into (%1, agentless) and the
        # human's own window (%2, a live agent).
        fake_runtime._panes = [("%1", "100", True), ("%2", "200", False)]
        fake_runtime._agents = {"%2"}

        with pytest.raises(ResumeFailed, match="left alone"):
            resume_session(name, "continue working")

        assert fake_runtime.stops == []
        assert fake_runtime.spawns == []


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
    tmp_path, monkeypatch, agent_comes_up
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
    reg.update(name, wolt="testwolt", runtime=original.to_record(),
               harness_session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

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


class TestResumeRacesARest:
    """resume_session looks for the agent without the session lock, then
    pastes. A rest takes that lock, stops tmux and writes resting in between.
    The paste goes under the lock, after a second look, so the message is
    never pasted into a pane being stopped: it wakes the session instead."""

    NAME = "testwolt-chompy-dam-abc123"

    def _race(self, wolt_env, monkeypatch, *, after):
        """Run resume_session until the agent is ready, then hold the lock and
        apply `after(registry, phase)` before letting the paste go on."""
        import sessions
        import threading

        registry = sessions.SessionRegistry(wolt_env)
        phase = {"now": "agent"}

        def resolve(target, harness, include_launching=False):
            if phase["now"] == "agent":
                return _agent_in("%7")
            if phase["now"] == "booting" and include_launching:
                return _agent_in("%9")
            return None

        monkeypatch.setattr(sessions, "resolve_agent_handle", resolve)
        monkeypatch.setattr(sessions, "_tmux_alive", lambda _s: phase["now"] != "rested")
        ready = threading.Event()

        def await_ready(target, harness, *args, **kwargs):
            ready.set()
            return target

        monkeypatch.setattr(sessions, "_await_input_ready", await_ready)
        pastes = []
        monkeypatch.setattr(sessions, "_tmux_paste", lambda target, text, settle=0.0: pastes.append((target, text)))
        outcome = {}

        def resume():
            try:
                outcome["result"] = sessions.resume_session(self.NAME, "hello")
            except Exception as exc:  # noqa: BLE001 - reported below
                outcome["error"] = exc

        worker = threading.Thread(target=resume, daemon=True)
        with registry._lock("testwolt", self.NAME):
            worker.start()
            assert ready.wait(5), "resume never found the agent ready"
            after(registry, phase)
        worker.join(10)
        assert not worker.is_alive()
        return registry, pastes, outcome

    @patch("sessions.subprocess.run")
    def test_a_rest_that_won_the_race_turns_the_paste_into_a_wake(
            self, mock_run, wolt_env, monkeypatch, agent_comes_up):
        def rest(registry, phase):
            data = registry._read("testwolt", self.NAME)
            data["status"] = "resting"
            registry._write("testwolt", self.NAME, data)
            phase["now"] = "rested"

        registry, pastes, outcome = self._race(wolt_env, monkeypatch, after=rest)
        assert "error" not in outcome, outcome
        assert pastes == [], "the message was pasted into a session being stopped"
        assert outcome["result"]["status"] == "respawned"
        spawned = [c for c in mock_run.call_args_list if "new-session" in str(c)]
        assert len(spawned) == 1 and "--resume" in str(spawned[0])
        assert registry._read("testwolt", self.NAME)["status"] == "running"

    @patch("sessions.subprocess.run")
    def test_a_session_woken_again_meanwhile_is_not_relaunched_over(
            self, mock_run, wolt_env, monkeypatch, agent_comes_up):
        """Rested and woken by someone else before the paste: the record says
        running, but only the new launcher is up. Relaunching would paste
        `run-session.sh --resume` into that booting pane: refuse instead."""
        def rested_and_woken(registry, phase):
            phase["now"] = "booting"

        registry, pastes, outcome = self._race(wolt_env, monkeypatch, after=rested_and_woken)
        assert pastes == []
        assert not [c for c in mock_run.call_args_list if "new-session" in str(c) or "respawn" in str(c)]
        from sessions import ResumeFailed
        assert isinstance(outcome.get("error"), ResumeFailed)
        assert "retry" in str(outcome["error"])

    @patch("sessions.subprocess.run")
    @patch("sessions.resolve_agent_handle", return_value=_agent_in("%7"))
    @patch("sessions._tmux_alive", return_value=True)
    def test_a_pasted_message_is_use(self, mock_alive, mock_agent, mock_run, wolt_env):
        from sessions import resume_session, SessionRegistry
        reg = SessionRegistry(wolt_env)
        data = reg._read("testwolt", self.NAME)
        data["last_activity"] = 1000
        reg._write("testwolt", self.NAME, data)
        before = int(time.time())
        resume_session(self.NAME, "hello")
        assert reg._read("testwolt", self.NAME)["last_activity"] >= before
