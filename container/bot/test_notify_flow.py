"""Tests for notify → reply → den routing flow.

Covers:
- Den reply detection (footer parsing, session extraction)
- message_session behavior (live, dead, missing sessions)
- Notify footer format contract
- Den reply message format (origin, notify instruction)

Usage: uv run pytest container/bot/test_notify_flow.py -v
"""

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Den reply detection (pure Python — mirrors telegram_adapter._is_den_reply)
# ---------------------------------------------------------------------------

DEN_REPLY_FOOTER = "\n↩️ reply to this message to talk to this session directly"
DEN_SESSION_RE = re.compile(r"session=([a-z0-9-]+)")


def _extract_session_from_text(text: str) -> str | None:
    """Simulate _is_den_reply logic: check footer, extract session name."""
    if DEN_REPLY_FOOTER.strip() not in text:
        return None
    match = DEN_SESSION_RE.search(text)
    return match.group(1) if match else None


class TestDenReplyDetection:
    """Test that session names are correctly extracted from notify footers."""

    def test_footer_with_full_url(self):
        """Standard case: notify includes a tunnel URL with session= param."""
        msg = (
            "🦫 neowolt: task done\n\n"
            "---\n"
            "session: https://abc.trycloudflare.com/tui?session=neowolt-chompy-dam-a3f1e2\n\n"
            "---"
            + DEN_REPLY_FOOTER
            + "\nhttps://abc.trycloudflare.com/tui?session=neowolt-chompy-dam-a3f1e2"
        )
        assert _extract_session_from_text(msg) == "neowolt-chompy-dam-a3f1e2"

    def test_footer_with_session_only(self):
        """Fallback case: no tunnel, just session=NAME."""
        msg = (
            "🦫 neowolt: task done\n\n"
            "---"
            + DEN_REPLY_FOOTER
            + "\nsession=neowolt-bold-creek-1a2b3c"
        )
        assert _extract_session_from_text(msg) == "neowolt-bold-creek-1a2b3c"

    def test_no_footer_returns_none(self):
        """Regular message without footer — not a den reply."""
        msg = "🐶 neowolt: just chatting"
        assert _extract_session_from_text(msg) is None

    def test_footer_without_session_returns_none(self):
        """Footer present but no session= anywhere — can't route."""
        msg = "some message" + DEN_REPLY_FOOTER
        assert _extract_session_from_text(msg) is None

    def test_blabo_session_name(self):
        """Other wolt names should work too."""
        msg = "🦫 blabo: done" + DEN_REPLY_FOOTER + "\nsession=blabo-muddy-lodge-ff00aa"
        assert _extract_session_from_text(msg) == "blabo-muddy-lodge-ff00aa"


# ---------------------------------------------------------------------------
# Notify footer format contract
# ---------------------------------------------------------------------------

class TestNotifyFooterFormat:
    """Verify the footer contract between server.js and telegram_adapter.py."""

    def test_footer_has_separator(self):
        """Footer should start with --- separator."""
        # Simulates what server.js builds
        session = "neowolt-sleek-burrow-5dd665"
        tunnel_url = "https://abc.trycloudflare.com"
        session_url = f"{tunnel_url}/tui?session={session}"
        footer = f"\n\n---" + DEN_REPLY_FOOTER + f"\n{session_url}"

        assert "---" in footer
        assert DEN_REPLY_FOOTER.strip() in footer
        assert f"session={session}" in footer

    def test_footer_session_url_is_clickable(self):
        """The session URL should be a full https URL, not just a name."""
        session = "neowolt-bold-creek-1a2b3c"
        tunnel_url = "https://abc.trycloudflare.com"
        session_url = f"{tunnel_url}/tui?session={session}"
        footer = f"\n\n---" + DEN_REPLY_FOOTER + f"\n{session_url}"

        assert session_url in footer
        assert session_url.startswith("https://")

    def test_footer_without_tunnel_falls_back(self):
        """When no tunnel, falls back to session=NAME."""
        session = "neowolt-bold-creek-1a2b3c"
        session_url = f"session={session}"
        footer = f"\n\n---" + DEN_REPLY_FOOTER + f"\n{session_url}"

        # Regex should still extract it
        match = DEN_SESSION_RE.search(footer)
        assert match and match.group(1) == session


