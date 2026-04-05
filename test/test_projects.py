"""Project isolation tests — unit, integration, and e2e.

Tests the project isolation feature end-to-end:
- Unit: registry tracks projects, tool params, system prompt, list_projects
- Integration: /projects and /project/{name}/ server endpoints
- E2E: bot tool → session scoped to project dir → project served

Usage:
  uv run pytest test/test_projects.py -v                  # all
  uv run pytest test/test_projects.py -k "Unit" -v        # unit only
  uv run pytest test/test_projects.py -k "Integration" -v # needs server
  uv run pytest test/test_projects.py -k "E2E" -v         # needs server + tmux
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
# Unit: Session registry tracks project field
# ---------------------------------------------------------------------------

class TestUnitRegistryProject:
    """Registry creates sessions with project metadata."""

    def test_create_with_project(self, tmp_registry):
        reg = tmp_registry
        data = reg.create("proj-sess-1", wolt="neowolt", project="my-app")
        assert data["project"] == "my-app"

    def test_create_without_project(self, tmp_registry):
        reg = tmp_registry
        data = reg.create("proj-sess-2", wolt="neowolt")
        assert data["project"] == ""

    def test_project_persists_on_read(self, tmp_registry):
        reg = tmp_registry
        reg.create("proj-sess-3", wolt="neowolt", project="dashboard")
        fetched = reg.get("proj-sess-3", check_alive=False)
        assert fetched["project"] == "dashboard"

    def test_list_sessions_includes_project(self, tmp_registry):
        reg = tmp_registry
        reg.create("sess-a", wolt="neowolt", project="app-one")
        reg.create("sess-b", wolt="neowolt", project="app-two")
        reg.create("sess-c", wolt="neowolt")  # no project
        sessions = reg.list()
        projects = [s["project"] for s in sessions]
        assert "app-one" in projects
        assert "app-two" in projects
        assert "" in projects


# ---------------------------------------------------------------------------
# Unit: Bot tool schemas include project parameter
# ---------------------------------------------------------------------------

class TestUnitToolSchemas:
    """Tool definitions expose the project parameter."""

    def test_claude_code_has_project_param(self):
        from bot.core import TOOLS
        claude_code = next(t for t in TOOLS if t["function"]["name"] == "claude_code")
        props = claude_code["function"]["parameters"]["properties"]
        assert "project" in props
        assert "string" == props["project"]["type"]

    def test_new_session_has_project_param(self):
        from bot.core import TOOLS
        new_session = next(t for t in TOOLS if t["function"]["name"] == "new_session")
        props = new_session["function"]["parameters"]["properties"]
        assert "project" in props

    def test_list_projects_tool_exists(self):
        from bot.core import TOOLS
        names = [t["function"]["name"] for t in TOOLS]
        assert "list_projects" in names

    def test_list_projects_in_handlers(self):
        from bot.core import TOOL_HANDLERS
        assert "list_projects" in TOOL_HANDLERS


# ---------------------------------------------------------------------------
# Unit: System prompt mentions projects
# ---------------------------------------------------------------------------

class TestUnitSystemPrompt:
    """System prompt teaches Haiku about project routing."""

    def test_prompt_mentions_projects(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "project" in prompt.lower()

    def test_prompt_mentions_list_projects(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "list_projects" in prompt

    def test_prompt_has_routing_guidance(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "wolt/projects/" in prompt


# ---------------------------------------------------------------------------
# Unit: list_projects tool implementation
# ---------------------------------------------------------------------------

class TestUnitListProjects:
    """_tool_list_projects reads project directories correctly."""

    def test_list_empty_projects(self, tmp_path):
        import bot.core as core
        original = core.WOLT_DIR
        try:
            core.WOLT_DIR = tmp_path
            projects_dir = tmp_path / "wolt" / "projects"
            projects_dir.mkdir(parents=True)
            result = json.loads(core._tool_list_projects({}, None))
            assert result["count"] == 0
            assert result["projects"] == []
        finally:
            core.WOLT_DIR = original

    def test_list_projects_with_dirs(self, tmp_path):
        import bot.core as core
        original = core.WOLT_DIR
        try:
            core.WOLT_DIR = tmp_path
            projects_dir = tmp_path / "wolt" / "projects"
            (projects_dir / "alpha").mkdir(parents=True)
            (projects_dir / "beta").mkdir(parents=True)
            result = json.loads(core._tool_list_projects({}, None))
            assert result["count"] == 2
            names = [p["name"] for p in result["projects"]]
            assert "alpha" in names
            assert "beta" in names
        finally:
            core.WOLT_DIR = original

    def test_list_projects_reads_project_json(self, tmp_path):
        import bot.core as core
        original = core.WOLT_DIR
        try:
            core.WOLT_DIR = tmp_path
            projects_dir = tmp_path / "wolt" / "projects"
            proj_dir = projects_dir / "my-app"
            proj_dir.mkdir(parents=True)
            (proj_dir / "project.json").write_text(json.dumps({
                "name": "my-app",
                "port": 4001,
                "description": "test app",
            }))
            result = json.loads(core._tool_list_projects({}, None))
            proj = result["projects"][0]
            assert proj["name"] == "my-app"
            assert proj["port"] == 4001
            assert proj["description"] == "test app"
        finally:
            core.WOLT_DIR = original

    def test_list_projects_skips_dotfiles(self, tmp_path):
        import bot.core as core
        original = core.WOLT_DIR
        try:
            core.WOLT_DIR = tmp_path
            projects_dir = tmp_path / "wolt" / "projects"
            (projects_dir / ".hidden").mkdir(parents=True)
            (projects_dir / "visible").mkdir(parents=True)
            result = json.loads(core._tool_list_projects({}, None))
            assert result["count"] == 1
            assert result["projects"][0]["name"] == "visible"
        finally:
            core.WOLT_DIR = original

    def test_list_projects_no_dir(self, tmp_path):
        """No projects directory at all should return empty list."""
        import bot.core as core
        original = core.WOLT_DIR
        try:
            core.WOLT_DIR = tmp_path
            result = json.loads(core._tool_list_projects({}, None))
            assert result["count"] == 0
        finally:
            core.WOLT_DIR = original


# ---------------------------------------------------------------------------
# Unit: start_claude_session project scoping
# ---------------------------------------------------------------------------

class TestUnitSessionProjectScoping:
    """start_claude_session passes project info through to start_session correctly."""

    def _mock_start_session(self, **kwargs):
        """Build a mock return value matching start_session's output."""
        result = {"name": "test-session-abc123", "url": None, "wolt": kwargs.get("wolt", "test")}
        if kwargs.get("project"):
            result["project"] = kwargs["project"]
        return result

    @patch("bot.core.start_session")
    def test_project_creates_directory(self, mock_start):
        """start_claude_session passes project to start_session."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        result = core.start_claude_session("build something", project="new-app")
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["project"] == "new-app"

    @patch("bot.core.start_session")
    def test_project_scopes_working_dir(self, mock_start):
        """start_session receives the project name for scoping."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("build something", project="scoped-app")
        assert mock_start.call_args[1]["project"] == "scoped-app"

    @patch("bot.core.start_session")
    def test_project_passed_to_registry(self, mock_start):
        """Project is forwarded to start_session which handles registry."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("test", project="tracked-app")
        assert mock_start.call_args[1]["project"] == "tracked-app"

    @patch("bot.core.start_session")
    def test_no_project_uses_wolt_root(self, mock_start):
        """No project means empty string passed to start_session."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        core.start_claude_session("test")
        assert mock_start.call_args[1]["project"] == ""

    @patch("bot.core.start_session")
    def test_project_in_result(self, mock_start):
        """Result includes project when provided."""
        import bot.core as core
        mock_start.side_effect = lambda **kw: self._mock_start_session(**kw)
        result = core.start_claude_session("test", project="visible-app")
        assert result["project"] == "visible-app"


