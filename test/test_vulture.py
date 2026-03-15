"""Vulture session reaper tests — pure Python, no server or tmux required.

Tests the reaping logic in container/creatures/vulture.py with mocked
tmux and filesystem operations.

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

def _make_session(registry_dir: Path, name: str, status: str = "running",
                  created_at: int = None, finished_at: int = None) -> dict:
    """Write a session JSON file to the registry dir."""
    now = int(time.time())
    data = {
        "name": name,
        "wolt": "testwolt",
        "creature": "beaver",
        "status": status,
        "created_at": created_at or (now - 600),  # 10 min ago by default
        "finished_at": finished_at,
        "exit_code": None,
        "last_activity": now - 300,
    }
    (registry_dir / f"{name}.json").write_text(json.dumps(data))
    return data


def _read_session(registry_dir: Path, name: str) -> dict:
    return json.loads((registry_dir / f"{name}.json").read_text())


# ---------------------------------------------------------------------------
# Registry reaping
# ---------------------------------------------------------------------------

class TestRegistryReaping:
    """Pass 1: marking dead registry entries as reaped."""

    def test_reaps_running_session_with_dead_tmux(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "dead-session", status="running")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = {
                "name": "dead-session", "status": "running",
                "created_at": int(time.time()) - 600, "finished_at": None,
            }
            MockReg.return_value = mock_reg

            stats = reap()

        assert "dead-session" in stats["registry_reaped"]

    def test_skips_already_completed_sessions(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "done-session", status="completed")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            MockReg.return_value = mock_reg

            stats = reap()

        assert stats["registry_reaped"] == []

    def test_skips_sessions_still_alive_in_tmux(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "alive-session", status="running")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value={"alive-session"}), \
             patch("creatures.vulture._session_has_claude", return_value=True), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = {
                "name": "alive-session", "status": "running",
                "created_at": int(time.time()) - 600,
            }
            MockReg.return_value = mock_reg

            stats = reap()

        assert stats["registry_reaped"] == []
        assert stats["tmux_killed"] == []


# ---------------------------------------------------------------------------
# Grace period
# ---------------------------------------------------------------------------

class TestGracePeriod:
    """Sessions younger than GRACE_PERIOD_SECONDS are never reaped."""

    def test_skips_fresh_sessions(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        # Created 30 seconds ago — within grace period
        _make_session(reg_dir, "fresh-session", status="running",
                      created_at=int(time.time()) - 30)

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            MockReg.return_value = mock_reg

            stats = reap()

        assert stats["registry_reaped"] == []


# ---------------------------------------------------------------------------
# Protected sessions
# ---------------------------------------------------------------------------

class TestProtectedSessions:
    """The 'main' tmux session is never killed."""

    def test_main_session_never_killed(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        # Empty registry dir — glob will return nothing naturally

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value={"main"}), \
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = None
            MockReg.return_value = mock_reg

            stats = reap()

        mock_kill.assert_not_called()
        assert stats["tmux_killed"] == []


# ---------------------------------------------------------------------------
# Zombie tmux cleanup
# ---------------------------------------------------------------------------

class TestZombieTmuxCleanup:
    """Pass 2: killing tmux sessions with no claude process inside."""

    def test_kills_zombie_tmux_with_running_registry(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "zombie-session", status="running")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value={"zombie-session"}), \
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = {
                "name": "zombie-session", "status": "running",
                "created_at": int(time.time()) - 600,
            }
            MockReg.return_value = mock_reg

            stats = reap()

        assert "zombie-session" in stats["tmux_killed"]
        mock_kill.assert_called_once_with("zombie-session")

    def test_kills_tmux_for_already_reaped_registry(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "leftover-session", status="reaped")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value={"leftover-session"}), \
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = {
                "name": "leftover-session", "status": "reaped",
                "created_at": int(time.time()) - 600,
            }
            MockReg.return_value = mock_reg

            stats = reap()

        assert "leftover-session" in stats["tmux_killed"]

    def test_kills_unregistered_tmux_session(self, tmp_path):
        from creatures.vulture import reap

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        # Empty registry dir — no json files, so ghost-session is unregistered

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value={"ghost-session"}), \
             patch("creatures.vulture._session_has_claude", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            mock_reg._read.return_value = None
            MockReg.return_value = mock_reg

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

        reg_dir = tmp_path / ".state" / "registry"
        reg_dir.mkdir(parents=True)
        _make_session(reg_dir, "victim-session", status="running")

        with patch("creatures.vulture.SessionRegistry") as MockReg, \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".state" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            mock_reg = MagicMock()
            mock_reg.dir = reg_dir
            MockReg.return_value = mock_reg

            stats = reap(dry_run=True)

        assert "victim-session" in stats["registry_reaped"]
        mock_reg._write.assert_not_called()
        mock_kill.assert_not_called()
