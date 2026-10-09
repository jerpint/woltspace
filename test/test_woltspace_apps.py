"""Tests for the woltspace.json app schema and management.

Tests the WoltspaceApp model, discovery, start/stop, port allocation.
All unit tests — no server required.

Usage:
  uv run --project server --with pytest pytest test/test_woltspace_apps.py -v
"""

import json
import os
import signal
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import apps


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def wolts_dir(tmp_path):
    """Create a temporary wolts directory and patch apps module paths."""
    import paths
    original = apps.WOLTS_DIR
    original_apps = apps.APPS_DIR
    original_legacy = apps.LEGACY_PROJECTS_DIR
    original_state = apps._RUNNING_STATE_DIR
    original_paths = paths.WOLTS_DIR
    apps.WOLTS_DIR = tmp_path
    apps.APPS_DIR = tmp_path / "apps"
    apps.LEGACY_PROJECTS_DIR = tmp_path / "projects"
    apps._RUNNING_STATE_DIR = tmp_path / ".space" / "apps"
    paths.WOLTS_DIR = tmp_path
    yield tmp_path
    apps.WOLTS_DIR = original
    apps.APPS_DIR = original_apps
    apps.LEGACY_PROJECTS_DIR = original_legacy
    apps._RUNNING_STATE_DIR = original_state
    paths.WOLTS_DIR = original_paths


def _make_app(wolts_dir, name, keeper="neowolt", **overrides):
    """Create an app directory with woltspace.json."""
    app_dir = wolts_dir / "apps" / name
    app_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "woltspace_version": "0.1",
        "name": name,
        "keeper": keeper,
        **overrides,
    }
    (app_dir / "woltspace.json").write_text(json.dumps(manifest, indent=2))
    return app_dir


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestWoltspaceApp:
    def test_minimal_fields(self):
        a = apps.WoltspaceApp(name="test", keeper="neowolt", port=4010)
        assert a.woltspace_version == "0.1"
        assert a.name == "test"
        assert a.keeper == "neowolt"
        assert a.port == 4010
        assert a.description is None
        assert a.stack is None
        assert a.install is None
        assert a.start is None
        assert a.source is None
        assert a.emoji in apps.FOREST_EMOJIS

    def test_port_is_required(self):
        with pytest.raises(Exception):
            apps.WoltspaceApp(name="test", keeper="neowolt")

    def test_all_fields(self):
        a = apps.WoltspaceApp(
            name="forj", keeper="neowolt", description="Workout tracker",
            stack="node", install="npm install", start="node server.js",
            port=4020, source=None, emoji="🦊",
        )
        assert a.description == "Workout tracker"
        assert a.stack == "node"
        assert a.start == "node server.js"
        assert a.port == 4020
        assert a.emoji == "🦊"

    def test_can_start_with_command(self):
        a = apps.WoltspaceApp(name="a", keeper="k", port=4010, start="npm start")
        assert a.can_start() is True

    def test_cannot_start_without_command(self):
        a = apps.WoltspaceApp(name="a", keeper="k", port=4010)
        assert a.can_start() is False

    def test_random_emoji_never_wolt_type(self):
        emojis = {apps.random_emoji() for _ in range(200)}
        assert not (emojis & apps.WOLT_EMOJIS)

    def test_serialization_roundtrip(self):
        a = apps.WoltspaceApp(name="test", keeper="k", port=4010, emoji="🐸")
        data = json.loads(a.model_dump_json())
        a2 = apps.WoltspaceApp(**data)
        assert a == a2


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestDiscovery:
    def test_discover_empty(self, wolts_dir):
        assert apps.discover_apps() == []

    def test_discover_finds_apps(self, wolts_dir):
        _make_app(wolts_dir, "forj", keeper="neowolt", stack="node", port=4010)
        _make_app(wolts_dir, "mockup", keeper="uxwolt", stack="html", port=4011)
        found = apps.discover_apps()
        assert len(found) == 2
        names = {a.name for a in found}
        assert names == {"forj", "mockup"}

    def test_discover_skips_invalid_manifest(self, wolts_dir):
        app_dir = wolts_dir / "apps" / "broken"
        app_dir.mkdir(parents=True)
        (app_dir / "woltspace.json").write_text("not json")
        assert apps.discover_apps() == []

    def test_discover_skips_missing_manifest(self, wolts_dir):
        app_dir = wolts_dir / "apps" / "nofile"
        app_dir.mkdir(parents=True)
        assert apps.discover_apps() == []

    def test_get_app(self, wolts_dir):
        _make_app(wolts_dir, "forj", description="workout app", port=4010)
        a = apps.get_app("forj")
        assert a is not None
        assert a.description == "workout app"

    def test_get_app_missing(self, wolts_dir):
        assert apps.get_app("nope") is None

    def test_app_dir(self, wolts_dir):
        d = apps.app_dir("forj")
        assert d == wolts_dir / "apps" / "forj"


