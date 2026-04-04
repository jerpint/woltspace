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
             patch("creatures.vulture._session_has_claude", return_value=True), \
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
             patch("creatures.vulture._session_has_claude", return_value=False), \
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
             patch("creatures.vulture._session_has_claude", return_value=False), \
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
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "leftover-session" in stats["tmux_killed"]

    def test_kills_unregistered_tmux_session(self, tmp_path):
        from creatures.vulture import reap

        # Create a wolt dir but no session for ghost-session
        (tmp_path / "testwolt" / ".state" / "sessions").mkdir(parents=True)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"ghost-session"}), \
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "ghost-session" in stats["tmux_killed"]
        mock_kill.assert_called_once_with("ghost-session")


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
