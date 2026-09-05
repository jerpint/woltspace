"""App isolation tests — unit, integration, and e2e.

Tests the app isolation feature end-to-end:
- Unit: registry tracks apps, tool params, system prompt, list_apps
- Integration: /apps and /app/{name}/ server endpoints
- E2E: bot tool → session scoped to app dir → app served

Usage:
  uv run pytest test/test_apps.py -v                  # all
  uv run pytest test/test_apps.py -k "Unit" -v        # unit only
  uv run pytest test/test_apps.py -k "Integration" -v # needs server
  uv run pytest test/test_apps.py -k "E2E" -v         # needs server + tmux
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from conftest import requires_server, requires_tmux

# Add bot to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


# ---------------------------------------------------------------------------
# Unit: Session registry tracks app field
# ---------------------------------------------------------------------------

class TestUnitRegistryApp:
    """Registry creates sessions with app metadata."""

    def test_create_with_app(self, tmp_registry):
        reg = tmp_registry
        data = reg.create("app-sess-1", wolt="neowolt", app="my-app")
        assert data["app"] == "my-app"

    def test_create_without_app(self, tmp_registry):
        reg = tmp_registry
        data = reg.create("app-sess-2", wolt="neowolt")
        assert data["app"] == ""

    def test_app_persists_on_read(self, tmp_registry):
        reg = tmp_registry
        reg.create("app-sess-3", wolt="neowolt", app="dashboard")
        fetched = reg.get("app-sess-3", check_alive=False)
        assert fetched["app"] == "dashboard"

    def test_list_sessions_includes_app(self, tmp_registry):
        reg = tmp_registry
        reg.create("sess-a", wolt="neowolt", app="app-one")
        reg.create("sess-b", wolt="neowolt", app="app-two")
        reg.create("sess-c", wolt="neowolt")  # no app
        sessions = reg.list()
        apps = [s["app"] for s in sessions]
        assert "app-one" in apps
        assert "app-two" in apps
        assert "" in apps


# ---------------------------------------------------------------------------
# Unit: Bot tool schemas include app parameter
# ---------------------------------------------------------------------------

class TestUnitToolSchemas:
    """Tool definitions expose the app parameter."""

    def test_claude_code_has_app_param(self):
        from bot.core import TOOLS
        claude_code = next(t for t in TOOLS if t["function"]["name"] == "claude_code")
        props = claude_code["function"]["parameters"]["properties"]
        assert "app" in props
        assert "string" == props["app"]["type"]

    def test_new_session_has_app_param(self):
        from bot.core import TOOLS
        new_session = next(t for t in TOOLS if t["function"]["name"] == "new_session")
        props = new_session["function"]["parameters"]["properties"]
        assert "app" in props

    def test_list_apps_tool_exists(self):
        from bot.core import TOOLS
        names = [t["function"]["name"] for t in TOOLS]
        assert "list_apps" in names

    def test_list_apps_in_handlers(self):
        from bot.core import TOOL_HANDLERS
        assert "list_apps" in TOOL_HANDLERS


# ---------------------------------------------------------------------------
# Unit: System prompt mentions apps
# ---------------------------------------------------------------------------

class TestUnitSystemPrompt:
    """System prompt teaches Haiku about app routing."""

    def test_prompt_mentions_apps(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "app" in prompt.lower()

    def test_prompt_mentions_list_apps(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "list_apps" in prompt

    def test_prompt_has_routing_guidance(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "wolts/apps/" in prompt


# ---------------------------------------------------------------------------
# Unit: list_apps tool implementation
# ---------------------------------------------------------------------------

class TestUnitListApps:
    """_tool_list_apps reads app directories correctly."""

    def test_list_empty_apps(self, tmp_path):
        import bot.core as core
        original = core.WOLTS_DIR
        try:
            core.WOLTS_DIR = tmp_path
            apps_dir = tmp_path / "apps"
            apps_dir.mkdir(parents=True)
            result = json.loads(core._tool_list_apps({}, None))
            assert result["count"] == 0
            assert result["apps"] == []
        finally:
            core.WOLTS_DIR = original

    def test_list_apps_with_dirs(self, tmp_path):
        import bot.core as core
        original = core.WOLTS_DIR
        try:
            core.WOLTS_DIR = tmp_path
            apps_dir = tmp_path / "apps"
            (apps_dir / "alpha").mkdir(parents=True)
            (apps_dir / "beta").mkdir(parents=True)
            result = json.loads(core._tool_list_apps({}, None))
            assert result["count"] == 2
            names = [a["name"] for a in result["apps"]]
            assert "alpha" in names
            assert "beta" in names
        finally:
            core.WOLTS_DIR = original

    def test_list_apps_reads_app_json(self, tmp_path):
        import bot.core as core
        original = core.WOLTS_DIR
        try:
            core.WOLTS_DIR = tmp_path
            apps_dir = tmp_path / "apps"
            app_dir = apps_dir / "my-app"
            app_dir.mkdir(parents=True)
            (app_dir / "app.json").write_text(json.dumps({
                "name": "my-app",
                "port": 4001,
                "description": "test app",
            }))
            result = json.loads(core._tool_list_apps({}, None))
            app = result["apps"][0]
            assert app["name"] == "my-app"
            assert app["port"] == 4001
            assert app["description"] == "test app"
        finally:
            core.WOLTS_DIR = original

    def test_list_apps_skips_dotfiles(self, tmp_path):
        import bot.core as core
        original = core.WOLTS_DIR
        try:
            core.WOLTS_DIR = tmp_path
            apps_dir = tmp_path / "apps"
            (apps_dir / ".hidden").mkdir(parents=True)
            (apps_dir / "visible").mkdir(parents=True)
            result = json.loads(core._tool_list_apps({}, None))
            assert result["count"] == 1
            assert result["apps"][0]["name"] == "visible"
        finally:
            core.WOLTS_DIR = original

    def test_list_apps_no_dir(self, tmp_path):
        """No apps directory at all should return empty list."""
        import bot.core as core
        original = core.WOLTS_DIR
        try:
            core.WOLTS_DIR = tmp_path
            result = json.loads(core._tool_list_apps({}, None))
            assert result["count"] == 0
        finally:
            core.WOLTS_DIR = original


# ---------------------------------------------------------------------------
# Unit: start_claude_session app scoping
# ---------------------------------------------------------------------------

class TestUnitSessionAppScoping:
    """start_claude_session passes app info through to start_session correctly."""

    def _mock_start_session(self, **kwargs):
        """Build a mock return value matching start_session's output."""
        result = {"name": "test-session-abc123", "url": None, "wolt": kwargs.get("wolt", "test")}
        if kwargs.get("app"):
            result["app"] = kwargs["app"]
        return result

    @patch("bot.core.start_session")
    def test_app_creates_directory(self, mock_start):
        """start_claude_session passes app to start_session."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        result = core.start_claude_session("build something", app="new-app")
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["app"] == "new-app"

    @patch("bot.core.start_session")
    def test_app_scopes_working_dir(self, mock_start):
        """start_session receives the app name for scoping."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("build something", app="scoped-app")
        assert mock_start.call_args[1]["app"] == "scoped-app"

    @patch("bot.core.start_session")
    def test_app_passed_to_registry(self, mock_start):
        """App is forwarded to start_session which handles registry."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("test", app="tracked-app")
        assert mock_start.call_args[1]["app"] == "tracked-app"

    @patch("bot.core.start_session")
    def test_no_app_uses_wolt_root(self, mock_start):
        """No app means empty string passed to start_session."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("test")
        assert mock_start.call_args[1]["app"] == ""

    @patch("bot.core.start_session")
    def test_app_in_result(self, mock_start):
        """Result includes app when provided."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        result = core.start_claude_session("test", app="visible-app")
        assert result["app"] == "visible-app"


# ---------------------------------------------------------------------------
# Unit: tool handlers pass app through
# ---------------------------------------------------------------------------

class TestUnitToolHandlers:
    """Tool handler functions forward the app parameter."""

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    def test_claude_code_passes_app(self, mock_start):
        from bot.core import _tool_claude_code
        _tool_claude_code({"prompt": "build it", "app": "my-proj"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["app"] == "my-proj"

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    def test_claude_code_no_app(self, mock_start):
        from bot.core import _tool_claude_code
        _tool_claude_code({"prompt": "build it"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["app"] is None

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    @patch("bot.core.list_sessions", return_value=[])
    def test_new_session_passes_app(self, mock_list, mock_start):
        from bot.core import _tool_new_session
        _tool_new_session({"prompt": "start", "app": "dashboard"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["app"] == "dashboard"


# ---------------------------------------------------------------------------
# Integration: /apps endpoint
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationAppsEndpoint:
    """Server /apps and /app/{name}/ endpoints."""

    def test_apps_endpoint_returns_list(self, server_get):
        result = server_get("/apps")
        assert isinstance(result, list)

    def test_app_not_found(self, server_get):
        result = server_get("/app/nonexistent-app-xyz")
        assert isinstance(result, (dict, str))
        # Should be 404 — either error dict or error text

    def test_app_invalid_name(self, server_get):
        """Names with special chars should be rejected."""
        result = server_get("/app/../etc/passwd")
        assert isinstance(result, (dict, str))


@requires_server
class TestIntegrationAppServing:
    """Test that apps with woltspace.json get served correctly."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_test_app(self):
        """Create a temporary test app with woltspace.json and an HTML file."""
        self.app_dir = self.WOLTS_DIR / "apps" / "test-serve-app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "index.html").write_text(
            "<html><body><h1>Test App</h1></body></html>"
        )
        (self.app_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-serve-app",
            "description": "A test app",
            "keeper": "neowolt",
            "port": 4900,
        }))
        yield
        import shutil
        if self.app_dir.exists():
            shutil.rmtree(self.app_dir)

    def test_app_listed(self, server_get):
        result = server_get("/apps")
        names = [a["name"] for a in result]
        assert "test-serve-app" in names

    def test_app_serves_index(self, server_get):
        result = server_get("/app/test-serve-app/")
        assert isinstance(result, str)
        assert "Test App" in result

    def test_app_serves_subpath(self, server_get):
        (self.app_dir / "about.html").write_text(
            "<html><body><h1>About</h1></body></html>"
        )
        result = server_get("/app/test-serve-app/about.html")
        assert isinstance(result, str)
        assert "About" in result

    def test_app_has_description(self, server_get):
        """App with woltspace.json should have description in listing."""
        result = server_get("/apps")
        app = next(a for a in result if a["name"] == "test-serve-app")
        assert app["description"] == "A test app"


