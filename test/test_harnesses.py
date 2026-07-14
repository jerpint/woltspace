"""Harness table tests — build_command spellings and harness field plumbing.

Usage: uv run --extra test pytest test/test_harnesses.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from harnesses import (
    DEFAULT_HARNESS,
    HARNESSES,
    build_command,
    creature_model,
    resolve_harness,
)


class TestResolveHarness:
    def test_empty_falls_back_to_default(self):
        assert resolve_harness("") == "claude"
        assert resolve_harness(None) == "claude"

    def test_unknown_falls_back_to_default(self):
        assert resolve_harness("winamp") == "claude"

    def test_known_passes_through(self):
        assert resolve_harness("claude") == "claude"


class TestCreatureModel:
    def test_claude_tiers(self):
        assert creature_model("claude", "raccoon") == "opus"
        assert creature_model("claude", "beaver") == "sonnet"
        assert creature_model("claude", "otter") == "haiku"
        assert creature_model("claude", "wolf") == "sonnet"

    def test_no_creature_is_none(self):
        assert creature_model("claude", "") is None
        assert creature_model("claude", None) is None

    def test_unknown_creature_is_none(self):
        assert creature_model("claude", "capybara") is None


class TestBuildCommandClaude:
    """The generated commands must match the historical wclaude invocations."""

    def test_spawn(self):
        cmd = build_command(
            "claude", "spawn",
            session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            session_name="testwolt-chompy-dam-abc123",
            model="opus",
            prompt="hey testwolt",
        )
        assert "wclaude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--session-id a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "--name testwolt-chompy-dam-abc123" in cmd
        assert "--model opus" in cmd
        assert cmd.endswith("'hey testwolt'")

    def test_resume(self):
        cmd = build_command(
            "claude", "resume",
            resume_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            model="opus",
            prompt="continue",
        )
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "--model opus" in cmd
        assert "--session-id" not in cmd

    def test_resume_without_id_omits_flag(self):
        cmd = build_command("claude", "resume", model="opus")
        assert "--resume" not in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_login(self):
        cmd = build_command("claude", "login")
        assert cmd.endswith("wclaude /login")
        assert "--dangerously-skip-permissions" not in cmd

    def test_prompt_is_shell_quoted(self):
        cmd = build_command("claude", "spawn", prompt='say "hi"; rm -rf /')
        assert "'say \"hi\"; rm -rf /'" in cmd

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unknown mode"):
            build_command("claude", "teleport")

    def test_unknown_harness_uses_default(self):
        assert build_command("winamp", "login") == build_command("claude", "login")


class TestTableShape:
    """Every harness entry must carry the keys the platform relies on."""

    REQUIRED_KEYS = {"wrapper", "command", "process_names", "models",
                     "skill_invoke", "instructions_file", "auth_file"}

    def test_all_entries_complete(self):
        for name, entry in HARNESSES.items():
            missing = self.REQUIRED_KEYS - set(entry)
            assert not missing, f"harness '{name}' missing keys: {missing}"

    def test_default_exists(self):
        assert DEFAULT_HARNESS in HARNESSES


class TestSessionHarnessPlumbing:
    """harness flows: param > wolt.json default > platform default; stored for life."""

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
        self.wolt_json = wolt_dir / "wolt.json"

    def _start(self, **kwargs):
        from sessions import start_session
        with patch("sessions.subprocess.run"), patch("sites.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            return start_session(wolt="testwolt", prompt="hello", **kwargs)

    def _session_data(self, name):
        from sessions import SessionRegistry
        return SessionRegistry(self.wolts_dir).get(name, check_alive=False)

    def test_default_harness_is_claude(self):
        result = self._start()
        assert result["harness"] == "claude"
        assert self._session_data(result["name"])["harness"] == "claude"

    def test_wolt_json_default_applies(self):
        self.wolt_json.write_text(json.dumps({
            "name": "testwolt", "type": "raccoon", "harness": "claude",
        }))
        result = self._start()
        assert result["harness"] == "claude"

    def test_unknown_harness_falls_back(self):
        result = self._start(harness="winamp")
        assert result["harness"] == "claude"

    def test_old_sessions_without_harness_resolve_to_claude(self):
        """Sessions created before the harness field must resume on claude."""
        from sessions import SessionRegistry
        reg = SessionRegistry(self.wolts_dir)
        data = reg.create("testwolt-old-dam-abc123", wolt="testwolt")
        # Simulate a pre-harness session file
        del data["harness"]
        reg._write("testwolt", "testwolt-old-dam-abc123", data)
        reg.update("testwolt-old-dam-abc123", wolt="testwolt",
                   claude_session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        from sessions import prepare_session_command, resume_session
        with patch("sessions.subprocess.run") as mock_run, \
             patch("sessions._tmux_alive", return_value=False):
            result = resume_session("testwolt-old-dam-abc123", "hello")
        assert result["status"] == "respawned"
        cmd_str = str([c for c in mock_run.call_args_list if "new-session" in str(c)][0])
        assert "--resume" in cmd_str  # wrapper delivered in resume mode
        # The agent-level command falls back to the legacy claude_session_id
        cmd = prepare_session_command("testwolt-old-dam-abc123", "resume", "hello")
        assert "wclaude" in cmd
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