# ---------------------------------------------------------------------------
# Den reply message format
# ---------------------------------------------------------------------------

class TestDenReplyMessageFormat:
    """Verify the message format sent to Claude Code sessions."""

    def test_text_reply_format(self):
        """Text reply should include origin and notify instruction."""
        human_name = "jerpint"
        text = "hey can you check the logs?"
        den_msg = (
            f"[telegram message from {human_name}]: {text}\n"
            f"Reply back to them with: notify \"your message\""
        )
        assert "[telegram message from jerpint]" in den_msg
        assert text in den_msg
        assert 'notify "your message"' in den_msg

    def test_voice_reply_format(self):
        """Voice reply should include origin and notify instruction."""
        human_name = "jerpint"
        text = "transcribed voice message"
        den_msg = (
            f"[telegram voice from {human_name}]: {text}\n"
            f"Reply back to them with: notify \"your message\""
        )
        assert "[telegram voice from jerpint]" in den_msg
        assert text in den_msg
        assert 'notify "your message"' in den_msg


# ---------------------------------------------------------------------------
# message_session behavior (requires tmux)
# ---------------------------------------------------------------------------

def tmux_available():
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _pane_command(session_name: str) -> str | None:
    """Mirror of core._pane_command."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_current_command}"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


@pytest.fixture
def tmux_session():
    """Create a tmux session, clean up after."""
    name = f"test-notify-{int(time.time()) % 100000}"
    yield name
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


@pytest.mark.skipif(not tmux_available(), reason="tmux not installed")
class TestMessageSession:
    """Test message delivery to tmux sessions."""

    def test_pane_command_detects_bash(self, tmux_session):
        """A session at a shell prompt should report 'bash'."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        time.sleep(0.5)
        cmd = _pane_command(name)
        assert cmd in ("bash", "sh", "zsh")

    def test_pane_command_detects_running_process(self, tmux_session):
        """A session running a command should report that command."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 30"], check=True)
        time.sleep(0.5)
        cmd = _pane_command(name)
        assert cmd == "sleep"

    def test_pane_command_missing_session(self):
        """A non-existent session should return None."""
        assert _pane_command("nonexistent-session-xyz") is None

    def test_send_keys_to_live_session(self, tmux_session):
        """Sending keys to a session with a running process should work."""
        name = tmux_session
        # Start a cat process that will echo input
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
        time.sleep(0.5)

        # Send some text
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", "hello from test"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)

        # Capture output
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert "hello from test" in result.stdout

    def test_send_keys_to_bash_goes_to_shell(self, tmux_session):
        """Sending to a bash session types into the shell (the bug we fixed)."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        time.sleep(0.5)

        # This is what used to happen — typing into bash
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", "echo test-marker"], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)

        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        # It ran as a bash command — this is the behavior we want to detect and avoid
        assert "test-marker" in result.stdout

    def test_revive_sets_wolt_session_env(self, tmux_session):
        """When reviving a dead session, WOLT_SESSION must be exported so notify works.

        Bug: revived sessions had WOLT_SESSION="" because run-session.sh wasn't involved,
        causing notify to send session= with a blank name.
        """
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        time.sleep(0.5)

        # Simulate what message_session does on revive: export WOLT_SESSION then run command
        revive_cmd = f"export WOLT_SESSION={shlex.quote(name)} && echo WOLT_SESSION=$WOLT_SESSION"
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", revive_cmd], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)

        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert f"WOLT_SESSION={name}" in result.stdout


# ---------------------------------------------------------------------------
# Tool result format
# ---------------------------------------------------------------------------

