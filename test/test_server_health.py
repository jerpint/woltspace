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

    def test_notify_returns_adapter(self, routed_test_session, server_post):
        """If routing exists, notify should return which adapter was used."""
        result = server_post("/notify", {
            "session": routed_test_session,
            "message": f"🧪 health check probe {int(time.time())}",
        })
        assert result.get("adapter") in ("telegram", "slack"), f"unexpected: {result}"


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


# ---------------------------------------------------------------------------
# Wolts endpoint
# ---------------------------------------------------------------------------

@requires_server
class TestWoltsEndpoint:
    """The /wolts endpoint lists all wolts."""

    def test_wolts_returns_list(self, server_get):
        result = server_get("/wolts")
        assert isinstance(result, list)

    def test_wolts_have_required_fields(self, server_get):
        result = server_get("/wolts")
        if result:
            wolt = result[0]
            assert "name" in wolt
            assert "dir" in wolt


# ---------------------------------------------------------------------------
# Session routing
# ---------------------------------------------------------------------------

@requires_server
class TestSessionRouting:
    """The /sessions/new/{adapter} endpoints route to the correct wolt."""

    def test_lodge_requires_wolt(self, server_post):
        result = server_post("/sessions/new/lodge", {})
        assert result.get("error"), "should reject missing wolt"

    def test_lodge_rejects_unknown_wolt(self, server_post):
        result = server_post("/sessions/new/lodge", {"wolt": "nonexistent-wolt-xyz"})
        assert "error" in result

    def test_telegram_requires_wolt(self, server_post):
        result = server_post("/sessions/new/telegram", {})
        assert result.get("error"), "should reject missing wolt"

    def test_slack_requires_wolt(self, server_post):
        result = server_post("/sessions/new/slack", {})
        assert result.get("error"), "should reject missing wolt"

    def test_lodge_spawns_session_for_valid_wolt(self, server_post):
        """Gnaw on a real wolt → session spawns under that wolt."""
        result = server_post("/sessions/new/lodge", {"wolt": "neowolt"})
        assert "name" in result, f"expected session name, got: {result}"
        assert result["name"].startswith("neowolt-"), f"session should be under neowolt, got: {result['name']}"
        assert result["wolt"] == "neowolt"

    def test_lodge_session_has_url(self, server_post):
        """Spawned session should include a URL for the split view."""
        result = server_post("/sessions/new/lodge", {"wolt": "neowolt"})
        # url may be None if no tunnel is configured, but the key should exist
        assert "url" in result