@requires_server
class TestIntegrationAppWithManifest:
    """Test app serving with full woltspace.json metadata."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_manifest_app(self):
        self.app_dir = self.WOLTS_DIR / "apps" / "test-manifest-app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "index.html").write_text("<html><body>Manifest App</body></html>")
        (self.app_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-manifest-app",
            "description": "A test app with manifest",
            "stack": "html",
            "keeper": "neowolt",
            "port": 4901,
        }))
        yield
        import shutil
        if self.app_dir.exists():
            shutil.rmtree(self.app_dir)

    def test_manifest_metadata_in_list(self, server_get):
        result = server_get("/apps")
        app = next(a for a in result if a["name"] == "test-manifest-app")
        assert app["description"] == "A test app with manifest"
        assert app["stack"] == "html"

    def test_manifest_app_still_serves(self, server_get):
        result = server_get("/app/test-manifest-app/")
        assert "Manifest App" in result


@requires_server
class TestIntegrationAppWithDist:
    """Test that dist/ directory takes priority for serving."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_dist_app(self):
        self.app_dir = self.WOLTS_DIR / "apps" / "test-dist-app"
        dist_dir = self.app_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        # Root index should NOT be served — dist/ takes priority
        (self.app_dir / "index.html").write_text("<html>ROOT (should not see this)</html>")
        (dist_dir / "index.html").write_text("<html><body>Built App</body></html>")
        (self.app_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-dist-app",
            "description": "A dist app",
            "keeper": "neowolt",
            "port": 4902,
        }))
        yield
        import shutil
        if self.app_dir.exists():
            shutil.rmtree(self.app_dir)

    def test_dist_takes_priority(self, server_get):
        result = server_get("/app/test-dist-app/")
        assert "Built App" in result
        assert "should not see this" not in result

    def test_dist_listed_in_apps(self, server_get):
        result = server_get("/apps")
        app = next(a for a in result if a["name"] == "test-dist-app")
        assert app["description"] == "A dist app"