# ---------------------------------------------------------------------------
# Running state
# ---------------------------------------------------------------------------

class TestRunningState:
    def test_no_running_apps(self, wolts_dir):
        assert apps.running_apps() == []

    def test_stale_state_not_listed_but_preserved(self, wolts_dir):
        """Under the intent model, dead-PID state files persist (user wants app on)
        but aren't reported as running. Boot-time apps_restore() respawns them.
        """
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "dead.json").write_text(json.dumps({
            "name": "dead", "port": 4001, "pid": 999999,
        }))
        assert apps.running_apps() == []
        assert (state_dir / "dead.json").exists()

    def test_intended_apps_includes_stale(self, wolts_dir):
        """intended_apps() returns both running and stale-intent apps with alive flag."""
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "dead.json").write_text(json.dumps({
            "name": "dead", "port": 4001, "pid": 999999,
        }))
        (state_dir / "alive.json").write_text(json.dumps({
            "name": "alive", "port": 4002, "pid": os.getpid(),
        }))
        result = {s["name"]: s for s in apps.intended_apps()}
        assert result["dead"]["alive"] is False
        assert result["alive"]["alive"] is True


# ---------------------------------------------------------------------------
# Start / Stop
# ---------------------------------------------------------------------------

class TestStartStop:
    def test_start_missing_app(self, wolts_dir):
        with pytest.raises(ValueError, match="not found"):
            apps.start_app("nonexistent")

    def test_start_no_start_command(self, wolts_dir):
        _make_app(wolts_dir, "static-only", port=4010)
        with pytest.raises(ValueError, match="no start command"):
            apps.start_app("static-only")

    @patch("apps.subprocess.Popen")
    def test_start_app_success(self, mock_popen, wolts_dir):
        mock_popen.return_value.pid = 12345
        _make_app(wolts_dir, "runner", start="node server.js", port=4200)
        state = apps.start_app("runner")
        assert state["port"] == 4200
        assert state["pid"] == 12345
        assert state["name"] == "runner"
        assert state["keeper"] == "neowolt"
        # Verify Popen was called with correct args
        call_kwargs = mock_popen.call_args[1]
        assert call_kwargs["env"]["PORT"] == "4200"
        assert call_kwargs["shell"] is True

    @patch("apps.subprocess.Popen")
    def test_start_uses_manifest_port(self, mock_popen, wolts_dir):
        """Port comes from woltspace.json, not dynamic allocation."""
        mock_popen.return_value.pid = 11111
        _make_app(wolts_dir, "fixed-port", start="echo hi", port=4321)
        state = apps.start_app("fixed-port")
        assert state["port"] == 4321

    @patch("apps.subprocess.Popen")
    def test_start_rejects_port_conflict_with_running(self, mock_popen, wolts_dir):
        """Can't start if another running app holds the same port."""
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "occupant.json").write_text(json.dumps({
            "name": "occupant", "port": 4300, "pid": os.getpid(),
        }))
        _make_app(wolts_dir, "conflict", start="echo hi", port=4300)
        with pytest.raises(RuntimeError, match="already in use"):
            apps.start_app("conflict")

    @patch("apps.subprocess.Popen")
    def test_start_returns_existing_if_alive(self, mock_popen, wolts_dir):
        """Starting an already-running app returns its existing state."""
        _make_app(wolts_dir, "already", start="echo hi", port=4005)
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "already.json").write_text(json.dumps({
            "name": "already", "port": 4005, "pid": os.getpid(),
        }))
        state = apps.start_app("already")
        assert state["port"] == 4005
        mock_popen.assert_not_called()

    def test_stop_not_running(self, wolts_dir):
        assert apps.stop_app("nothing") is False

    @patch("apps.os.killpg")
    @patch("apps.os.getpgid", return_value=999)
    def test_stop_running_app(self, mock_getpgid, mock_killpg, wolts_dir):
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "app.json").write_text(json.dumps({
            "name": "app", "port": 4001, "pid": os.getpid(),
        }))
        result = apps.stop_app("app")
        assert result is True
        mock_killpg.assert_called_once_with(999, signal.SIGTERM)
        assert not (state_dir / "app.json").exists()


# ---------------------------------------------------------------------------
# apps_restore (boot-time autorestore)
# ---------------------------------------------------------------------------

