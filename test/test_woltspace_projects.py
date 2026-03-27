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
    """Create a temporary wolts directory and patch projects module paths."""
    import paths
    original = projects.WOLTS_DIR
    original_projects = projects.PROJECTS_DIR
    original_state = projects._RUNNING_STATE_DIR
    original_paths = paths.WOLTS_DIR
    projects.WOLTS_DIR = tmp_path
    projects.PROJECTS_DIR = tmp_path / "projects"
    projects._RUNNING_STATE_DIR = tmp_path / ".space" / "projects"
    paths.WOLTS_DIR = tmp_path
    yield tmp_path
    projects.WOLTS_DIR = original
    projects.PROJECTS_DIR = original_projects
    projects._RUNNING_STATE_DIR = original_state
    paths.WOLTS_DIR = original_paths


_next_port = 4010

def _make_project(wolts_dir, name, keeper="neowolt", **overrides):
    """Create a project directory with woltspace.json."""
    global _next_port
    proj_dir = wolts_dir / "projects" / name
    proj_dir.mkdir(parents=True, exist_ok=True)
    if "port" not in overrides:
        overrides["port"] = _next_port
        _next_port += 1
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
        p = projects.WoltspaceProject(name="test", keeper="neowolt", port=4010)
        assert p.woltspace_version == "0.1"
        assert p.name == "test"
        assert p.keeper == "neowolt"
        assert p.port == 4010
        assert p.description is None
        assert p.stack is None
        assert p.install is None
        assert p.start is None
        assert p.source is None
        assert p.emoji in projects.FOREST_EMOJIS

    def test_port_is_required(self):
        with pytest.raises(Exception):
            projects.WoltspaceProject(name="test", keeper="neowolt")

    def test_all_fields(self):
        p = projects.WoltspaceProject(
            name="forj", keeper="neowolt", description="Workout tracker",
            stack="node", install="npm install", start="node server.js",
            port=4020, source=None, emoji="🦊",
        )
        assert p.description == "Workout tracker"
        assert p.stack == "node"
        assert p.start == "node server.js"
        assert p.port == 4020
        assert p.emoji == "🦊"

    def test_can_start_with_command(self):
        p = projects.WoltspaceProject(name="a", keeper="k", port=4010, start="npm start")
        assert p.can_start() is True

    def test_cannot_start_without_command(self):
        p = projects.WoltspaceProject(name="a", keeper="k", port=4010)
        assert p.can_start() is False

    def test_random_emoji_never_wolt_type(self):
        emojis = {projects.random_emoji() for _ in range(200)}
        assert not (emojis & projects.WOLT_EMOJIS)

    def test_serialization_roundtrip(self):
        p = projects.WoltspaceProject(name="test", keeper="k", port=4010, emoji="🐸")
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
        _make_project(wolts_dir, "forj", keeper="neowolt", stack="node")
        _make_project(wolts_dir, "mockup", keeper="uxwolt", stack="html")
        found = projects.discover_projects()
        assert len(found) == 2
        names = {p.name for p in found}
        assert names == {"forj", "mockup"}

    def test_discover_skips_invalid_manifest(self, wolts_dir):
        proj_dir = wolts_dir / "projects" / "broken"
        proj_dir.mkdir(parents=True)
        (proj_dir / "woltspace.json").write_text("not json")
        assert projects.discover_projects() == []

    def test_discover_skips_missing_manifest(self, wolts_dir):
        proj_dir = wolts_dir / "projects" / "nofile"
        proj_dir.mkdir(parents=True)
        assert projects.discover_projects() == []

    def test_get_project(self, wolts_dir):
        _make_project(wolts_dir, "forj", description="workout app")
        p = projects.get_project("forj")
        assert p is not None
        assert p.description == "workout app"

    def test_get_project_missing(self, wolts_dir):
        assert projects.get_project("nope") is None

    def test_project_dir(self, wolts_dir):
        d = projects.project_dir("forj")
        assert d == wolts_dir / "projects" / "forj"


# ---------------------------------------------------------------------------
# Port validation
# ---------------------------------------------------------------------------

