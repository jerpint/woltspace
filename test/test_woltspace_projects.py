"""Tests for the woltspace.json project schema and management.

Tests the WoltspaceProject model, discovery, start/stop, port allocation.
All unit tests — no server required.

Usage:
  uv run --project server --with pytest pytest test/test_woltspace_projects.py -v
"""

import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import projects


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wolts_dir(tmp_path):
    """Create a temporary wolts directory and patch projects.WOLTS_DIR."""
    original = projects.WOLTS_DIR
    original_state = projects._RUNNING_STATE_DIR
    projects.WOLTS_DIR = tmp_path
    projects._RUNNING_STATE_DIR = tmp_path / ".state" / "projects"
    yield tmp_path
    projects.WOLTS_DIR = original
    projects._RUNNING_STATE_DIR = original_state


def _make_project(wolts_dir, keeper, name, **overrides):
    """Create a project directory with woltspace.json."""
    proj_dir = wolts_dir / keeper / "wolt" / "projects" / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "woltspace_version": "0.1",
        "name": name,
        "keeper": keeper,
        **overrides,
    }
    (proj_dir / "woltspace.json").write_text(json.dumps(manifest, indent=2))
    return proj_dir


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestWoltspaceProject:
    def test_minimal_fields(self):
        p = projects.WoltspaceProject(name="test", keeper="neowolt")
        assert p.woltspace_version == "0.1"
        assert p.name == "test"
        assert p.keeper == "neowolt"
        assert p.description is None
        assert p.stack is None
        assert p.install is None
        assert p.start is None
        assert p.source is None
        assert p.emoji in projects.FOREST_EMOJIS

    def test_all_fields(self):
        p = projects.WoltspaceProject(
            name="forj", keeper="neowolt", description="Workout tracker",
            stack="node", install="npm install", start="node server.js",
            source=None, emoji="🦊",
        )
        assert p.description == "Workout tracker"
        assert p.stack == "node"
        assert p.start == "node server.js"
        assert p.emoji == "🦊"

    def test_can_start_with_command(self):
        p = projects.WoltspaceProject(name="a", keeper="k", start="npm start")
        assert p.can_start() is True

    def test_cannot_start_without_command(self):
        p = projects.WoltspaceProject(name="a", keeper="k")
        assert p.can_start() is False

    def test_random_emoji_never_wolt_type(self):
        emojis = {projects.random_emoji() for _ in range(200)}
        assert not (emojis & projects.WOLT_EMOJIS)

    def test_serialization_roundtrip(self):
        p = projects.WoltspaceProject(name="test", keeper="k", emoji="🐸")
        data = json.loads(p.model_dump_json())
        p2 = projects.WoltspaceProject(**data)
        assert p == p2


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discover_empty(self, wolts_dir):
        assert projects.discover_projects() == []

    def test_discover_finds_projects(self, wolts_dir):
        _make_project(wolts_dir, "neowolt", "forj", stack="node")
        _make_project(wolts_dir, "uxwolt", "mockup", stack="html")
        found = projects.discover_projects()
        assert len(found) == 2
        names = {p.name for p in found}
        assert names == {"forj", "mockup"}

    def test_discover_skips_invalid_manifest(self, wolts_dir):
        proj_dir = wolts_dir / "neowolt" / "wolt" / "projects" / "broken"
        proj_dir.mkdir(parents=True)
        (proj_dir / "woltspace.json").write_text("not json")
        assert projects.discover_projects() == []

    def test_discover_skips_missing_manifest(self, wolts_dir):
        proj_dir = wolts_dir / "neowolt" / "wolt" / "projects" / "nofile"
        proj_dir.mkdir(parents=True)
        assert projects.discover_projects() == []

    def test_get_project(self, wolts_dir):
        _make_project(wolts_dir, "neowolt", "forj", description="workout app")
        p = projects.get_project("neowolt", "forj")
        assert p is not None
        assert p.description == "workout app"

    def test_get_project_missing(self, wolts_dir):
        assert projects.get_project("neowolt", "nope") is None

    def test_project_dir(self, wolts_dir):
        d = projects.project_dir("neowolt", "forj")
        assert d == wolts_dir / "neowolt" / "wolt" / "projects" / "forj"


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

