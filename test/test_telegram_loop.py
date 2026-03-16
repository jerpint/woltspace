"""Telegram bot loop tests — simulate the full user→bot→session→notify cycle.

This is the "agent unit test loop" from jerpint's airplane-mode notes:
simulate a Telegram user interacting with the bot, verify the full chain works.

Three tiers:
1. Mock tests (no external deps) — test message parsing, routing logic
2. Server tests (require localhost:7777) — test notify, session creation
3. Live bot tests (require TELEGRAM_BOT_TOKEN) — actual Telegram API calls

Usage: uv run pytest test/test_telegram_loop.py -v
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from conftest import requires_server, requires_telegram


# ---------------------------------------------------------------------------
# Tier 1: Mock tests — message parsing and routing logic
# ---------------------------------------------------------------------------

class TestDenReplyDetection:
    """Test detection of replies to den (session) messages."""

    DEN_REPLY_FOOTER = "\n↩️ reply to this message to talk to this session directly"
    DEN_SESSION_RE = re.compile(r"session=([a-z0-9-]+)")

    def _extract_session(self, text: str) -> str | None:
        if self.DEN_REPLY_FOOTER.strip() not in text:
            return None
        match = self.DEN_SESSION_RE.search(text)
        return match.group(1) if match else None

    def test_full_url_with_footer(self):
        msg = (
            "🦫 neowolt: done\n\n---\n"
            "session: https://abc.trycloudflare.com/tui?session=neowolt-chompy-dam-a3f1e2\n\n---"
            + self.DEN_REPLY_FOOTER
            + "\nhttps://abc.trycloudflare.com/tui?session=neowolt-chompy-dam-a3f1e2"
        )
        assert self._extract_session(msg) == "neowolt-chompy-dam-a3f1e2"

    def test_no_footer(self):
        assert self._extract_session("just a regular message") is None

    def test_footer_no_session(self):
        msg = "some text" + self.DEN_REPLY_FOOTER
        assert self._extract_session(msg) is None

    def test_multiple_session_names(self):
        """Should match the first session= in the text."""
        msg = (
            "session=first-abc123 and session=second-def456"
            + self.DEN_REPLY_FOOTER
        )
        assert self._extract_session(msg) == "first-abc123"


class TestResponseFormatting:
    """Test how bot responses are formatted for Telegram."""

    def test_text_response_format(self):
        from bot.telegram_adapter import format_response
        with patch.dict(os.environ, {"WOLT_NAME": "neowolt"}):
            result = {"type": "text", "text": "hello world"}
            formatted = format_response(result)
            assert "🐶" in formatted
            assert "neowolt" in formatted
            assert "hello world" in formatted

    def test_session_response_with_text(self):
        from bot.telegram_adapter import format_response
        with patch.dict(os.environ, {"WOLT_NAME": "neowolt"}):
            result = {
                "type": "session",
                "text": "session started — chompy dam",
                "session": {"name": "neowolt-test-123", "url": "https://example.com"},
            }
            formatted = format_response(result)
            assert "session started" in formatted

    def test_image_response_format(self):
        from bot.telegram_adapter import format_response
        with patch.dict(os.environ, {"WOLT_NAME": "neowolt"}):
            result = {"type": "image", "text": "cool image", "path": "/tmp/img.png"}
            formatted = format_response(result)
            assert "cool image" in formatted


class TestToolCallLogging:
    """Test the tool call log formatting."""

    def test_format_tool_log_new_session(self):
        from bot.telegram_adapter import _format_tool_log
        tc = {
            "tool": "new_session",
            "creature": "raccoon",
            "args": {"wolt": "neowolt", "creature": "raccoon"},
            "url": "https://example.com/tui?session=test",
        }
        line = _format_tool_log(tc)
        assert "🪵" in line
        assert "🦝" in line
        assert "new_session" in line
        assert "https://example.com" in line

    def test_format_tool_log_no_creature(self):
        from bot.telegram_adapter import _format_tool_log
        tc = {"tool": "list_sessions", "args": {}}
        line = _format_tool_log(tc)
        assert "🪵" in line
        assert "list_sessions" in line


class TestAllowedUsers:
    """Test user allowlist logic."""

    def test_no_allowlist_means_open(self):
        from bot.telegram_adapter import ALLOWED_USERS
        # When empty, all users should be allowed
        ALLOWED_USERS.clear()
        mock_update = MagicMock()
        mock_update.effective_user.id = 99999
        from bot.telegram_adapter import is_allowed
        assert is_allowed(mock_update) is True

    def test_allowlist_blocks_unknown(self):
        from bot.telegram_adapter import ALLOWED_USERS, is_allowed
        ALLOWED_USERS.clear()
        ALLOWED_USERS.add(12345)
        mock_update = MagicMock()
        mock_update.effective_user.id = 99999
        assert is_allowed(mock_update) is False
        ALLOWED_USERS.clear()  # cleanup

    def test_allowlist_permits_known(self):
        from bot.telegram_adapter import ALLOWED_USERS, is_allowed
        ALLOWED_USERS.clear()
        ALLOWED_USERS.add(12345)
        mock_update = MagicMock()
        mock_update.effective_user.id = 12345
        assert is_allowed(mock_update) is True
        ALLOWED_USERS.clear()


# ---------------------------------------------------------------------------
# Tier 2: Server integration — notify round-trip
# ---------------------------------------------------------------------------

@requires_server
class TestNotifyRoundTrip:
    """Test the notify→server→telegram chain."""

    def test_notify_script_exists(self):
        """The notify binary should be on PATH or at the known location."""
        notify_path = Path("/workspace/woltspace/container/bin/notify")
        assert notify_path.exists(), "notify script missing"
        assert os.access(notify_path, os.X_OK), "notify not executable"

    def test_server_notify_json_contract(self, routed_test_session, server_post):
        """The /notify endpoint should accept {session, message} and return {ok/error, adapter}."""
        result = server_post("/notify", {
            "session": routed_test_session,
            "message": "contract check",
        })
        # Should return a dict with either ok/adapter or error
        assert isinstance(result, dict)
        # Must have one of these keys
        assert any(k in result for k in ("ok", "error", "adapter")), f"unexpected shape: {result}"


# ---------------------------------------------------------------------------
# Tier 3: Live Telegram API tests
# ---------------------------------------------------------------------------

@requires_telegram
class TestTelegramAPI:
    """Tests that hit the real Telegram API. Require TELEGRAM_BOT_TOKEN."""

    def test_bot_token_valid(self):
        """Verify the bot token works by calling getMe."""
        import urllib.request
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        url = f"https://api.telegram.org/bot{token}/getMe"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())
            assert data["ok"] is True
            assert "result" in data
            assert "username" in data["result"]

    def test_bot_can_get_updates(self):
        """Bot should be able to fetch recent updates (even if empty)."""
        import urllib.request
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=1&timeout=1"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
            assert data["ok"] is True
            assert isinstance(data["result"], list)
