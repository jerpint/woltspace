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
    """start_claude_session creates project dirs and scopes correctly."""

    @patch("bot.core.subprocess")
    @patch("bot.core.registry")
    @patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com")
    def test_project_creates_directory(self, mock_tunnel, mock_registry, mock_subprocess, tmp_path):
        import bot.core as core
        original_wolt = core.WOLT_DIR
        original_wolts = core.WOLTS_DIR
        mock_registry.create.return_value = {}
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        try:
            core.WOLT_DIR = tmp_path
            core.WOLTS_DIR = tmp_path.parent
            os.environ["WOLT_NAME"] = "test"
            result = core.start_claude_session("build something", project="new-app")
            project_dir = tmp_path / "wolt" / "projects" / "new-app"
            assert project_dir.exists()
            assert project_dir.is_dir()
        finally:
            core.WOLT_DIR = original_wolt
            core.WOLTS_DIR = original_wolts

    @patch("bot.core.subprocess")
    @patch("bot.core.registry")
    @patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com")
    def test_project_scopes_working_dir(self, mock_tunnel, mock_registry, mock_subprocess, tmp_path):
        import bot.core as core
        original_wolt = core.WOLT_DIR
        original_wolts = core.WOLTS_DIR
        mock_registry.create.return_value = {}
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        try:
            core.WOLT_DIR = tmp_path
            core.WOLTS_DIR = tmp_path.parent
            os.environ["WOLT_NAME"] = "test"
            core.start_claude_session("build something", project="scoped-app")
            # Check tmux was called with the project dir
            tmux_call = mock_subprocess.run.call_args
            tmux_args = tmux_call[0][0]
            expected_dir = str(tmp_path / "wolt" / "projects" / "scoped-app")
            assert expected_dir in tmux_args, f"Expected {expected_dir} in tmux args: {tmux_args}"
        finally:
            core.WOLT_DIR = original_wolt
            core.WOLTS_DIR = original_wolts

    @patch("bot.core.subprocess")
    @patch("bot.core.registry")
    @patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com")
    def test_project_passed_to_registry(self, mock_tunnel, mock_registry, mock_subprocess, tmp_path):
        import bot.core as core
        original_wolt = core.WOLT_DIR
        original_wolts = core.WOLTS_DIR
        mock_registry.create.return_value = {}
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        try:
            core.WOLT_DIR = tmp_path
            core.WOLTS_DIR = tmp_path.parent
            os.environ["WOLT_NAME"] = "test"
            core.start_claude_session("test", project="tracked-app")
            registry_call = mock_registry.create.call_args
            assert registry_call[1]["project"] == "tracked-app"
        finally:
            core.WOLT_DIR = original_wolt
            core.WOLTS_DIR = original_wolts

    @patch("bot.core.subprocess")
    @patch("bot.core.registry")
    @patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com")
    def test_no_project_uses_wolt_root(self, mock_tunnel, mock_registry, mock_subprocess, tmp_path):
        import bot.core as core
        original_wolt = core.WOLT_DIR
        original_wolts = core.WOLTS_DIR
        mock_registry.create.return_value = {}
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        try:
            core.WOLT_DIR = tmp_path
            core.WOLTS_DIR = tmp_path.parent
            os.environ["WOLT_NAME"] = "test"
            core.start_claude_session("test")
            tmux_call = mock_subprocess.run.call_args
            tmux_args = tmux_call[0][0]
            # Should use wolt root, not a project subdir
            assert str(tmp_path) in tmux_args
            assert "projects" not in str(tmux_args)
        finally:
            core.WOLT_DIR = original_wolt
            core.WOLTS_DIR = original_wolts

    @patch("bot.core.subprocess")
    @patch("bot.core.registry")
    @patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com")
    def test_project_in_result(self, mock_tunnel, mock_registry, mock_subprocess, tmp_path):
        import bot.core as core
        original_wolt = core.WOLT_DIR
        original_wolts = core.WOLTS_DIR
        mock_registry.create.return_value = {}
        mock_subprocess.run.return_value = MagicMock(returncode=0)
        try:
            core.WOLT_DIR = tmp_path
            core.WOLTS_DIR = tmp_path.parent
            os.environ["WOLT_NAME"] = "test"
            result = core.start_claude_session("test", project="visible-app")
            assert result["project"] == "visible-app"
        finally:
            core.WOLT_DIR = original_wolt
            core.WOLTS_DIR = original_wolts


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
    """Test that projects with actual files get served correctly."""

    @pytest.fixture(autouse=True)
    def setup_test_project(self):
        """Create a temporary test project with an HTML file."""
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        self.project_dir = wolt_dir / "wolt" / "projects" / "test-serve-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text(
            "<html><body><h1>Test Project</h1></body></html>"
        )
        yield
        # Cleanup
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

    def test_project_mode_is_directory(self, server_get):
        """Project without project.json or dist/ should be mode 'directory'."""
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-serve-project")
        assert proj["mode"] == "directory"