class TestAppsRestore:
    def test_restore_empty(self, wolts_dir):
        """No state files — nothing to restore."""
        assert apps.apps_restore() == []

    def test_restore_survived_app(self, wolts_dir):
        """App whose PID is still alive is left alone (no respawn)."""
        _make_app(wolts_dir, "surviving", start="echo hi", port=4100)
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "surviving.json").write_text(json.dumps({
            "name": "surviving", "port": 4100, "pid": os.getpid(),
        }))
        with patch("apps.subprocess.Popen") as mock_popen:
            actions = apps.apps_restore()
        mock_popen.assert_not_called()
        assert actions == [{"name": "surviving", "action": "survived", "pid": os.getpid()}]

    @patch("apps.subprocess.Popen")
    def test_restore_respawns_dead_app(self, mock_popen, wolts_dir):
        """App with dead PID gets respawned via start_app()."""
        mock_popen.return_value.pid = 22222
        _make_app(wolts_dir, "crashed", start="node server.js", port=4101)
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "crashed.json").write_text(json.dumps({
            "name": "crashed", "port": 4101, "pid": 999999,  # dead
        }))
        actions = apps.apps_restore()
        mock_popen.assert_called_once()
        assert len(actions) == 1
        assert actions[0]["name"] == "crashed"
        assert actions[0]["action"] == "restored"
        assert actions[0]["pid"] == 22222

    def test_restore_cleans_orphan_state(self, wolts_dir):
        """State file for an app whose manifest was deleted gets removed."""
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        orphan = state_dir / "ghost.json"
        orphan.write_text(json.dumps({"name": "ghost", "port": 4102, "pid": 999999}))
        actions = apps.apps_restore()
        assert not orphan.exists()
        assert actions == [{"name": "ghost", "action": "orphan-cleaned"}]

    @patch("apps.subprocess.Popen")
    def test_restore_records_failure(self, mock_popen, wolts_dir):
        """If respawn fails, record the error and keep going."""
        mock_popen.side_effect = RuntimeError("boom")
        _make_app(wolts_dir, "cursed", start="node server.js", port=4103)
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "cursed.json").write_text(json.dumps({
            "name": "cursed", "port": 4103, "pid": 999999,
        }))
        actions = apps.apps_restore()
        assert actions[0]["action"] == "restore-failed"
        assert "boom" in actions[0]["error"]


# ---------------------------------------------------------------------------
# apps_autostart (manifest-driven boot launch)
# ---------------------------------------------------------------------------

class TestAppsAutostart:
    def test_autostart_defaults_false(self):
        a = apps.WoltspaceApp(name="t", keeper="neowolt", port=4010)
        assert a.autostart is False

    def test_autostart_parsed_from_manifest(self, wolts_dir):
        _make_app(wolts_dir, "boot-me", start="node s.js", port=4200, autostart=True)
        app = apps.get_app("boot-me")
        assert app.autostart is True

    def test_autostart_empty(self, wolts_dir):
        """No autostart apps — nothing to do."""
        assert apps.apps_autostart() == []

    def test_autostart_skips_when_flag_false(self, wolts_dir):
        _make_app(wolts_dir, "off", start="node s.js", port=4201, autostart=False)
        with patch("apps.subprocess.Popen") as mock_popen:
            actions = apps.apps_autostart()
        mock_popen.assert_not_called()
        assert actions == []

    @patch("apps.subprocess.Popen")
    def test_autostart_starts_when_flag_true(self, mock_popen, wolts_dir):
        mock_popen.return_value.pid = 33333
        _make_app(wolts_dir, "on", start="node s.js", port=4202, autostart=True)
        actions = apps.apps_autostart()
        mock_popen.assert_called_once()
        assert actions[0]["name"] == "on"
        assert actions[0]["action"] == "autostarted"
        assert actions[0]["pid"] == 33333

    @patch("apps.subprocess.Popen")
    def test_autostart_idempotent_when_already_running(self, mock_popen, wolts_dir):
        """If the app is already running (state file + live PID), start_app returns
        existing state and no new process is spawned."""
        _make_app(wolts_dir, "already", start="node s.js", port=4203, autostart=True)
        state_dir = apps._RUNNING_STATE_DIR
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "already.json").write_text(json.dumps({
            "name": "already", "port": 4203, "pid": os.getpid(),
        }))
        actions = apps.apps_autostart()
        mock_popen.assert_not_called()
        assert actions[0]["name"] == "already"
        assert actions[0]["action"] == "autostarted"
        assert actions[0]["pid"] == os.getpid()

    def test_autostart_skips_app_with_no_start_command(self, wolts_dir):
        """An app flagged autostart but with no start command is silently skipped."""
        _make_app(wolts_dir, "static", port=4204, autostart=True)  # no start
        with patch("apps.subprocess.Popen") as mock_popen:
            actions = apps.apps_autostart()
        mock_popen.assert_not_called()
        assert actions == []

    @patch("apps.subprocess.Popen")
    def test_autostart_records_failure(self, mock_popen, wolts_dir):
        mock_popen.side_effect = RuntimeError("kaboom")
        _make_app(wolts_dir, "doomed", start="node s.js", port=4205, autostart=True)
        actions = apps.apps_autostart()
        assert actions[0]["action"] == "autostart-failed"
        assert "kaboom" in actions[0]["error"]
