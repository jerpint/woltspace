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

    def test_discover_reports_invalid_manifest_reason(self, wolts_dir):
        app_dir = wolts_dir / "apps" / "broken"
        app_dir.mkdir(parents=True)
        (app_dir / "woltspace.json").write_text(json.dumps({
            "name": "broken",
            "port": 4010,
        }))

        valid, invalid = apps.discover_apps_with_errors()

        assert valid == []
        assert invalid == [{
            "name": "broken",
            "error": "keeper: Field required",
        }]

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
    def test_start_refuses_the_lodge_port(self, mock_popen, wolts_dir, monkeypatch):
        monkeypatch.delenv("WOLTSPACE_PORT", raising=False)
        monkeypatch.delenv("PORT", raising=False)
        _make_app(wolts_dir, "squatter", start="echo hi", port=7777)
        with pytest.raises(RuntimeError, match="lodge's own port"):
            apps.start_app("squatter")
        mock_popen.assert_not_called()

    @patch("apps.subprocess.Popen")
    def test_starting_a_dead_app_closes_its_recorded_tunnel(self, mock_popen, wolts_dir, monkeypatch):
        """A direct Start, not only boot restore, must not forget the old tunnel."""
        mock_popen.return_value.pid = 33333
        stopped = []
        monkeypatch.setattr(apps, "stop_cloudflared", lambda pid: stopped.append(pid) or True)
        _make_app(wolts_dir, "revived", start="echo hi", port=4334)
        apps._write_state("revived", {
            "name": "revived", "port": 4334, "pid": 999999,  # dead
            "tunnel_pid": 6666, "tunnel_url": "https://old.trycloudflare.com",
        })
        state = apps.start_app("revived")
        assert stopped == [6666]
        assert state["pid"] == 33333 and "tunnel_pid" not in state

    @patch("apps.subprocess.Popen")
    def test_a_dead_app_with_a_surviving_tunnel_does_not_start(self, mock_popen, wolts_dir, monkeypatch):
        monkeypatch.setattr(apps, "stop_cloudflared", lambda pid: False)
        monkeypatch.setattr(apps, "is_cloudflared", lambda pid: True)
        _make_app(wolts_dir, "stuck", start="echo hi", port=4335)
        apps._write_state("stuck", {
            "name": "stuck", "port": 4335, "pid": 999999,
            "tunnel_pid": 7777777, "tunnel_url": "https://old.trycloudflare.com",
        })
        with pytest.raises(RuntimeError, match="could not stop the old quick tunnel"):
            apps.start_app("stuck")
        mock_popen.assert_not_called()
        assert apps._read_state("stuck")["tunnel_pid"] == 7777777

    @patch("apps.subprocess.Popen")
    def test_a_public_manifest_never_opens_a_tunnel(self, mock_popen, wolts_dir, monkeypatch):
        """A manifest is wolt-editable, so "public": true must publish nothing."""
        mock_popen.return_value.pid = 22222
        monkeypatch.setenv("WOLTSPACE_APP_QUICK_TUNNELS", "1")
        monkeypatch.setattr(
            apps, "start_cloudflared",
            lambda **_kw: pytest.fail("a manifest opened a tunnel"),
        )
        _make_app(wolts_dir, "loud", start="echo hi", port=4333)
        manifest = wolts_dir / "apps" / "loud" / "woltspace.json"
        data = json.loads(manifest.read_text())
        data["public"] = True
        manifest.write_text(json.dumps(data))
        state = apps.start_app("loud")
        assert "tunnel_url" not in state

    @patch("apps.subprocess.Popen")
    def test_start_app_withholds_lodge_service_credentials(
        self, mock_popen, wolts_dir, monkeypatch,
    ):
        """Chat, GitHub-bot and Cloudflare credentials stay with the lodge."""
        for key in (
            "TELEGRAM_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_BOT_TOKEN",
            "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_INSTALLATION_ID",
            "CLOUDFLARE_TUNNEL_TOKEN", "CLOUDFLARE_API_TOKEN",
        ):
            monkeypatch.setenv(key, "secret-" + key.lower())
        monkeypatch.setenv("OPENROUTER_API_KEY", "kept")
        monkeypatch.setenv("APP_SETTING", "kept")
        mock_popen.return_value.pid = 12346
        _make_app(wolts_dir, "quiet", start="node server.js", port=4201)

        apps.start_app("quiet")

        env = mock_popen.call_args[1]["env"]
        leaked = [k for k in env if k.startswith(apps.LODGE_ONLY_ENV_PREFIXES)]
        assert leaked == []
        assert not any(v.startswith("secret-") for v in env.values())
        assert env["OPENROUTER_API_KEY"] == "kept"
        assert env["APP_SETTING"] == "kept"
        assert env["PORT"] == "4201"

    @patch("apps.subprocess.run")
    @patch("apps.subprocess.Popen")
    def test_app_install_withholds_lodge_service_credentials(
        self, mock_popen, mock_run, wolts_dir, monkeypatch,
    ):
        monkeypatch.setenv("SLACK_APP_TOKEN", "secret")
        mock_popen.return_value.pid = 12347
        _make_app(
            wolts_dir, "fresh", start="npm run dev", port=4202,
            stack="node", install="npm install",
        )

        apps.start_app("fresh")

        assert "SLACK_APP_TOKEN" not in mock_run.call_args[1]["env"]

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


