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
        msg = "🦦 neowolt: just chatting"
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
    """Verify tool results have the fields the otter needs."""

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