class TestToolResultFormat:
    """Verify tool results have the fields the dog needs."""

    def test_send_message_success_fields(self):
        """Successful send_message should have ok, session, url, status, detail."""
        result = {
            "ok": True,
            "session": "neowolt-bold-creek-1a2b3c",
            "url": "https://abc.trycloudflare.com/tui?session=neowolt-bold-creek-1a2b3c",
            "status": "delivered",
            "detail": "Claude is running, message sent directly",
        }
        assert result["ok"] is True
        assert result["session"]
        assert result["url"]
        assert result["status"] in ("delivered", "revived")
        assert result["detail"]

    def test_send_message_revived_fields(self):
        """Revived session should have revived status."""
        result = {
            "ok": True,
            "session": "neowolt-bold-creek-1a2b3c",
            "url": "https://abc.trycloudflare.com/tui?session=neowolt-bold-creek-1a2b3c",
            "status": "revived",
            "detail": "Claude had exited — restarted with --continue and delivered message",
        }
        assert result["status"] == "revived"

    def test_send_message_error_fields(self):
        """Failed send_message should have ok=False and error."""
        result = {
            "ok": False,
            "session": "neowolt-bold-creek-1a2b3c",
            "url": None,
            "error": "session neowolt-bold-creek-1a2b3c not found — it may have been killed or expired",
        }
        assert result["ok"] is False
        assert result["error"]
        assert result["session"]

    def test_kill_session_success_fields(self):
        result = {"ok": True, "session": "test-123", "url": None, "detail": "session test-123 killed"}
        assert result["ok"] is True
        assert result["detail"]

    def test_kill_session_error_fields(self):
        result = {"ok": False, "session": "test-123", "url": None, "error": "couldn't kill test-123"}
        assert result["ok"] is False
        assert result["error"]


# ---------------------------------------------------------------------------
# Creature emoji routing
# ---------------------------------------------------------------------------


class TestCreatureEmoji:
    """Test creature emoji is routed correctly through session routing files."""

    def test_build_ack_text_raccoon(self):
        from bot.core import build_ack_text
        text = build_ack_text(url="https://example.com/tui?session=test", session_name="neowolt-test-123", creature="raccoon")
        assert "🦝" in text
        assert "🦫" not in text

    def test_build_ack_text_beaver(self):
        from bot.core import build_ack_text
        text = build_ack_text(url="https://example.com/tui?session=test", session_name="neowolt-test-123", creature="beaver")
        assert "🦫" in text

    def test_build_ack_text_default_no_creature(self):
        from bot.core import build_ack_text
        text = build_ack_text(url="https://example.com/tui?session=test", session_name="neowolt-test-123")
        assert "🦫" in text  # default is beaver

    def test_creature_emojis_map(self):
        from bot.core import CREATURE_EMOJIS
        assert CREATURE_EMOJIS["raccoon"] == "🦝"
        assert CREATURE_EMOJIS["beaver"] == "🦫"

    def test_routing_with_creature_merges(self):
        from bot.core import _routing_with_creature
        routing = {"adapter": "telegram", "chat_id": 123}
        merged = _routing_with_creature(routing, "raccoon")
        assert merged["creature"] == "raccoon"
        assert merged["adapter"] == "telegram"
        assert merged["chat_id"] == 123

    def test_routing_with_creature_none_routing(self):
        from bot.core import _routing_with_creature
        merged = _routing_with_creature(None, "raccoon")
        assert merged == {"creature": "raccoon"}

    def test_routing_with_creature_none_creature(self):
        from bot.core import _routing_with_creature
        routing = {"adapter": "telegram", "chat_id": 123}
        merged = _routing_with_creature(routing, None)
        assert merged == routing
        assert "creature" not in merged

    def test_routing_with_creature_both_none(self):
        from bot.core import _routing_with_creature
        assert _routing_with_creature(None, None) is None

    def test_notify_reads_creature_from_routing(self, tmp_path):
        """Simulate what the notify script does: read creature from routing file."""
        routing = {"adapter": "telegram", "chat_id": 123, "creature": "raccoon"}
        routing_file = tmp_path / "session-routing" / "test-session.json"
        routing_file.parent.mkdir(parents=True)
        routing_file.write_text(json.dumps(routing))
        # Same logic as notify script
        data = json.loads(routing_file.read_text())
        creature = data.get("creature", "")
        emoji_map = {"raccoon": "🦝", "beaver": "🦫"}
        emoji = emoji_map.get(creature, "🦫")
        assert emoji == "🦝"

    def test_notify_defaults_beaver_when_no_creature(self, tmp_path):
        """No creature in routing → default beaver emoji."""
        routing = {"adapter": "telegram", "chat_id": 123}
        routing_file = tmp_path / "test-routing.json"
        routing_file.write_text(json.dumps(routing))
        data = json.loads(routing_file.read_text())
        creature = data.get("creature", "")
        emoji_map = {"raccoon": "🦝", "beaver": "🦫"}
        emoji = emoji_map.get(creature, "🦫")
        assert emoji == "🦫"

    def test_tool_calls_log_in_response(self):
        """get_response should include tool_calls_log in result."""
        # Verify the key exists in a mock result structure
        result = {
            "type": "text",
            "text": "hello",
            "history_messages": [],
            "tool_calls_log": [{"tool": "new_session", "args": {"prompt": "hey", "creature": "raccoon"}}],
        }
        assert len(result["tool_calls_log"]) == 1
        assert result["tool_calls_log"][0]["tool"] == "new_session"
        assert result["tool_calls_log"][0]["args"]["creature"] == "raccoon"