class TestPortAllocation:
    def test_first_port(self, wolts_dir):
        port = projects._allocate_port()
        assert port == projects.PORT_MIN

    def test_skips_used_ports(self, wolts_dir):
        # Simulate a running project on PORT_MIN
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "neo--app1.json").write_text(json.dumps({
            "keeper": "neo", "name": "app1", "port": projects.PORT_MIN, "pid": os.getpid(),
        }))
        port = projects._allocate_port()
        assert port == projects.PORT_MIN + 1


# ---------------------------------------------------------------------------
# Running state
# ---------------------------------------------------------------------------

class TestRunningState:
    def test_no_running_projects(self, wolts_dir):
        assert projects.running_projects() == []

    def test_stale_state_cleaned(self, wolts_dir):
        """State files with dead PIDs get cleaned up."""
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "neo--dead.json").write_text(json.dumps({
            "keeper": "neo", "name": "dead", "port": 4001, "pid": 999999,
        }))
        assert projects.running_projects() == []
        assert not (state_dir / "neo--dead.json").exists()


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_missing_project(self, wolts_dir):
        with pytest.raises(ValueError, match="not found"):
            projects.start_project("neowolt", "nonexistent")

    def test_start_no_start_command(self, wolts_dir):
        _make_project(wolts_dir, "neowolt", "static-only")
        with pytest.raises(ValueError, match="no start command"):
            projects.start_project("neowolt", "static-only")

    def test_start_respects_max_running(self, wolts_dir):
        """Can't exceed MAX_RUNNING concurrent projects."""
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        # Fake 2 running projects (using current PID so they appear alive)
        for i in range(projects.MAX_RUNNING):
            (state_dir / f"neo--app{i}.json").write_text(json.dumps({
                "keeper": "neo", "name": f"app{i}", "port": 4001 + i, "pid": os.getpid(),
            }))
        _make_project(wolts_dir, "neowolt", "one-more", start="echo hi")
        with pytest.raises(RuntimeError, match="Max"):
            projects.start_project("neowolt", "one-more")

    @patch("projects.subprocess.Popen")
    def test_start_project_success(self, mock_popen, wolts_dir):
        mock_popen.return_value.pid = 12345
        _make_project(wolts_dir, "neowolt", "runner", start="node server.js")
        state = projects.start_project("neowolt", "runner")
        assert state["port"] == projects.PORT_MIN
        assert state["pid"] == 12345
        assert state["keeper"] == "neowolt"
        assert state["name"] == "runner"
        # Verify Popen was called with correct args
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["env"]["PORT"] == str(projects.PORT_MIN)
        assert call_kwargs["shell"] is True

    @patch("projects.subprocess.Popen")
    def test_start_returns_existing_if_alive(self, mock_popen, wolts_dir):
        """Starting an already-running project returns its existing state."""
        _make_project(wolts_dir, "neowolt", "already", start="echo hi")
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "neowolt--already.json").write_text(json.dumps({
            "keeper": "neowolt", "name": "already", "port": 4005, "pid": os.getpid(),
        }))
        state = projects.start_project("neowolt", "already")
        assert state["port"] == 4005
        mock_popen.assert_not_called()

    def test_stop_not_running(self, wolts_dir):
        assert projects.stop_project("neowolt", "nothing") is False

    @patch("projects.os.killpg")
    @patch("projects.os.getpgid", return_value=999)
    def test_stop_running_project(self, mock_getpgid, mock_killpg, wolts_dir):
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "neowolt--app.json").write_text(json.dumps({
            "keeper": "neowolt", "name": "app", "port": 4001, "pid": os.getpid(),
        }))
        result = projects.stop_project("neowolt", "app")
        assert result is True
        mock_killpg.assert_called_once_with(999, signal.SIGTERM)
        assert not (state_dir / "neowolt--app.json").exists()