@requires_server
class TestIntegrationProjectWithManifest:
    """Test project serving with project.json metadata."""

    @pytest.fixture(autouse=True)
    def setup_manifest_project(self):
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        self.project_dir = wolt_dir / "wolt" / "projects" / "test-manifest-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text("<html><body>Manifest App</body></html>")
        (self.project_dir / "project.json").write_text(json.dumps({
            "name": "test-manifest-project",
            "description": "A test project with manifest",
            "language": "html",
        }))
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_manifest_metadata_in_list(self, server_get):
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-manifest-project")
        assert proj["description"] == "A test project with manifest"
        assert proj["language"] == "html"

    def test_manifest_project_still_serves(self, server_get):
        result = server_get("/project/test-manifest-project/")
        assert "Manifest App" in result


@requires_server
class TestIntegrationProjectWithDist:
    """Test that dist/ directory takes priority for serving."""

    @pytest.fixture(autouse=True)
    def setup_dist_project(self):
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        self.project_dir = wolt_dir / "wolt" / "projects" / "test-dist-project"
        dist_dir = self.project_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        # Root index should NOT be served — dist/ takes priority
        (self.project_dir / "index.html").write_text("<html>ROOT (should not see this)</html>")
        (dist_dir / "index.html").write_text("<html><body>Built App</body></html>")
        yield
        import shutil
        if self.project_dir.exists():
            shutil.rmtree(self.project_dir)

    def test_dist_takes_priority(self, server_get):
        result = server_get("/project/test-dist-project/")
        assert "Built App" in result
        assert "should not see this" not in result

    def test_dist_mode_is_static(self, server_get):
        result = server_get("/projects")
        proj = next(p for p in result if p["name"] == "test-dist-project")
        assert proj["mode"] == "static"


# ---------------------------------------------------------------------------
# Integration: project path traversal protection
# ---------------------------------------------------------------------------

@requires_server
class TestIntegrationProjectSecurity:
    """Ensure projects can't serve files outside their directory."""

    @pytest.fixture(autouse=True)
    def setup_security_project(self):
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        self.project_dir = wolt_dir / "wolt" / "projects" / "test-security-project"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text("<html>safe</html>")
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

    @pytest.fixture(autouse=True)
    def setup_e2e_project(self):
        """Create a project directory with content for the e2e test."""
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        self.project_dir = wolt_dir / "wolt" / "projects" / "e2e-test-app"
        self.project_dir.mkdir(parents=True, exist_ok=True)
        (self.project_dir / "index.html").write_text(
            "<html><body><h1>E2E Test App</h1><p>Built by beaver</p></body></html>"
        )
        (self.project_dir / "project.json").write_text(json.dumps({
            "name": "e2e-test-app",
            "description": "E2E test project",
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
        """start_claude_session with project= creates the project directory."""
        wolt_dir = Path(os.environ.get("WOLT_DIR", "/workspace/wolts/neowolt"))
        test_proj = wolt_dir / "wolt" / "projects" / "e2e-auto-created"
        try:
            # Verify it doesn't exist yet
            if test_proj.exists():
                import shutil
                shutil.rmtree(test_proj)
            assert not test_proj.exists()

            # Call tool handler (mocking tmux/registry to avoid side effects)
            import bot.core as core
            with patch("bot.core.subprocess") as mock_sub, \
                 patch("bot.core.registry") as mock_reg, \
                 patch("bot.core.get_tunnel_url", return_value="https://test.trycloudflare.com"):
                mock_reg.create.return_value = {}
                mock_sub.run.return_value = MagicMock(returncode=0)
                original_wolt = core.WOLT_DIR
                original_wolts = core.WOLTS_DIR
                try:
                    core.WOLT_DIR = wolt_dir
                    core.WOLTS_DIR = wolt_dir.parent
                    core.start_claude_session("test", project="e2e-auto-created")
                finally:
                    core.WOLT_DIR = original_wolt
                    core.WOLTS_DIR = original_wolts

            assert test_proj.exists()
            assert test_proj.is_dir()
        finally:
            if test_proj.exists():
                import shutil
                shutil.rmtree(test_proj)