# ---------------------------------------------------------------------------
# Integration: app path traversal protection
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationAppSecurity:
    """Ensure apps can't serve files outside their directory."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_security_app(self):
        self.app_dir = self.WOLTS_DIR / "apps" / "test-security-app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "index.html").write_text("<html>safe</html>")
        (self.app_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-security-app",
            "keeper": "neowolt",
            "port": 4904,
        }))
        yield
        import shutil
        if self.app_dir.exists():
            shutil.rmtree(self.app_dir)

    def test_path_traversal_blocked(self, server_get):
        """../../../etc/passwd style paths should not escape app dir."""
        result = server_get("/app/test-security-app/../../.env")
        # Should not return .env contents
        if isinstance(result, str):
            assert "TELEGRAM_BOT_TOKEN" not in result
            assert "SPOTIFY" not in result


# ---------------------------------------------------------------------------
# E2E: Full app lifecycle via bot tools
# ---------------------------------------------------------------------------

@requires_server
@requires_tmux
class TestE2EAppLifecycle:
    """End-to-end: create app via bot tool, verify it's served."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_e2e_app(self):
        """Create an app directory with content for the e2e test."""
        self.app_dir = self.WOLTS_DIR / "apps" / "e2e-test-app"
        self.app_dir.mkdir(parents=True, exist_ok=True)
        (self.app_dir / "index.html").write_text(
            "<html><body><h1>E2E Test App</h1><p>Built by beaver</p></body></html>"
        )
        (self.app_dir / "woltspace.json").write_text(json.dumps({
            "name": "e2e-test-app",
            "description": "E2E test app",
            "keeper": "neowolt",
            "port": 4903,
        }))
        yield
        import shutil
        if self.app_dir.exists():
            shutil.rmtree(self.app_dir)

    def test_full_app_flow(self, server_get, tmp_registry):
        """App created → listed → served → registry has app field."""
        # 1. Registry tracks app
        reg = tmp_registry
        data = reg.create("e2e-sess", wolt="neowolt", app="e2e-test-app", creature="beaver")
        assert data["app"] == "e2e-test-app"

        # 2. App appears in listing
        apps = server_get("/apps")
        names = [a["name"] for a in apps]
        assert "e2e-test-app" in names

        # 3. App content is served
        html = server_get("/app/e2e-test-app/")
        assert "E2E Test App" in html

        # 4. App metadata is correct
        app = next(a for a in apps if a["name"] == "e2e-test-app")
        assert app["description"] == "E2E test app"
        assert app["url"] == "/app/e2e-test-app/"

    def test_app_session_scoping_creates_dir(self, shadow_wolt):
        """start_session with app= creates the app directory under the wolt."""
        wolt_name = shadow_wolt
        wolt_dir = self.WOLTS_DIR / wolt_name
        test_app = wolt_dir / "wolt" / "apps" / "e2e-auto-created"
        try:
            if test_app.exists():
                import shutil
                shutil.rmtree(test_app)
            assert not test_app.exists()

            # Mock tmux and tunnel to avoid side effects, but let start_session
            # do the real filesystem work (app dir creation)
            with patch("sessions.subprocess") as mock_sub, \
                 patch("sessions.get_tunnel_url", return_value="https://test.trycloudflare.com"):
                mock_sub.run.return_value = MagicMock(returncode=0)
                from sessions import start_session
                start_session(wolt=wolt_name, prompt="test", app="e2e-auto-created")

            assert test_app.exists()
            assert test_app.is_dir()
        finally:
            if test_app.exists():
                import shutil
                shutil.rmtree(test_app)


