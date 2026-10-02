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

from conftest import (
    requires_live_send,
    requires_live_telegram,
    requires_server,
    requires_telegram,
)


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


class TestReplyToRouting:
    """Test reply-to session parsing and wolt extraction from adapter code."""

    def test_parse_session_from_full_url(self):
        from bot.telegram_adapter import _parse_session_from_reply
        msg = (
            "🦦 nunu: here's your playlist!\n\n"
            "---\n"
            "↩️ reply to this message to talk to this session directly\n"
            "https://abc.trycloudflare.com/tui?session=nunu-swift-marsh-a1b2c3"
        )
        assert _parse_session_from_reply(msg) == "nunu-swift-marsh-a1b2c3"

    def test_parse_session_no_footer(self):
        from bot.telegram_adapter import _parse_session_from_reply
        assert _parse_session_from_reply("just a regular message") is None

    def test_parse_session_footer_no_url(self):
        from bot.telegram_adapter import _parse_session_from_reply
        msg = "some text\n↩️ reply to this message to talk to this session directly"
        assert _parse_session_from_reply(msg) is None

    def test_parse_session_fallback_format(self):
        from bot.telegram_adapter import _parse_session_from_reply
        msg = (
            "🦫 neowolt: done\n\n"
            "---\n"
            "↩️ reply to this message to talk to this session directly\n"
            "session=neowolt-chompy-dam-a3f1e2"
        )
        assert _parse_session_from_reply(msg) == "neowolt-chompy-dam-a3f1e2"

    def test_wolt_from_session_name(self):
        from bot.telegram_adapter import _wolt_from_session_name
        assert _wolt_from_session_name("nunu-swift-marsh-a1b2c3") == "nunu"
        assert _wolt_from_session_name("uxwolt-scrappy-log-02e806") == "uxwolt"
        assert _wolt_from_session_name("neowolt-chompy-dam-a3f1e2") == "neowolt"

    def test_wolt_from_session_name_empty(self):
        from bot.telegram_adapter import _wolt_from_session_name
        assert _wolt_from_session_name("") is None
        assert _wolt_from_session_name(None) is None

    def test_wolt_from_session_name_unexpected_format(self):
        from bot.telegram_adapter import _wolt_from_session_name
        # Not enough segments
        assert _wolt_from_session_name("just-two") is None

    @pytest.mark.asyncio
    async def test_reply_routes_to_correct_session(self):
        """Replying to a wolt message routes to that wolt's session."""
        from bot.telegram_adapter import handle_message

        reply_msg_text = (
            "🦦 nunu: here's your playlist!\n\n"
            "---\n"
            "↩️ reply to this message to talk to this session directly\n"
            "https://abc.trycloudflare.com/tui?session=nunu-swift-marsh-a1b2c3"
        )

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 99999
        update.effective_chat.type = "private"
        update.message.text = "love this track"
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.text = reply_msg_text
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.username = "testbot"

        with patch("bot.telegram_adapter.is_allowed", return_value=True), \
             patch("bot.telegram_adapter._is_session_alive", return_value=True), \
             patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock, return_value=True) as mock_route:
            await handle_message(update, context)
            mock_route.assert_called_once()
            # Verify it routed to nunu's session, not the active wolt
            call_args = mock_route.call_args
            assert call_args[0][1] == "nunu-swift-marsh-a1b2c3"  # session name
            assert call_args[0][2] == "nunu"  # wolt name
            # Verify reply context is included
            assert "[replying to:" in call_args[0][3]

    @pytest.mark.asyncio
    async def test_reply_without_footer_falls_through(self):
        """Replying to a message without footer uses regular routing."""
        from bot.telegram_adapter import handle_message

        update = MagicMock()
        update.effective_user.id = 12345
        update.effective_chat.id = 99999
        update.effective_chat.type = "private"
        update.message.text = "hey"
        update.message.reply_to_message = MagicMock()
        update.message.reply_to_message.text = "some random message without footer"
        update.message.reply_text = AsyncMock()

        context = MagicMock()
        context.bot.username = "testbot"

        with patch("bot.telegram_adapter.is_allowed", return_value=True), \
             patch("bot.telegram_adapter._load_chat_state", return_value={"active_wolt": "uxwolt", "active_session": "uxwolt-test-123-abc"}), \
             patch("bot.telegram_adapter._is_session_alive", return_value=True), \
             patch("bot.telegram_adapter._route_to_session", new_callable=AsyncMock, return_value=True) as mock_route:
            await handle_message(update, context)
            # Should route to active wolt (uxwolt), not try reply routing
            call_args = mock_route.call_args
            assert call_args[0][1] == "uxwolt-test-123-abc"