class TestPortAlreadyAnswers:
    """The gateway trusts whoever answers on an app's port: never start into a held one."""

    @patch("apps.subprocess.Popen")
    def test_start_refuses_a_port_another_program_holds(self, mock_popen, wolts_dir):
        import socket
        holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        holder.bind(("127.0.0.1", 0))
        holder.listen(1)
        port = holder.getsockname()[1]
        try:
            _make_app(wolts_dir, "squatted", start="echo hi", port=port)
            with pytest.raises(RuntimeError, match="already in use by another program"):
                apps.start_app("squatted")
            mock_popen.assert_not_called()
            assert apps._read_state("squatted") is None
        finally:
            holder.close()

    def test_a_free_port_answers_nothing(self):
        import socket
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # bound then released, nothing listening
        assert apps._port_answers(port) == ""


def _foreign_listener():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)  # room for every probe connection; nobody accepts them
    return sock, sock.getsockname()[1]


class TestForeignListenerAcrossLifecycle:
    @patch("apps.subprocess.Popen")
    def test_restore_and_restart_refuse_and_publish_nothing(self, mock_popen, wolts_dir):
        sock, port = _foreign_listener()
        try:
            _make_app(wolts_dir, "squatted", start="echo hi", port=port)
            apps._write_state("squatted", {"name": "squatted", "port": port, "pid": 999999})
            actions = apps.apps_restore()
            assert [a["action"] for a in actions if a["name"] == "squatted"] == ["restore-failed"]
            with pytest.raises(RuntimeError, match="already in use by another program"):
                apps.restart_app("squatted")
            mock_popen.assert_not_called()
            assert all(r["name"] != "squatted" for r in apps.running_apps())
        finally:
            sock.close()  # the foreign listener is never touched by the lodge



class TestManagedSamePortRace:
    @patch("apps.subprocess.Popen")
    def test_two_apps_on_one_port_only_one_spawns(self, mock_popen, wolts_dir, monkeypatch):
        # The first child never binds (a slow dev server), so the connect
        # probe alone would let the second app through.
        mock_popen.return_value.pid = os.getpid()
        monkeypatch.setattr(apps, "_port_answers", lambda port: "")
        _make_app(wolts_dir, "first", start="echo hi", port=4590)
        _make_app(wolts_dir, "second", start="echo hi", port=4590)
        real_running = apps.running_apps
        scans = {"n": 0}

        def running_apps_stale_first_scan():
            # The outer scan runs before "first" exists; only the in-lock
            # recheck can see it.
            scans["n"] += 1
            return [] if scans["n"] == 1 else real_running()

        apps.start_app("first")
        monkeypatch.setattr(apps, "running_apps", running_apps_stale_first_scan)
        with pytest.raises(RuntimeError, match="already in use by running app 'first'"):
            apps.start_app("second")
        assert scans["n"] == 2  # refused by the in-lock recheck, not the outer scan
        assert mock_popen.call_count == 1
        assert apps._read_state("second") is None


def test_gateway_refuses_an_app_whose_start_was_refused(wolts_dir, monkeypatch):
    """Real lifecycle: the app's manifest points at a port a foreign listener
    holds, start is refused, and the gateway then makes no upstream connection."""
    import asyncio
    import socket
    import httpx
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    import server.gateway as gateway

    foreign = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    foreign.bind(("127.0.0.1", 0))
    foreign.listen(64)
    foreign.setblocking(False)
    port = foreign.getsockname()[1]

    def drain():
        n = 0
        while True:
            try:
                conn, _ = foreign.accept()
                conn.close()
                n += 1
            except (BlockingIOError, OSError):
                return n

    try:
        _make_app(wolts_dir, "squatted", start="echo hi", port=port)
        with pytest.raises(RuntimeError, match="already in use by another program"):
            apps.start_app("squatted")
        assert drain() >= 1  # the start probe's own preflight connection
        monkeypatch.setattr(gateway, "WOLTS_DIR", wolts_dir)
        host = "squatted.localhost:7117"

        async def get():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=gateway.app, client=("127.0.0.1", 1)),
                base_url=f"http://{host}",
            ) as client:
                return await client.get("/", headers={"host": host})

        response = asyncio.run(get())
        assert response.status_code == 503
        with pytest.raises(WebSocketDisconnect):
            with TestClient(gateway.app, client=("127.0.0.1", 1)).websocket_connect(
                "/socket", headers={"host": host, "origin": f"http://{host}"},
            ):
                pass
        assert drain() == 0  # the gateway never connected to the foreign port
    finally:
        foreign.close()