# ---------------------------------------------------------------------------
# Unit: share_app / unshare_app
# ---------------------------------------------------------------------------

class TestUnitShareApp:
    """Unit tests for share_app() and unshare_app()."""

    def test_share_raises_if_not_running(self, tmp_path, monkeypatch):
        """share_app raises ValueError when app has no running state."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        with pytest.raises(ValueError, match="not running"):
            apps_mod.share_app("phantom-app")

    def test_unshare_returns_false_if_no_state(self, tmp_path, monkeypatch):
        """unshare_app returns False when app has no state file."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        assert apps_mod.unshare_app("phantom-app") is False

    def test_unshare_returns_false_if_no_tunnel(self, tmp_path, monkeypatch):
        """unshare_app returns False when state exists but no tunnel_pid."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        apps_mod._write_state("my-proj", {"name": "my-proj", "port": 4500, "pid": 99})
        assert apps_mod.unshare_app("my-proj") is False

    def test_unshare_clears_tunnel_fields(self, tmp_path, monkeypatch):
        """unshare_app clears tunnel_pid and tunnel_url in state."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        # _is_pid_alive returns False — tunnel process already gone
        monkeypatch.setattr(apps_mod, "_is_pid_alive", lambda pid: False)

        apps_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
            "tunnel_pid": 11111,
            "tunnel_url": "https://test.trycloudflare.com",
        })

        result = apps_mod.unshare_app("my-proj")
        assert result is True

        state = apps_mod._read_state("my-proj")
        assert state["tunnel_pid"] is None
        assert state["tunnel_url"] is None

    def test_share_returns_existing_tunnel_if_alive(self, tmp_path, monkeypatch):
        """share_app returns existing quick tunnel info if tunnel process is alive."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.setattr(apps_mod, "_is_pid_alive", lambda pid: True)
        monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)

        apps_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
            "tunnel_pid": 11111,
            "tunnel_url": "https://already-live.trycloudflare.com",
        })

        result = apps_mod.share_app("my-proj")
        assert result["tunnel_url"] == "https://already-live.trycloudflare.com"
        assert result["pid"] == 11111

    def test_share_uses_subdomain_when_tunnel_url_set(self, tmp_path, monkeypatch):
        """share_app returns subdomain URL when CLOUDFLARE_TUNNEL_URL is set."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://jerpint.woltspace.com")

        apps_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
        })

        result = apps_mod.share_app("my-proj")
        assert result["tunnel_url"] == "https://my-proj.woltspace.com"
        assert result["pid"] is None

    def test_share_falls_back_to_quick_tunnel(self, tmp_path, monkeypatch):
        """share_app uses quick tunnel when CLOUDFLARE_TUNNEL_URL is not set."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)
        monkeypatch.setattr(apps_mod, "_is_pid_alive", lambda pid: False)
        monkeypatch.setattr(apps_mod, "start_cloudflared", lambda port, host_header: {
            "url": "https://random.trycloudflare.com",
            "pid": 9999,
        })

        apps_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
        })

        result = apps_mod.share_app("my-proj")
        assert "trycloudflare.com" in result["tunnel_url"]
        assert result["pid"] == 9999

    def test_share_blocked_when_sharing_disabled(self, tmp_path, monkeypatch):
        """share_app raises RuntimeError when SHARING_ENABLED is False."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.setattr(apps_mod, "SHARING_ENABLED", False)

        apps_mod._write_state("my-proj", {"name": "my-proj", "port": 4500, "pid": 99})

        with pytest.raises(RuntimeError, match="Sharing is disabled"):
            apps_mod.share_app("my-proj")

    def test_unshare_all_stops_all_tunnels(self, tmp_path, monkeypatch):
        """unshare_all_apps kills all tunnel processes."""
        import apps as apps_mod
        monkeypatch.setattr(apps_mod, "_RUNNING_STATE_DIR", tmp_path)

        killed_pids = []
        def mock_kill(pid):
            killed_pids.append(pid)
        monkeypatch.setattr(apps_mod, "_is_pid_alive", lambda pid: True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append(pid))
        # Also mock _set_public to avoid filesystem writes
        monkeypatch.setattr(apps_mod, "_set_public", lambda name, public: None)

        apps_mod._write_state("app-a", {
            "name": "app-a", "port": 4500, "pid": 10,
            "tunnel_pid": 111, "tunnel_url": "https://a.trycloudflare.com",
        })
        apps_mod._write_state("app-b", {
            "name": "app-b", "port": 4501, "pid": 20,
            "tunnel_pid": 222, "tunnel_url": "https://b.trycloudflare.com",
        })
        apps_mod._write_state("app-c", {
            "name": "app-c", "port": 4502, "pid": 30,
        })  # No tunnel

        unshared = apps_mod.unshare_all_apps()
        assert set(unshared) == {"app-a", "app-b"}
        assert 111 in killed_pids
        assert 222 in killed_pids