class TestResponseFormatting:
    """Test how bot responses are formatted for Telegram."""

    def test_text_response_format(self):
        from bot.telegram_adapter import format_response
        with patch("bot.telegram_adapter._dog_name", return_value="testdog"):
            result = {"type": "text", "text": "hello world"}
            formatted = format_response(result)
            assert "🐶" in formatted
            assert "testdog" in formatted
            assert "hello world" in formatted

    def test_session_response_with_text(self):
        from bot.telegram_adapter import format_response
        with patch.dict(os.environ, {"WOLTSPACE_WOLT_NAME": "neowolt"}):
            result = {
                "type": "session",
                "text": "session started — chompy dam",
                "session": {"name": "neowolt-test-123", "url": "https://example.com"},
            }
            formatted = format_response(result)
            assert "session started" in formatted

    def test_image_response_format(self):
        from bot.telegram_adapter import format_response
        with patch.dict(os.environ, {"WOLTSPACE_WOLT_NAME": "neowolt"}):
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

    def test_no_allowlist_means_deny_all(self):
        from bot.telegram_adapter import ALLOWED_USERS
        # When empty, all users should be denied (secure default)
        ALLOWED_USERS.clear()
        mock_update = MagicMock()
        mock_update.effective_user.id = 99999
        from bot.telegram_adapter import is_allowed
        assert is_allowed(mock_update) is False

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


class TestReplyRetry:
    """Test _reply() retry logic on TimedOut."""

    @pytest.mark.asyncio
    async def test_retry_on_timeout(self):
        """_reply retries once after TimedOut and succeeds."""
        from telegram.error import TimedOut
        from bot.telegram_adapter import _reply

        update = MagicMock()
        update.message.reply_text = AsyncMock(
            side_effect=[TimedOut("timed out"), MagicMock()]
        )
        await _reply(update, "hello")
        assert update.message.reply_text.call_count == 2

    @pytest.mark.asyncio
    async def test_success_no_retry(self):
        """_reply doesn't retry when reply_text succeeds."""
        from bot.telegram_adapter import _reply

        update = MagicMock()
        update.message.reply_text = AsyncMock(return_value=MagicMock())
        await _reply(update, "hello")
        assert update.message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_non_timeout_error_bubbles(self):
        """_reply doesn't retry on non-TimedOut errors."""
        from bot.telegram_adapter import _reply

        update = MagicMock()
        update.message.reply_text = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError, match="boom"):
            await _reply(update, "hello")
        assert update.message.reply_text.call_count == 1

    @pytest.mark.asyncio
    async def test_double_timeout_raises(self):
        """_reply raises if both attempts time out."""
        from telegram.error import TimedOut
        from bot.telegram_adapter import _reply

        update = MagicMock()
        update.message.reply_text = AsyncMock(
            side_effect=[TimedOut("first"), TimedOut("second")]
        )
        with pytest.raises(TimedOut):
            await _reply(update, "hello")
        assert update.message.reply_text.call_count == 2