# ---------------------------------------------------------------------------
# Unit: tool handlers pass project through
# ---------------------------------------------------------------------------

class TestUnitToolHandlers:
    """Tool handler functions forward the project parameter."""

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    def test_claude_code_passes_project(self, mock_start):
        from bot.core import _tool_claude_code
        _tool_claude_code({"prompt": "build it", "project": "my-proj"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["project"] == "my-proj"

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    def test_claude_code_no_project(self, mock_start):
        from bot.core import _tool_claude_code
        _tool_claude_code({"prompt": "build it"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["project"] is None

    @patch("bot.core.start_claude_session", return_value={"name": "test", "url": None, "wolt": "test"})
    @patch("bot.core.list_sessions", return_value=[])
    def test_new_session_passes_project(self, mock_list, mock_start):
        from bot.core import _tool_new_session
        _tool_new_session({"prompt": "start", "project": "dashboard"}, None)
        mock_start.assert_called_once()
        assert mock_start.call_args[1]["project"] == "dashboard"


# ---------------------------------------------------------------------------
# Integration: /projects endpoint
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationProjectsEndpoint:
    """Server /projects and /project/{name}/ endpoints."""

    def test_projects_endpoint_returns_list(self, server_get):
        result = server_get("/projects")
        assert isinstance(result, list)

    def test_project_not_found(self, server_get):
        result = server_get("/project/nonexistent-project-xyz")
        assert isinstance(result, (dict, str))
        # Should be 404 — either error dict or error text

    def test_project_invalid_name(self, server_get):
        """Names with special chars should be rejected."""
        result = server_get("/project/../etc/passwd")
        assert isinstance(result, (dict, str))


@requires_server
class TestIntegrationProjectServing:
    """Test that projects with woltspace.json get served correctly."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_test_project(self):
        """Create a temporary test project with woltspace.json and an HTML file."""
        self.project_dir = self.WOLTS_DIR / "projects" / "test-serve-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text(
            "<html><body><h1>Test Project</h1></body></html>"
        )
        (self.project_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-serve-project",
            "description": "A test project",
            "keeper": "neowolt",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_project_listed(self, server_get):
        result = server_get("/projects")
        names = [p["name"] for p in result]
        assert "test-serve-project" in names

    def test_project_serves_index(self, server_get):
        result = server_get("/project/test-serve-project/")
        assert isinstance(result, str)
        assert "Test Project" in result

    def test_project_serves_subpath(self, server_get):
        (self.project_dir / "about.html").write_text(
            "<html><body><h1>About</h1></body></html>"
        )
        result = server_get("/project/test-serve-project/about.html")
        assert isinstance(result, str)
        assert "About" in result

    def test_project_has_description(self, server_get):
        """Project with woltspace.json should have description in listing."""
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-serve-project")
        assert proj["description"] == "A test project"


@requires_server
class TestIntegrationProjectWithManifest:
    """Test project serving with full woltspace.json metadata."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_manifest_project(self):
        self.project_dir = self.WOLTS_DIR / "projects" / "test-manifest-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text("<html><body>Manifest App</body></html>")
        (self.project_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-manifest-project",
            "description": "A test project with manifest",
            "stack": "html",
            "keeper": "neowolt",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_manifest_metadata_in_list(self, server_get):
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-manifest-project")
        assert proj["description"] == "A test project with manifest"
        assert proj["stack"] == "html"

    def test_manifest_project_still_serves(self, server_get):
        result = server_get("/project/test-manifest-project/")
        assert "Manifest App" in result


@requires_server
class TestIntegrationProjectWithDist:
    """Test that dist/ directory takes priority for serving."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_dist_project(self):
        self.project_dir = self.WOLTS_DIR / "projects" / "test-dist-project"
        dist_dir = self.project_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        # Root index should NOT be served — dist/ takes priority
        (self.project_dir / "index.html").write_text("<html>ROOT (should not see this)</html>")
        (dist_dir / "index.html").write_text("<html><body>Built App</body></html>")
        (self.project_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-dist-project",
            "description": "A dist project",
            "keeper": "neowolt",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_dist_takes_priority(self, server_get):
        result = server_get("/project/test-dist-project/")
        assert "Built App" in result
        assert "should not see this" not in result

    def test_dist_listed_in_projects(self, server_get):
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-dist-project")
        assert proj["description"] == "A dist project"


# ---------------------------------------------------------------------------
# Integration: project path traversal protection
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationProjectSecurity:
    """Ensure projects can't serve files outside their directory."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_security_project(self):
        self.project_dir = self.WOLTS_DIR / "projects" / "test-security-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text("<html>safe</html>")
        (self.project_dir / "woltspace.json").write_text(json.dumps({
            "name": "test-security-project",
            "keeper": "neowolt",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_path_traversal_blocked(self, server_get):
        """../../../etc/passwd style paths should not escape project dir."""
        result = server_get("/project/test-security-project/../../.env")
        # Should not return .env contents
        if isinstance(result, str):
            assert "TELEGRAM_BOT_TOKEN" not in result
            assert "SPOTIFY" not in result


# ---------------------------------------------------------------------------
# E2E: Full project lifecycle via bot tools
# ---------------------------------------------------------------------------

@requires_server
@requires_tmux
class TestE2EProjectLifecycle:
    """End-to-end: create project via bot tool, verify it's served."""

    WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    @pytest.fixture(autouse=True)
    def setup_e2e_project(self):
        """Create a project directory with content for the e2e test."""
        self.project_dir = self.WOLTS_DIR / "projects" / "e2e-test-app"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text(
            "<html><body><h1>E2E Test App</h1><p>Built by beaver</p></body></html>"
        )
        (self.project_dir / "woltspace.json").write_text(json.dumps({
            "name": "e2e-test-app",
            "description": "E2E test project",
            "keeper": "neowolt",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_full_project_flow(self, server_get, tmp_registry):
        """Project created → listed → served → registry has project field."""
        # 1. Registry tracks project
        reg = tmp_registry
        data = reg.create("e2e-sess", wolt="neowolt", project="e2e-test-app", creature="beaver")
        assert data["project"] == "e2e-test-app"

        # 2. Project appears in listing
        projects = server_get("/projects")
        names = [p["name"] for p in projects]
        assert "e2e-test-app" in names

        # 3. Project content is served
        html = server_get("/project/e2e-test-app/")
        assert "E2E Test App" in html

        # 4. Project metadata is correct
        proj = next(p for p in projects if p["name"] == "e2e-test-app")
        assert proj["description"] == "E2E test project"
        assert proj["url"] == "/project/e2e-test-app/"

    def test_project_session_scoping_creates_dir(self):
        """start_session with project= creates the project directory under the wolt."""
        # Use a real wolt dir that exists
        wolt_name = os.environ.get("WOLT_NAME", "neowolt")
        wolt_dir = self.WOLTS_DIR / wolt_name
        test_proj = wolt_dir / "wolt" / "projects" / "e2e-auto-created"
        try:
            if test_proj.exists():
                import shutil
                shutil.rmtree(test_proj)
            assert not test_proj.exists()

            # Mock tmux and tunnel to avoid side effects, but let start_session
            # do the real filesystem work (project dir creation)
            with patch("sessions.subprocess") as mock_sub, \
                 patch("sessions.get_tunnel_url", return_value="https://test.trycloudflare.com"):
                mock_sub.run.return_value = MagicMock(returncode=0)
                from sessions import start_session
                start_session(wolt=wolt_name, prompt="test", project="e2e-auto-created")

            assert test_proj.exists()
            assert test_proj.is_dir()
        finally:
            if test_proj.exists():
                import shutil
                shutil.rmtree(test_proj)


# ---------------------------------------------------------------------------
# Unit: share_project / unshare_project
# ---------------------------------------------------------------------------

class TestUnitShareProject:
    """Unit tests for share_project() and unshare_project()."""

    def test_share_raises_if_not_running(self, tmp_path, monkeypatch):
        """share_project raises ValueError when project has no running state."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        with pytest.raises(ValueError, match="not running"):
            proj_mod.share_project("phantom-project")

    def test_unshare_returns_false_if_no_state(self, tmp_path, monkeypatch):
        """unshare_project returns False when project has no state file."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        assert proj_mod.unshare_project("phantom-project") is False

    def test_unshare_returns_false_if_no_tunnel(self, tmp_path, monkeypatch):
        """unshare_project returns False when state exists but no tunnel_pid."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        proj_mod._write_state("my-proj", {"name": "my-proj", "port": 4500, "pid": 99})
        assert proj_mod.unshare_project("my-proj") is False

    def test_unshare_clears_tunnel_fields(self, tmp_path, monkeypatch):
        """unshare_project clears tunnel_pid and tunnel_url in state."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        # _is_pid_alive returns False — tunnel process already gone
        monkeypatch.setattr(proj_mod, "_is_pid_alive", lambda pid: False)

        proj_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
            "tunnel_pid": 11111,
            "tunnel_url": "https://test.trycloudflare.com",
        })

        result = proj_mod.unshare_project("my-proj")
        assert result is True

        state = proj_mod._read_state("my-proj")
        assert state["tunnel_pid"] is None
        assert state["tunnel_url"] is None

    def test_share_returns_existing_tunnel_if_alive(self, tmp_path, monkeypatch):
        """share_project returns existing tunnel info if tunnel process is alive."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.setattr(proj_mod, "_is_pid_alive", lambda pid: True)

        proj_mod._write_state("my-proj", {
            "name": "my-proj",
            "port": 4500,
            "pid": 99,
            "tunnel_pid": 11111,
            "tunnel_url": "https://already-live.trycloudflare.com",
        })

        result = proj_mod.share_project("my-proj")
        assert result["tunnel_url"] == "https://already-live.trycloudflare.com"
        assert result["pid"] == 11111

    def test_share_blocked_when_sharing_disabled(self, tmp_path, monkeypatch):
        """share_project raises RuntimeError when SHARING_ENABLED is False."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)
        monkeypatch.setattr(proj_mod, "SHARING_ENABLED", False)

        proj_mod._write_state("my-proj", {"name": "my-proj", "port": 4500, "pid": 99})

        with pytest.raises(RuntimeError, match="Sharing is disabled"):
            proj_mod.share_project("my-proj")

    def test_unshare_all_stops_all_tunnels(self, tmp_path, monkeypatch):
        """unshare_all_projects kills all tunnel processes."""
        import projects as proj_mod
        monkeypatch.setattr(proj_mod, "_RUNNING_STATE_DIR", tmp_path)

        killed_pids = []
        def mock_kill(pid):
            killed_pids.append(pid)
        monkeypatch.setattr(proj_mod, "_is_pid_alive", lambda pid: True)
        monkeypatch.setattr(os, "kill", lambda pid, sig: killed_pids.append(pid))
        # Also mock _set_public to avoid filesystem writes
        monkeypatch.setattr(proj_mod, "_set_public", lambda name, public: None)

        proj_mod._write_state("proj-a", {
            "name": "proj-a", "port": 4500, "pid": 10,
            "tunnel_pid": 111, "tunnel_url": "https://a.trycloudflare.com",
        })
        proj_mod._write_state("proj-b", {
            "name": "proj-b", "port": 4501, "pid": 20,
            "tunnel_pid": 222, "tunnel_url": "https://b.trycloudflare.com",
        })
        proj_mod._write_state("proj-c", {
            "name": "proj-c", "port": 4502, "pid": 30,
        })  # No tunnel

        unshared = proj_mod.unshare_all_projects()
        assert set(unshared) == {"proj-a", "proj-b"}
        assert 111 in killed_pids
        assert 222 in killed_pids


# ---------------------------------------------------------------------------
# Integration: share/unshare endpoints
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationShareEndpoints:
    """Test /projects/{name}/share and /projects/{name}/unshare endpoints."""

    def test_share_nonexistent_project_returns_error(self, server_post):
        """Sharing a project that has no running state returns an error."""
        result = server_post("/projects/definitely-does-not-exist-xyz/share", {})
        assert "error" in result

    def test_unshare_nonexistent_project_returns_error(self, server_post):
        """Unsharing a project with no tunnel returns an error."""
        result = server_post("/projects/definitely-does-not-exist-xyz/unshare", {})
        assert "error" in result


# ---------------------------------------------------------------------------
# Unit: set_viewport auto-detects project port
# ---------------------------------------------------------------------------

class TestUnitSetViewportProjectPort:
    """set_viewport() auto-detects project port for /project/ URLs."""

    def test_set_viewport_nonproject_url_unchanged(self, tmp_registry):
        """Non-project URLs keep port=7777."""
        reg = tmp_registry
        reg.create("sess-vp", wolt="neowolt")
        reg.set_viewport("sess-vp", "/wolt/neowolt/site/index.html", wolt="neowolt")
        data = reg.get("sess-vp", check_alive=False)
        assert data["viewport_port"] == 7777

    def test_set_viewport_project_url_no_running_project(self, tmp_registry):
        """Project URL with no running project keeps port=7777."""
        reg = tmp_registry
        reg.create("sess-vp2", wolt="neowolt")
        # No running state for "my-app" — port stays 7777
        reg.set_viewport("sess-vp2", "/project/my-app/", wolt="neowolt")
        data = reg.get("sess-vp2", check_alive=False)
        assert data["viewport_url"] == "/project/my-app/"
        assert data["viewport_port"] == 7777

    def test_set_viewport_explicit_port_respected(self, tmp_registry):
        """Explicit port= is always used, no auto-detection."""
        reg = tmp_registry
        reg.create("sess-vp3", wolt="neowolt")
        reg.set_viewport("sess-vp3", "/project/my-app/", wolt="neowolt", port=4500)
        data = reg.get("sess-vp3", check_alive=False)
        assert data["viewport_port"] == 4500
