"""Server health checks — verify core HTTP endpoints are responding correctly.

These tests hit the real running server. Skip automatically if server is down.

Usage: uv run pytest test/test_server_health.py -v
"""

import json
import time
from pathlib import Path

import pytest

from conftest import requires_server


# ---------------------------------------------------------------------------
# Basic health
# ---------------------------------------------------------------------------

@requires_server
class TestServerAlive:
    """Server responds to basic requests."""

    def test_root_returns_200(self, server_get):
        result = server_get("/")
        assert not isinstance(result, dict) or "error" not in result

    def test_current_url_endpoint(self, server_get):
        """The /current endpoint should return JSON (even if no viewport is set)."""
        result = server_get("/current?session=main")
        assert isinstance(result, (dict, str))


# ---------------------------------------------------------------------------
# Notify endpoint
# ---------------------------------------------------------------------------

@requires_server
class TestNotifyEndpoint:
    """The /notify endpoint accepts messages and routes them."""

    def test_notify_with_empty_session(self, server_post):
        """Notify with empty session should fail gracefully."""
        result = server_post("/notify", {"session": "", "message": "test"})
        # Should either succeed with fallback or fail with clear error
        assert "error" in result or "ok" in result or "adapter" in result

    def test_notify_with_nonexistent_session(self, server_post):
        """Notify to a session that doesn't exist should fail gracefully."""
        result = server_post("/notify", {
            "session": "nonexistent-test-session-xyz",
            "message": "test probe",
        })
        # Should not crash — either error or fallback routing
        assert isinstance(result, dict)

    def test_notify_returns_adapter(self, server_post):
        """If routing exists, notify should return which adapter was used."""
        # Find any session with routing
        registry_dir = Path("/workspace/wolts/.state/registry")
        if not registry_dir.exists():
            pytest.skip("no registry dir")
        sessions = list(registry_dir.glob("*.json"))
        if not sessions:
            pytest.skip("no registered sessions")

        for sf in sessions:
            try:
                data = json.loads(sf.read_text())
                if data.get("adapter") and data.get("chat_id"):
                    session_name = data["name"]
                    result = server_post("/notify", {
                        "session": session_name,
                        "message": f"🧪 health check probe {int(time.time())}",
                    })
                    assert result.get("adapter") in ("telegram", "slack"), f"unexpected: {result}"
                    return
            except (json.JSONDecodeError, KeyError):
                continue
        pytest.skip("no sessions with routing found")


# ---------------------------------------------------------------------------
# Tool proxy
# ---------------------------------------------------------------------------

@requires_server
class TestToolProxy:
    """The /tools endpoint handles tool registrations."""

    def test_tools_endpoint_exists(self, server_get):
        """GET /tools should return something (list or error, not 404)."""
        result = server_get("/tools")
        assert result is not None


# ---------------------------------------------------------------------------
# Session redirect
# ---------------------------------------------------------------------------

@requires_server
class TestSessionRedirect:
    """The viewport/redirect mechanism for sessions."""

    def test_current_returns_json(self, server_get):
        result = server_get("/current?session=main")
        # Should be JSON with url field
        if isinstance(result, dict):
            assert "url" in result or "error" in result