class TestSessionsAliveFilter:
    """Test /sessions only returns alive sessions."""

    @pytest.mark.asyncio
    async def test_sessions_filters_dead(self):
        """handle_sessions only shows alive sessions."""
        from bot.telegram_adapter import handle_sessions

        sessions = [
            {"name": "wolt-a-1", "alive": True},
            {"name": "wolt-b-2", "alive": False},
            {"name": "wolt-c-3", "alive": True},
        ]
        update = MagicMock()
        update.effective_user.id = 99
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("bot.telegram_adapter.is_allowed", return_value=True), \
             patch("bot.telegram_adapter.list_sessions", return_value=sessions), \
             patch("bot.telegram_adapter.get_tunnel_url", return_value=None):
            await handle_sessions(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert "wolt-a-1" in reply_text
        assert "wolt-c-3" in reply_text
        assert "wolt-b-2" not in reply_text

    @pytest.mark.asyncio
    async def test_sessions_all_dead(self):
        """handle_sessions says 'no active sessions' when all are dead."""
        from bot.telegram_adapter import handle_sessions

        sessions = [
            {"name": "wolt-a-1", "alive": False},
        ]
        update = MagicMock()
        update.effective_user.id = 99
        update.message.reply_text = AsyncMock()
        context = MagicMock()

        with patch("bot.telegram_adapter.is_allowed", return_value=True), \
             patch("bot.telegram_adapter.list_sessions", return_value=sessions), \
             patch("bot.telegram_adapter.get_tunnel_url", return_value=None):
            await handle_sessions(update, context)

        reply_text = update.message.reply_text.call_args[0][0]
        assert reply_text == "no active sessions."


class TestErrorHandler:
    """Test global error handler behavior."""

    @pytest.mark.asyncio
    async def test_timeout_swallowed(self):
        """TimedOut errors are logged but not re-raised."""
        from telegram.error import TimedOut
        from bot.telegram_adapter import _error_handler

        context = MagicMock()
        context.error = TimedOut("timed out")
        # Should not raise
        await _error_handler(None, context)

    @pytest.mark.asyncio
    async def test_other_errors_logged(self):
        """Non-TimedOut errors are logged with exc_info."""
        from bot.telegram_adapter import _error_handler

        context = MagicMock()
        context.error = RuntimeError("unexpected")
        with patch("bot.telegram_adapter.logger") as mock_logger:
            await _error_handler(None, context)
            mock_logger.error.assert_called_once()


# ---------------------------------------------------------------------------
# Tier 2: Server integration — notify round-trip
# ---------------------------------------------------------------------------

class TestNotifySource:
    """Source contracts; installed artifact contracts are checked separately."""

    def test_notify_script_exists(self):
        """The notify binary should be on PATH or at the known location."""
        notify_path = (Path(__file__).resolve().parents[1] / "container/bin/notify")
        assert notify_path.exists(), "notify script missing"
        assert os.access(notify_path, os.X_OK), "notify not executable"


@requires_server
class TestNotifyRoundTrip:
    """Test the notify→server→telegram chain."""

    @requires_live_send
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
@requires_live_telegram
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


class TestOptionalDog:
    @pytest.mark.parametrize("provider,key", [
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("openai", "OPENAI_API_KEY"),
        ("openrouter", "OPENROUTER_API_KEY"),
        ("gemini", "GEMINI_API_KEY"),
    ])
    @pytest.mark.parametrize("dog,key_present,expected", [
        (None, True, False), ("doggo", True, True), ("doggo", False, False),
    ])
    def test_known_provider_availability(self, monkeypatch, provider, key, dog, key_present, expected):
        from bot import core
        monkeypatch.setattr(core, "get_active_creature", lambda kind: dog)
        monkeypatch.setattr(core, "LLM_MODEL", provider + "/model")
        monkeypatch.setenv(key, "test-key" if key_present else "")
        assert core.dog_available() is expected

    @pytest.mark.parametrize("provider", ["groq", "ollama", "custom"])
    @pytest.mark.parametrize("dog,expected", [(None, False), ("doggo", True)])
    def test_other_providers_preserve_behavior(self, monkeypatch, provider, dog, expected):
        from bot import core
        monkeypatch.setattr(core, "get_active_creature", lambda kind: dog)
        monkeypatch.setattr(core, "LLM_MODEL", provider + "/model")
        assert core.dog_available() is expected

    @pytest.fixture
    def routing(self, monkeypatch, tmp_path):
        from bot import telegram_adapter as adapter
        update = MagicMock()
        update.effective_chat.id = 123
        update.effective_chat.type = "private"
        update.message.text = "hello"
        update.message.reply_to_message = None
        update.message.reply_text = AsyncMock()
        context = MagicMock()
        context.bot.username = "testbot"
        state = {}
        monkeypatch.setattr(adapter, "is_allowed", lambda update: True)
        monkeypatch.setattr(adapter, "dog_available", lambda: False)
        monkeypatch.setattr(adapter, "_load_chat_state", lambda chat_id: dict(state))
        monkeypatch.setattr(adapter, "_save_chat_state", lambda chat_id, value: state.update(value))
        monkeypatch.setattr(adapter, "_bot_log", lambda *args: None)
        monkeypatch.setattr(adapter, "get_tunnel_url", lambda: "")
        monkeypatch.setattr(adapter, "list_wolts", lambda: [{"name": "nunu", "type": "otter"}])
        spawn = MagicMock(return_value={"name": "nunu-swift-marsh-abc123"})
        route = AsyncMock(return_value="nunu-swift-marsh-abc123")
        model = MagicMock(side_effect=AssertionError("No model call allowed"))
        monkeypatch.setattr(adapter, "_spawn_session", spawn)
        monkeypatch.setattr(adapter, "_route_to_session", route)
        monkeypatch.setattr(adapter, "get_response", model)
        monkeypatch.setattr(adapter, "_save_upload", lambda *args: tmp_path / "upload")
        return adapter, update, context, state, spawn, route, model

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mention", [False, True])
    async def test_one_wolt_routes_without_model(self, routing, mention):
        adapter, update, context, state, spawn, route, model = routing
        if mention:
            update.message.text = "@testbot hello"
            update.effective_chat.type = "group"
        await adapter.handle_message(update, context)
        spawn.assert_called_once_with("nunu", 123)
        route.assert_awaited_once_with(update, "nunu-swift-marsh-abc123", "nunu", "hello", 123)
        assert state["active_wolt"] == "nunu"
        assert state["active_session"] == "nunu-swift-marsh-abc123"
        model.assert_not_called()

    @pytest.mark.asyncio
    async def test_mention_keeps_active_wolt(self, routing):
        adapter, update, context, state, spawn, route, model = routing
        state.update(active_wolt="other", active_session="other-swift-marsh-123abc")
        update.message.text = "@testbot hello"
        await adapter.handle_message(update, context)
        spawn.assert_not_called()
        assert route.await_args.args[1:4] == ("other-swift-marsh-123abc", "other", "hello")
        model.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("builders,expected", [
        ([], "no wolts found. create a wolt in the lodge first."),
        ([{"name": "b", "type": "beaver"}, {"name": "a", "type": "raccoon"}],
         "your message was not delivered: pick a wolt above (or send /wolt <name>), "
         "then send it again."),
    ])
    async def test_selection_is_plain_text(self, routing, monkeypatch, builders, expected):
        adapter, update, context, state, spawn, route, model = routing
        monkeypatch.setattr(adapter, "list_wolts", lambda: builders + [
            {"name": "doggo", "type": "dog"}, {"name": "wolfie", "type": "wolf"}])
        await adapter.handle_message(update, context)
        if builders:
            calls = update.message.reply_text.await_args_list
            assert len(calls) == 2
            assert calls[0].args == (adapter._wolt_picker_header(builders, None),)
            # plain text: no parse mode, so a typed name can never break the message
            assert calls[0].kwargs == {
                "reply_markup": adapter._wolt_picker_keyboard(builders, None),
            }
            assert calls[1].args == (expected,)
            assert calls[1].kwargs == {}
        else:
            update.message.reply_text.assert_awaited_once_with(expected)
        spawn.assert_not_called()
        route.assert_not_called()
        model.assert_not_called()
        assert not state

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mention", [False, True])
    async def test_available_dog_uses_existing_model_path(self, routing, monkeypatch, mention):
        adapter, update, context, state, spawn, route, model = routing
        monkeypatch.setattr(adapter, "dog_available", lambda: True)
        monkeypatch.setattr(adapter, "_dog_ack", AsyncMock())
        monkeypatch.setattr(adapter, "_load_history", lambda chat_id: [])
        monkeypatch.setattr(adapter, "_append_history", lambda *args: None)
        send = AsyncMock()
        monkeypatch.setattr(adapter, "_send_result", send)
        model.side_effect = None
        model.return_value = {"history_messages": [], "type": "text", "text": "woof"}
        if mention:
            state.update(active_wolt="nunu", active_session="existing")
            update.message.text = "@testbot hello"
        await adapter.handle_message(update, context)
        model.assert_called_once_with("hello", conversation_history=[], routing={"adapter": "telegram", "chat_id": 123})
        send.assert_awaited_once_with(update, model.return_value)
        spawn.assert_not_called()
        route.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["voice", "photo", "document"])
    async def test_media_without_dog_routes_to_wolt(self, routing, monkeypatch, kind):
        adapter, update, context, state, spawn, route, model = routing
        file = MagicMock()
        file.download_to_drive = AsyncMock()
        file.download_as_bytearray = AsyncMock(return_value=b"content")
        context.bot.get_file = AsyncMock(return_value=file)
        update.message.caption = "caption"
        update.message.document.file_name = "note.txt"
        update.message.document.mime_type = "text/plain"
        monkeypatch.setattr(adapter, "transcribe_audio", lambda path: "hello")
        await getattr(adapter, "handle_" + kind)(update, context)
        spawn.assert_called_once_with("nunu", 123)
        route.assert_awaited_once()
        assert state["active_wolt"] == "nunu"
        model.assert_not_called()

    @pytest.mark.asyncio
    async def test_voice_missing_transcription_key_preserves_notice(self, routing, monkeypatch):
        adapter, update, context, state, spawn, route, model = routing
        file = MagicMock()
        file.download_to_drive = AsyncMock()
        context.bot.get_file = AsyncMock(return_value=file)
        monkeypatch.setenv("OPENAI_API_KEY", "")
        await adapter.handle_voice(update, context)
        spawn.assert_called_once_with("nunu", 123)
        route.assert_awaited_once()
        assert "no OPENAI_API_KEY set for transcription" in route.await_args.args[3]
        model.assert_not_called()


    @pytest.mark.asyncio
    @pytest.mark.parametrize("available,expected", [
        (False, "Hey. Send a message and it goes to your wolt. /wolt picks which one."),
        (True, "Hey. I'm doggo. Talk to me and I'll connect you to a wolt."),
    ])
    async def test_start_with_optional_dog(self, routing, monkeypatch, available, expected):
        adapter, update, context, state, spawn, route, model = routing
        monkeypatch.setattr(adapter, "dog_available", lambda: available)
        monkeypatch.setattr(adapter, "_dog_name", lambda: "doggo")
        await adapter.handle_start(update, context)
        update.message.reply_text.assert_awaited_once_with(expected)
        model.assert_not_called()

    @pytest.mark.asyncio
    async def test_same_wolt_new_session_switch_notice(self, routing, monkeypatch, tmp_path):
        adapter, update, context, state, spawn, route, model = routing
        monkeypatch.setattr(adapter, "_WOLTS_DIR", tmp_path)
        manifest = tmp_path / "nunu" / "wolt" / "wolt.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"type": "otter"}))
        await adapter._notify_switch(
            update, {"active_wolt": "nunu", "active_session": "old"}, "nunu", "new")
        emoji = adapter.CREATURE_EMOJIS["otter"]
        update.message.reply_text.assert_awaited_once_with(
            f"🪵 now talking to {emoji} nunu (new)")