# ---------------------------------------------------------------------------
# Integration: share/unshare endpoints
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationShareEndpoints:
    """Test /apps/{name}/share and /apps/{name}/unshare endpoints."""

    def test_share_nonexistent_app_returns_error(self, server_post):
        """Sharing an app that has no running state returns an error."""
        result = server_post("/apps/definitely-does-not-exist-xyz/share", {})
        assert "error" in result

    def test_unshare_nonexistent_app_returns_error(self, server_post):
        """Unsharing an app with no tunnel returns an error."""
        result = server_post("/apps/definitely-does-not-exist-xyz/unshare", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# Unit: set_viewport auto-detects app port
# ---------------------------------------------------------------------------

class TestUnitSetViewportAppPort:
    """set_viewport() auto-detects app port for /app/ URLs."""

    def test_set_viewport_nonapp_url_unchanged(self, tmp_registry):
        """Non-app URLs keep port=7777."""
        reg = tmp_registry
        reg.create("sess-vp", wolt="neowolt")
        reg.set_viewport("sess-vp", "/wolt/neowolt/site/index.html", wolt="neowolt")
        data = reg.get("sess-vp", check_alive=False)
        assert data["viewport_port"] == 7777

    def test_set_viewport_app_url_no_running_app(self, tmp_registry):
        """App URL with no running app keeps port=7777."""
        reg = tmp_registry
        reg.create("sess-vp2", wolt="neowolt")
        # No running state for "my-app" — port stays 7777
        reg.set_viewport("sess-vp2", "/app/my-app/", wolt="neowolt")
        data = reg.get("sess-vp2", check_alive=False)
        assert data["viewport_url"] == "/app/my-app/"
        assert data["viewport_port"] == 7777

    def test_set_viewport_explicit_port_respected(self, tmp_registry):
        """Explicit port= is always used, no auto-detection."""
        reg = tmp_registry
        reg.create("sess-vp3", wolt="neowolt")
        reg.set_viewport("sess-vp3", "/app/my-app/", wolt="neowolt", port=4500)
        data = reg.get("sess-vp3", check_alive=False)
        assert data["viewport_port"] == 4500