class TestPortValidation:
    def test_valid_port(self):
        projects.validate_port(4010)  # should not raise

    def test_port_below_range(self):
        with pytest.raises(ValueError, match="out of range"):
            projects.validate_port(3000)

    def test_port_above_range(self):
        with pytest.raises(ValueError, match="out of range"):
            projects.validate_port(5000)

    def test_reserved_port(self):
        with pytest.raises(ValueError, match="reserved"):
            projects.validate_port(7777)

    def test_port_conflict_detection(self, wolts_dir):
        _make_project(wolts_dir, "app-a", port=4050)
        conflict = projects.check_port_conflict(4050)
        assert conflict == "app-a"

    def test_no_port_conflict(self, wolts_dir):
        _make_project(wolts_dir, "app-b", port=4060)
        assert projects.check_port_conflict(4061) is None

    def test_port_conflict_excludes_self(self, wolts_dir):
        _make_project(wolts_dir, "app-c", port=4070)
        assert projects.check_port_conflict(4070, exclude_name="app-c") is None


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
        (state_dir / "dead.json").write_text(json.dumps({
            "name": "dead", "port": 4001, "pid": 999999,
        }))
        assert projects.running_projects() == []
        assert not (state_dir / "dead.json").exists()


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_missing_project(self, wolts_dir):
        with pytest.raises(ValueError, match="not found"):
            projects.start_project("nonexistent")

    def test_start_no_start_command(self, wolts_dir):
        _make_project(wolts_dir, "static-only")
        with pytest.raises(ValueError, match="no start command"):
            projects.start_project("static-only")

    def test_start_respects_max_running(self, wolts_dir):
        """Can't exceed MAX_RUNNING concurrent projects."""
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        # Fake 2 running projects (using current PID so they appear alive)
        for i in range(projects.MAX_RUNNING):
            (state_dir / f"app{i}.json").write_text(json.dumps({
                "name": f"app{i}", "port": 4001 + i, "pid": os.getpid(),
            }))
        _make_project(wolts_dir, "one-more", start="echo hi", port=4100)
        with pytest.raises(RuntimeError, match="Max"):
            projects.start_project("one-more")

    @patch("projects.subprocess.Popen")
    def test_start_project_success(self, mock_popen, wolts_dir):
        mock_popen.return_value.pid = 12345
        _make_project(wolts_dir, "runner", start="node server.js", port=4200)
        state = projects.start_project("runner")
        assert state["port"] == 4200
        assert state["pid"] == 12345
        assert state["name"] == "runner"
        assert state["keeper"] == "neowolt"
        # Verify Popen was called with correct args
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["env"]["PORT"] == "4200"
        assert call_kwargs["shell"] is True

    @patch("projects.subprocess.Popen")
    def test_start_uses_manifest_port(self, mock_popen, wolts_dir):
        """Port comes from woltspace.json, not dynamic allocation."""
        mock_popen.return_value.pid = 11111
        _make_project(wolts_dir, "fixed-port", start="echo hi", port=4321)
        state = projects.start_project("fixed-port")
        assert state["port"] == 4321

    @patch("projects.subprocess.Popen")
    def test_start_rejects_port_conflict_with_running(self, mock_popen, wolts_dir):
        """Can't start if another running project holds the same port."""
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "occupant.json").write_text(json.dumps({
            "name": "occupant", "port": 4300, "pid": os.getpid(),
        }))
        _make_project(wolts_dir, "conflict", start="echo hi", port=4300)
        with pytest.raises(RuntimeError, match="already in use"):
            projects.start_project("conflict")

    @patch("projects.subprocess.Popen")
    def test_start_returns_existing_if_alive(self, mock_popen, wolts_dir):
        """Starting an already-running project returns its existing state."""
        _make_project(wolts_dir, "already", start="echo hi", port=4005)
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "already.json").write_text(json.dumps({
            "name": "already", "port": 4005, "pid": os.getpid(),
        }))
        state = projects.start_project("already")
        assert state["port"] == 4005
        mock_popen.assert_not_called()

    def test_stop_not_running(self, wolts_dir):
        assert projects.stop_project("nothing") is False

    @patch("projects.os.killpg")
    @patch("projects.os.getpgid", return_value=999)
    def test_stop_running_project(self, mock_getpgid, mock_killpg, wolts_dir):
        state_dir = projects._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "app.json").write_text(json.dumps({
            "name": "app", "port": 4001, "pid": os.getpid(),
        }))
        result = projects.stop_project("app")
        assert result is True
        mock_killpg.assert_called_once_with(999, signal.SIGTERM)
        assert not (state_dir / "app.json").exists()