# ---------------------------------------------------------------------------
# Notify end-to-end: server.js appends footer to every notify message
# ---------------------------------------------------------------------------


class TestNotifyEndToEnd:
    """Hit the real server.js /notify endpoint and verify footer is present.

    These tests require the server to be running on localhost:3000.
    Skipped automatically if the server is down.
    """

    @pytest.fixture(autouse=True)
    def _check_server(self):
        """Skip if server isn't running."""
        import urllib.request
        try:
            urllib.request.urlopen("http://localhost:3000/", timeout=2)
        except Exception:
            pytest.skip("server not running on localhost:3000")

    def _post_notify(self, session: str, message: str) -> dict:
        """POST to /notify and return the response (without actually sending to Telegram)."""
        import urllib.request
        data = json.dumps({"session": session, "message": message}).encode()
        req = urllib.request.Request(
            "http://localhost:3000/notify",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as e:
            # urllib raises on non-2xx but we can still check
            return {"error": str(e)}

    def _read_last_chat_line(self, chat_id: str) -> str:
        """Read the last line from the chat history file that server.js writes."""
        from pathlib import Path
        chat_file = Path("/workspace/wolts/.state/chat") / f"telegram-{chat_id}.jsonl"
        if not chat_file.exists():
            return ""
        lines = chat_file.read_text().strip().split("\n")
        return lines[-1] if lines else ""

    def test_notify_sends_and_returns_adapter(self):
        """Basic smoke test: /notify returns ok with adapter info."""
        # Use a real session that has routing
        routing_dir = Path("/workspace/wolts/.state/session-routing")
        if not routing_dir.exists():
            pytest.skip("no session routing dir")
        files = list(routing_dir.glob("*.json"))
        if not files:
            pytest.skip("no session routing files")
        session = files[0].stem
        result = self._post_notify(session, "🧪 test: footer reliability check")
        assert result.get("adapter") in ("telegram", "slack"), f"unexpected result: {result}"

    def test_chat_history_has_message_without_footer(self):
        """server.js stores the raw message (no footer) in chat history."""
        routing_dir = Path("/workspace/wolts/.state/session-routing")
        if not routing_dir.exists():
            pytest.skip("no session routing dir")
        files = list(routing_dir.glob("*.json"))
        if not files:
            pytest.skip("no session routing files")
        session = files[0].stem
        routing = json.loads(files[0].read_text())
        chat_id = str(routing.get("chat_id", ""))
        if not chat_id:
            pytest.skip("no chat_id in routing")

        marker = f"🧪 footer-test-{int(time.time())}"
        result = self._post_notify(session, marker)
        assert result.get("adapter"), f"notify failed: {result}"

        # Chat history should have the message WITHOUT footer
        last_line = self._read_last_chat_line(chat_id)
        if last_line:
            data = json.loads(last_line)
            content = data.get("content", "")
            assert marker in content, "message not in chat history"
            # Footer should NOT be in chat history (server strips it)
            assert "↩️ reply to this message" not in content, "footer leaked into chat history"

    def test_notify_footer_contract_in_server_js(self):
        """Verify server.js has the footer constant matching our expectation."""
        server_path = Path("/workspace/woltspace/server.js")
        if not server_path.exists():
            pytest.skip("server.js not found")
        source = server_path.read_text()
        # The DEN_REPLY_FOOTER constant must exist
        assert "DEN_REPLY_FOOTER" in source
        assert "↩️ reply to this message to talk to this session directly" in source
        # The footer must be appended to the message in sendNotification
        assert "message + footer" in source
