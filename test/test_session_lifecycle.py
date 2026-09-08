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
from unittest.mock import patch

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

        fetched = reg.get("test-session-1", wolt="neowolt", check_alive=False)
        assert fetched["name"] == "test-session-1"

    def test_update_fields(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-2", wolt="neowolt")
        updated = reg.update("test-session-2", viewport_url="http://example.com")
        assert updated["viewport_url"] == "http://example.com"

    def test_finish_success(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-3", wolt="neowolt")
        finished = reg.finish("test-session-3", 0, wolt="neowolt")
        assert finished["status"] == "completed"
        assert finished["exit_code"] == 0
        assert finished["finished_at"] is not None

    def test_finish_failure(self, tmp_registry):
        reg = tmp_registry
        reg.create("test-session-4", wolt="neowolt")
        finished = reg.finish("test-session-4", 1, wolt="neowolt")
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
        reg.create("to-delete", wolt="neowolt")
        assert reg.delete("to-delete", wolt="neowolt") is True
        assert reg.get("to-delete", wolt="neowolt", check_alive=False) is None
        assert reg.delete("to-delete", wolt="neowolt") is False

    def test_touch_updates_activity(self, tmp_registry):
        reg = tmp_registry
        reg.create("touch-me", wolt="neowolt")
        before = reg.get("touch-me", wolt="neowolt", check_alive=False)["last_activity"]
        time.sleep(1.1)
        reg.touch("touch-me", wolt="neowolt")
        after = reg.get("touch-me", wolt="neowolt", check_alive=False)["last_activity"]
        assert after > before

    def test_get_nonexistent(self, tmp_registry):
        assert tmp_registry.get("nope", wolt="neowolt", check_alive=False) is None

    def test_update_nonexistent(self, tmp_registry):
        assert tmp_registry.update("nope", wolt="neowolt", status="done") is None

    def test_prompt_truncated(self, tmp_registry):
        reg = tmp_registry
        long_prompt = "x" * 1000
        data = reg.create("truncate-test", wolt="neowolt", prompt=long_prompt)
        assert len(data["prompt"]) == 500

    def test_atomic_write(self, tmp_registry):
        """Write uses tmp + rename for atomicity — no partial reads."""
        reg = tmp_registry
        reg.create("atomic-test", wolt="neowolt")
        sessions_dir = reg.wolts_dir / "neowolt" / ".state" / "sessions"
        tmp_files = list(sessions_dir.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_corrupt_json_handled(self, tmp_registry):
        """Corrupt JSON in registry should not crash."""
        reg = tmp_registry
        # Create a valid session first so the wolt dir exists
        reg.create("valid-session", wolt="neowolt")
        sessions_dir = reg.wolts_dir / "neowolt" / ".state" / "sessions"
        bad_file = sessions_dir / "corrupt.json"
        bad_file.write_text("{invalid json")
        # get should return None
        assert reg.get("corrupt", wolt="neowolt", check_alive=False) is None
        # list should skip it
        sessions = reg.list(wolt="neowolt")
        assert all(s["name"] != "corrupt" for s in sessions)


# ---------------------------------------------------------------------------
# Liveness: agent present, not tmux present
# ---------------------------------------------------------------------------

class TestListLiveness:
    """`alive` in the session list means an agent is in there.

    A tmux session outlives its agent — a failed respawn, an OOM-killed
    harness, or an ordinary exit all leave the login shell holding the session
    open. Reporting that as alive is what let the TUI *attach* to it: the human
    pressed enter expecting their conversation and got a bare shell wearing the
    session's name.
    """

    def _reg(self, tmp_registry, monkeypatch, *, tmux, agents):
        import sessions
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set(tmux))
        monkeypatch.setattr(
            sessions, "sessions_with_agent_process",
            lambda: None if agents is None else set(agents),
        )
        return tmp_registry

    def test_an_agentless_tmux_session_is_not_alive(self, tmp_registry, monkeypatch):
        reg = self._reg(tmp_registry, monkeypatch,
                        tmux={"husk-sess"}, agents=set())
        reg.create("husk-sess", wolt="neowolt")
        reg.update("husk-sess", wolt="neowolt", status="running")

        row = reg.list()[0]
        assert row["tmux_alive"] is True
        assert row["agent_alive"] is False
        assert row["alive"] is False
        # ...and it stops claiming to be running, with the reason spelled out.
        assert row["status"] == "orphaned"
        assert row["orphaned_reason"] == sessions_module().ORPHAN_AGENT_GONE

    def test_an_agent_bearing_session_is_alive(self, tmp_registry, monkeypatch):
        reg = self._reg(tmp_registry, monkeypatch,
                        tmux={"live-sess"}, agents={"live-sess"})
        reg.create("live-sess", wolt="neowolt")
        reg.update("live-sess", wolt="neowolt", status="running")

        row = reg.list()[0]
        assert (row["tmux_alive"], row["agent_alive"], row["alive"]) == (True, True, True)
        assert row["status"] == "running"

    def test_a_missing_tmux_session_keeps_its_own_orphan_reason(
        self, tmp_registry, monkeypatch
    ):
        reg = self._reg(tmp_registry, monkeypatch, tmux=set(), agents=set())
        reg.create("gone-sess", wolt="neowolt")
        reg.update("gone-sess", wolt="neowolt", status="running")

        row = reg.list()[0]
        assert row["tmux_alive"] is False
        assert row["orphaned_reason"] == sessions_module().ORPHAN_TMUX_MISSING

    def test_alive_only_filters_on_the_agent(self, tmp_registry, monkeypatch):
        reg = self._reg(tmp_registry, monkeypatch,
                        tmux={"husk-sess", "live-sess"}, agents={"live-sess"})
        reg.create("husk-sess", wolt="neowolt")
        reg.create("live-sess", wolt="neowolt")

        assert [s["name"] for s in reg.list(alive_only=True)] == ["live-sess"]

    def test_an_unreadable_process_table_falls_back_to_tmux(
        self, tmp_registry, monkeypatch
    ):
        """None is 'undetermined', never 'dead'.

        A ps hiccup must not mark the colony offline and route every enter
        through a resume.
        """
        reg = self._reg(tmp_registry, monkeypatch,
                        tmux={"live-sess"}, agents=None)
        reg.create("live-sess", wolt="neowolt")

        row = reg.list()[0]
        assert (row["tmux_alive"], row["agent_alive"], row["alive"]) == (True, True, True)

    def test_liveness_follows_the_persisted_tmux_name(self, tmp_registry, monkeypatch):
        """The tmux session name is not always the woltspace session name."""
        reg = self._reg(tmp_registry, monkeypatch,
                        tmux={"tmux-name"}, agents={"tmux-name"})
        reg.create("wolt-sess", wolt="neowolt")
        reg.update("wolt-sess", wolt="neowolt", runtime={
            "woltspace_session_id": "wolt-sess",
            "tmux_session_name": "tmux-name",
            "pane_id": "%3",
            "kind": "tmux",
        })

        assert reg.list()[0]["alive"] is True


class TestGetLiveness:
    """get(check_alive=True) means what list() means.

    It used to mean only "tmux holds this session", so the two disagreed about
    the same session: check_session reported a husk alive and running with pane
    output while list() called it orphaned, and deliver_message pasted into its
    login shell.
    """

    def _reg(self, tmp_registry, monkeypatch, *, tmux, agent):
        import sessions
        monkeypatch.setattr(sessions, "_tmux_alive", lambda s: tmux)
        monkeypatch.setattr(sessions, "session_has_agent_process",
                            lambda *a, **k: agent)
        return tmp_registry

    def test_a_husk_is_not_alive(self, tmp_registry, monkeypatch):
        reg = self._reg(tmp_registry, monkeypatch, tmux=True, agent=False)
        reg.create("husk-sess", wolt="neowolt")
        reg.update("husk-sess", wolt="neowolt", status="running")

        data = reg.get("husk-sess")
        assert (data["tmux_alive"], data["agent_alive"], data["alive"]) == (True, False, False)
        assert data["status"] == "orphaned"
        assert data["orphaned_reason"] == sessions_module().ORPHAN_AGENT_GONE

    def test_an_agent_bearing_session_is_alive(self, tmp_registry, monkeypatch):
        reg = self._reg(tmp_registry, monkeypatch, tmux=True, agent=True)
        reg.create("live-sess", wolt="neowolt")
        reg.update("live-sess", wolt="neowolt", status="running")

        data = reg.get("live-sess")
        assert data["alive"] is True
        assert data["status"] == "running"

    def test_undetermined_falls_back_to_tmux_presence(self, tmp_registry, monkeypatch):
        """`alive` is the permissive answer on purpose — a ps hiccup must not
        report the colony offline. Delivery gates on agent_alive instead."""
        reg = self._reg(tmp_registry, monkeypatch, tmux=True, agent=None)
        reg.create("fog-sess", wolt="neowolt")
        reg.update("fog-sess", wolt="neowolt", status="running")

        data = reg.get("fog-sess")
        assert data["agent_alive"] is None
        assert data["alive"] is True
        assert data["status"] == "running"

    def test_a_dead_tmux_session_skips_the_process_walk(self, tmp_registry, monkeypatch):
        """No panes means no processes. Don't pay for a ps fork to learn that."""
        import sessions
        walked = []
        monkeypatch.setattr(sessions, "_tmux_alive", lambda s: False)
        monkeypatch.setattr(sessions, "session_has_agent_process",
                            lambda *a, **k: walked.append(a) or True)
        tmp_registry.create("gone-sess", wolt="neowolt")
        tmp_registry.update("gone-sess", wolt="neowolt", status="running")

        data = tmp_registry.get("gone-sess")
        assert data["agent_alive"] is False
        assert data["alive"] is False
        assert data["orphaned_reason"] == sessions_module().ORPHAN_TMUX_MISSING
        assert walked == [], "tmux already answered it"

    def test_check_alive_false_computes_nothing(self, tmp_registry, monkeypatch):
        import sessions
        monkeypatch.setattr(sessions, "_tmux_alive",
                            lambda s: pytest.fail("must not ask tmux"))
        tmp_registry.create("quiet-sess", wolt="neowolt")

        data = tmp_registry.get("quiet-sess", check_alive=False)
        assert "alive" not in data
        assert "agent_alive" not in data

    def test_get_and_list_agree_about_a_husk(self, tmp_registry, monkeypatch):
        """The disagreement was the whole bug — pin it shut from both ends."""
        import sessions
        reg = tmp_registry
        reg.create("husk-sess", wolt="neowolt")
        reg.update("husk-sess", wolt="neowolt", status="running")
        monkeypatch.setattr(sessions, "_tmux_alive", lambda s: True)
        monkeypatch.setattr(sessions, "session_has_agent_process",
                            lambda *a, **k: False)
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: {"husk-sess"})
        monkeypatch.setattr(sessions, "sessions_with_agent_process", lambda: set())

        one = reg.get("husk-sess")
        many = reg.list()[0]
        for field in ("alive", "tmux_alive", "agent_alive", "status", "orphaned_reason"):
            assert one[field] == many[field], field


class TestBatchedAgentLiveness:
    """A fixed handful of process calls for the whole list, not two per session."""

    def test_twenty_sessions_cost_a_constant_number_of_process_calls(self, monkeypatch):
        import session_runtime

        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            from types import SimpleNamespace
            if argv[1] == "list-panes":
                rows = "\n".join(
                    f"sess-{i}\t%{i}\t{1000 + i}\t1" for i in range(20)
                )
                return SimpleNamespace(stdout=rows, returncode=0)
            # ps: every pane pid parents a claude
            rows = "\n".join(
                f"{2000 + i} {1000 + i} claude" for i in range(20)
            )
            return SimpleNamespace(stdout=rows, returncode=0)

        runtime = session_runtime.TmuxSessionRuntime(runner=fake_run)
        found = runtime.sessions_with_process({"claude"})

        assert found == {f"sess-{i}" for i in range(20)}
        # list-panes, then the two ps field sets that make one snapshot (comm
        # cannot be read in the same call as args without being truncated).
        # Three for twenty sessions is the property under test — not two.
        assert len(calls) == 3, f"expected list-panes + two ps, got {calls}"

    def test_an_unreadable_process_table_is_undetermined(self, monkeypatch):
        import session_runtime
        from types import SimpleNamespace

        def fake_run(argv, **kwargs):
            if argv[1] == "list-panes":
                return SimpleNamespace(stdout="sess-a\t%1\t1001\t1", returncode=0)
            raise OSError("ps: cannot read process table")

        runtime = session_runtime.TmuxSessionRuntime(runner=fake_run)
        assert runtime.sessions_with_process({"claude"}) is None

    def test_no_tmux_server_is_empty_not_undetermined(self):
        import session_runtime

        def fake_run(argv, **kwargs):
            raise OSError("no server running")

        runtime = session_runtime.TmuxSessionRuntime(runner=fake_run)
        assert runtime.sessions_with_process({"claude"}) == set()


def sessions_module():
    import sessions
    return sessions


# ---------------------------------------------------------------------------
# start_session — site auto-start (unit tests)
# ---------------------------------------------------------------------------

class TestStartSessionSiteAutoStart:
    """start_session() should auto-start wolt sites for non-project sessions."""

    @pytest.fixture(autouse=True)
    def setup_wolt(self, tmp_path, monkeypatch):
        """Create a minimal wolt directory with wolt.json."""
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
        import sessions
        import sites
        import paths

        self.sessions = sessions
        self.sites = sites
        self.wolts_dir = tmp_path

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
        monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

        # Create a rodent wolt
        wolt_dir = tmp_path / "testwolt" / "wolt"
        wolt_dir.mkdir(parents=True)
        site_dir = wolt_dir / "site"
        site_dir.mkdir()
        (site_dir / "index.html").write_text("<h1>test</h1>")
        (wolt_dir / "wolt.json").write_text(json.dumps({
            "name": "testwolt", "type": "raccoon",
        }))

    @patch("sessions.subprocess.run")
    def test_start_session_returns_site_url(self, mock_run, tmp_path):
        """Non-project session should include site_url in result."""
        result = self.sessions.start_session(
            wolt="testwolt",
            prompt="hello",
            routing={"adapter": "telegram", "chat_id": "123"},
        )
        assert result.get("site_url") == "/wolt/testwolt/site/"

    @patch("sessions.subprocess.run")
    def test_start_session_with_app_no_site(self, mock_run, tmp_path):
        """App sessions should NOT get a site viewport."""
        result = self.sessions.start_session(
            wolt="testwolt",
            prompt="hello",
            app="myproject",
            routing={"adapter": "lodge"},
        )
        assert "site_url" not in result

    @patch("sessions.subprocess.run")
    def test_site_started_for_all_adapters(self, mock_run, tmp_path):
        """Site viewport works for lodge, telegram, and slack."""
        for adapter in ["lodge", "telegram", "slack"]:
            result = self.sessions.start_session(
                wolt="testwolt",
                prompt="hello",
                routing={"adapter": adapter},
            )
            assert result.get("site_url") == "/wolt/testwolt/site/", f"failed for {adapter}"

    @patch("sessions.subprocess.run")
    def test_viewport_url_stored_in_session(self, mock_run, tmp_path):
        """start_session() should store viewport URL in the session JSON."""
        result = self.sessions.start_session(
            wolt="testwolt",
            prompt="hello",
            routing={"adapter": "telegram", "chat_id": "123"},
        )
        session_name = result["name"]
        # Viewport URL is now stored in the session JSON itself
        session_file = tmp_path / "testwolt" / ".state" / "sessions" / f"{session_name}.json"
        assert session_file.exists(), f"session file not found at {session_file}"
        data = json.loads(session_file.read_text())
        assert data["viewport_url"] == "/wolt/testwolt/site/"
        assert data["viewport_port"] == 7777

    @patch("sessions.subprocess.run")
    @patch("sessions.ensure_site", side_effect=OSError("disk full"))
    def test_site_failure_does_not_block_session(self, mock_ensure_site, mock_run, tmp_path):
        """If site auto-start fails, session should still be created."""
        result = self.sessions.start_session(
            wolt="testwolt",
            prompt="hello",
            routing={"adapter": "telegram"},
        )
        assert result.get("name")
        assert result.get("wolt") == "testwolt"
        assert "site_url" not in result

    @patch("sessions.subprocess.run")
    def test_prompt_passed_verbatim_to_tmux(self, mock_run, tmp_path):
        """start_session() should pass the prompt to run-session.sh without appending start-chat.

        run-session.sh is responsible for appending /woltspace:start-chat — Python must not duplicate it.
        """
        self.sessions.start_session(
            wolt="testwolt",
            prompt="hello world",
            routing={"adapter": "lodge"},
        )
        # Find the tmux new-session call
        tmux_cmd = mock_run.call_args[0][0]
        # The last arg is the shell command: run-session.sh <name> <dir> <prompt>
        shell_cmd = tmux_cmd[-1]
        # Prompt should appear exactly once, not duplicated or with start-chat appended
        assert "hello world" in shell_cmd
        assert "/woltspace:start-chat" not in shell_cmd


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
