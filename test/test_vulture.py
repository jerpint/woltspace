"""Vulture session reaper tests — pure Python, no server or tmux required.

Tests the reaping logic in container/creatures/vulture.py with mocked
tmux and filesystem operations.

Now uses the per-wolt session model: sessions live at
wolts/{wolt}/.state/sessions/{name}.json, and the vulture iterates all
wolt directories instead of using reg.dir.

Usage: uv run pytest test/test_vulture.py -v
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


@pytest.fixture(autouse=True)
def _idle_closing_off():
    """These tests cover reaping; idle closing (on by default, 24h) has its own tests."""
    with patch("creatures.vulture.get_idle_timeout", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _vulture_state_in_tmp(tmp_path, monkeypatch):
    """Every vulture write lands in tmp_path, never the developer's live lodge.

    The module computes its log and state paths from WOLTSPACE_WOLTS_DIR at
    import, so a test run from a wolt session used to append to the real log.
    """
    from creatures import vulture
    state = tmp_path / ".vulture-state"
    monkeypatch.setattr(vulture, "STATE_DIR", state)
    monkeypatch.setattr(vulture, "LOG_FILE", state / "vulture.log")
    monkeypatch.setattr(vulture, "LAST_RUN_FILE", state / "last-run")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wolt_session(wolts_dir: Path, wolt: str, name: str,
                       status: str = "running", created_at: int = None,
                       finished_at: int = None) -> dict:
    """Write a session JSON file to the wolt's .state/sessions/ dir."""
    now = int(time.time())
    sessions_dir = wolts_dir / wolt / ".state" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "wolt": wolt,
        "creature": "beaver",
        "status": status,
        "created_at": created_at or (now - 600),  # 10 min ago by default
        "finished_at": finished_at,
        "exit_code": None,
        "last_activity": now - 300,
    }
    (sessions_dir / f"{name}.json").write_text(json.dumps(data))
    return data


def _read_wolt_session(wolts_dir: Path, wolt: str, name: str) -> dict:
    return json.loads(
        (wolts_dir / wolt / ".state" / "sessions" / f"{name}.json").read_text()
    )


# ---------------------------------------------------------------------------
# Registry reaping (Pass 1)
# ---------------------------------------------------------------------------

class TestRegistryReaping:
    """Pass 1: marking dead registry entries as reaped."""

    def test_reaps_running_session_with_dead_tmux(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "dead-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "dead-session" in stats["registry_reaped"]
        data = _read_wolt_session(tmp_path, "testwolt", "dead-session")
        assert data["status"] == "reaped"

    def test_a_dead_session_that_can_resume_rests_instead(self, tmp_path):
        """A reaped session refuses messages; a resting one wakes on them."""
        from creatures.vulture import reap

        data = _make_wolt_session(tmp_path, "testwolt", "can-resume", status="running")
        data["harness_session_id"] = "abc"
        (tmp_path / "testwolt" / ".state" / "sessions" / "can-resume.json").write_text(json.dumps(data))

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            stats = reap()

        assert "can-resume" in stats["registry_reaped"]
        after = _read_wolt_session(tmp_path, "testwolt", "can-resume")
        assert after["status"] == "resting"
        assert after["rested_at"]

    def test_skips_already_completed_sessions(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "done-session", status="completed")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []

    def test_skips_sessions_still_alive_in_tmux(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "alive-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"alive-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=True), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []
        assert stats["tmux_killed"] == []

    def test_reaps_across_multiple_wolts(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "woltA", "sess-a", status="running")
        _make_wolt_session(tmp_path, "woltB", "sess-b", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "sess-a" in stats["registry_reaped"]
        assert "sess-b" in stats["registry_reaped"]


# ---------------------------------------------------------------------------
# Grace period
# ---------------------------------------------------------------------------

class TestGracePeriod:
    """Sessions younger than GRACE_PERIOD_SECONDS are never reaped."""

    def test_skips_fresh_sessions(self, tmp_path):
        from creatures.vulture import reap

        # Created 30 seconds ago — within grace period
        _make_wolt_session(tmp_path, "testwolt", "fresh-session",
                          status="running", created_at=int(time.time()) - 30)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []


# ---------------------------------------------------------------------------
# Protected sessions
# ---------------------------------------------------------------------------

class TestProtectedSessions:
    """The 'main' tmux session is never killed."""

    def test_main_session_never_killed(self, tmp_path):
        from creatures.vulture import reap

        # Create at least one wolt dir so iteration works
        (tmp_path / "testwolt" / ".state" / "sessions").mkdir(parents=True)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"main"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        mock_kill.assert_not_called()
        assert stats["tmux_killed"] == []


# ---------------------------------------------------------------------------
# Zombie tmux cleanup (Pass 2)
# ---------------------------------------------------------------------------

class TestZombieTmuxCleanup:
    """Pass 2: killing tmux sessions with no claude process inside."""

    def test_kills_zombie_tmux_with_running_registry(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "zombie-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"zombie-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "zombie-session" in stats["tmux_killed"]
        mock_kill.assert_called_once_with("zombie-session")
        data = _read_wolt_session(tmp_path, "testwolt", "zombie-session")
        assert data["status"] == "reaped"

    def test_kills_tmux_for_already_reaped_registry(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "leftover-session", status="reaped")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"leftover-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "leftover-session" in stats["tmux_killed"]

    def test_never_touches_a_tmux_session_the_lodge_did_not_register(self, tmp_path):
        """Natively the lodge shares the owner's tmux server: these are their terminals."""
        from creatures.vulture import reap

        (tmp_path / "testwolt" / ".state" / "sessions").mkdir(parents=True)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"145", "my-shell"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["tmux_killed"] == []
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    """Dry run mode identifies but doesn't mutate anything."""

    def test_dry_run_does_not_write(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "victim-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap(dry_run=True)

        assert "victim-session" in stats["registry_reaped"]
        mock_kill.assert_not_called()
        # Original file should still say "running"
        data = _read_wolt_session(tmp_path, "testwolt", "victim-session")
        assert data["status"] == "running"


def test_idle_closing_skips_sessions_that_cannot_resume(tmp_path):
    """A codex session with no stamped conversation id must never be rested: it could not come back."""
    import creatures.vulture as vulture
    reg = MagicMock()
    old = int(time.time()) - 10 * 86400
    reg.list.return_value = [
        {"name": "no-id", "wolt": "w", "status": "running", "created_at": old},
        {"name": "has-id", "wolt": "w", "status": "running", "created_at": old, "harness_session_id": "abc"},
    ]
    runtime = MagicMock()
    runtime.last_keys.return_value = {"no-id": old, "has-id": old}
    with patch.object(vulture, "get_runtime", return_value=runtime), \
         patch.object(vulture, "_rest_via_api", return_value=True) as rest:
        rested = vulture._rest_idle_sessions(reg, {"no-id", "has-id"}, int(time.time()), 86400, dry_run=False)
    assert rested == ["has-id"]
    rest.assert_called_once()