class TestDisplayNamesInTelegram:
    """People read the display name; routing and commands keep the slug."""

    @pytest.fixture
    def wolts(self, monkeypatch, tmp_path):
        import bot.telegram_adapter as adapter

        def make(slug, creature, display_name=None):
            home = tmp_path / slug / "wolt"
            home.mkdir(parents=True)
            config = {"name": slug, "type": creature}
            if display_name:
                config["display_name"] = display_name
            (home / "wolt.json").write_text(json.dumps(config))
            return config

        monkeypatch.setattr(adapter, "_WOLTS_DIR", tmp_path)
        return adapter, [
            make("justin-beaver", "beaver", "Justin Beaver"),
            make("chip", "otter"),
            make("snake-case", "raccoon", "Sn_ake *Case*"),
        ]

    def test_picker_buttons_show_names_and_carry_slugs(self, wolts):
        adapter, listed = wolts
        keyboard = adapter._wolt_picker_keyboard(listed, None)
        buttons = [button for row in keyboard.inline_keyboard for button in row]
        assert [(b.text, b.callback_data) for b in buttons] == [
            ("🦫 Justin Beaver", "wolt:justin-beaver"),
            ("🦦 chip", "wolt:chip"),
            ("🦝 Sn_ake *Case*", "wolt:snake-case"),
        ]

    def test_picker_header_is_plain_text(self, wolts):
        adapter, listed = wolts
        # plain text: a typed name is shown as typed and needs no escaping
        assert adapter._wolt_picker_header(listed, "justin-beaver").startswith(
            "active: 🦫 Justin Beaver\n")
        assert adapter._wolt_picker_header(listed, "snake-case").startswith(
            "active: 🦝 Sn_ake *Case*\n")
        assert adapter._wolt_picker_header(listed, "chip").startswith("active: 🦦 chip\n")
        assert not hasattr(adapter, "_md")

    def test_shown_falls_back_to_the_slug(self, wolts):
        adapter, _ = wolts
        assert adapter._shown("justin-beaver") == "Justin Beaver"
        assert adapter._shown("chip") == "chip"
        assert adapter._shown("no-such-wolt") == "no-such-wolt"

    @pytest.mark.asyncio
    async def test_switch_notice_uses_the_display_name(self, wolts, monkeypatch):
        adapter, _ = wolts
        reply = AsyncMock()
        monkeypatch.setattr(adapter, "_reply", reply)
        await adapter._notify_switch(
            MagicMock(), {"active_wolt": "chip", "active_session": "old"},
            "justin-beaver", "justin-beaver-a-b-123456",
        )
        assert reply.await_args.args[1] == (
            "🪵 now talking to 🦫 Justin Beaver (justin-beaver-a-b-123456)"
        )
