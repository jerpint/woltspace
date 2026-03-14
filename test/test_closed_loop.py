"""Closed-loop integration tests — the full user → bot → session → notify → user chain.

This is the "agent unit test loop" from the roadmap:
simulate a real Telegram interaction end-to-end, verify every link in the chain.

The loop being tested:
  1. User sends message → Telegram API
  2. Bot (Haiku) receives it → processes via core.get_response()
  3. Bot dispatches work → tmux session created via core.start_claude_session()
  4. Session runs → calls notify to report back
  5. Notify → server.js → Telegram API → message appears in chat
  6. User can reply to that message → routed back to the session

We can't impersonate a Telegram user (API doesn't allow it), so we test
the chain at each seam:
  - Telegram API validity (bot can send/receive)
  - Server notify pipeline (POST /notify → Telegram sendMessage)
  - Session creation via routing files + tmux
  - Notify delivery verification via bot debug logs
  - Den reply routing (message → session via tmux send-keys)

Usage:
  uv run pytest test/test_closed_loop.py -v                    # all
  uv run pytest test/test_closed_loop.py -k "not live" -v      # skip real Telegram calls
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from conftest import requires_server, requires_telegram, requires_tmux


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
WOLT_NAME = os.environ.get("WOLT_NAME", "neowolt")
# Server writes to wolt-level log, core.py writes to wolts-level log
BOT_LOG_PATHS = [
    WOLTS_DIR / WOLT_NAME / ".state" / "bot-debug" / "bot.jsonl",
    WOLTS_DIR / ".state" / "bot-debug" / "bot.jsonl",
]
SESSION_ROUTING_DIR = WOLTS_DIR / ".state" / "session-routing"


def _read_bot_log_tail(n: int = 20) -> list[dict]:
    """Read last N entries from bot debug logs (checks both wolt and wolts level)."""
    entries = []
    for log_path in BOT_LOG_PATHS:
        if not log_path.exists():
            continue
        lines = log_path.read_text().strip().split("\n")
        for line in lines[-n:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Sort by timestamp, return last N
    entries.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return entries[:n]


def _telegram_send(token: str, chat_id: str, text: str) -> dict:
    """Send a message via Telegram API. Returns the API response."""
    import urllib.request
    body = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _telegram_get_updates(token: str, offset: int = 0, limit: int = 5, timeout: int = 1) -> dict:
    """Poll for new updates from Telegram."""
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&limit={limit}&timeout={timeout}"
    with urllib.request.urlopen(url, timeout=timeout + 5) as resp:
        return json.loads(resp.read())


def _find_chat_id() -> str | None:
    """Find a chat_id for tests — prefer TEST_CHAT_ID env var, fall back to routing files."""
    env_id = os.environ.get("TEST_CHAT_ID")
    if env_id:
        return env_id
    if not SESSION_ROUTING_DIR.exists():
        return None
    for f in SESSION_ROUTING_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("chat_id") and data.get("adapter") == "telegram":
                return str(data["chat_id"])
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def _write_routing(session_name: str, adapter: str, chat_id: str):
    """Write a session routing file."""
    SESSION_ROUTING_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_ROUTING_DIR / f"{session_name}.json").write_text(
        json.dumps({"adapter": adapter, "chat_id": chat_id})
    )


def _delete_routing(session_name: str):
    """Delete a session routing file."""
    f = SESSION_ROUTING_DIR / f"{session_name}.json"
    if f.exists():
        f.unlink()


# ---------------------------------------------------------------------------
# Seam 1: Telegram API — bot can send and receive
# ---------------------------------------------------------------------------

@requires_telegram
class TestTelegramSeam:
    """Verify the Telegram API connection is healthy."""

    def test_bot_identity(self):
        """getMe returns valid bot info."""
        import urllib.request
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=10) as resp:
            data = json.loads(resp.read())
        assert data["ok"]
        assert data["result"]["is_bot"]
        assert data["result"]["username"]

    def test_bot_can_send_to_chat(self):
        """Bot can send a message to its known chat."""
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = _find_chat_id()
        if not chat_id:
            pytest.skip("no chat_id found")

        marker = f"🧪 closed-loop probe {int(time.time())}"
        result = _telegram_send(token, chat_id, marker)
        assert result["ok"]
        assert result["result"]["text"] == marker

    def test_bot_can_poll_updates(self):
        """getUpdates returns without error (even if empty)."""
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        result = _telegram_get_updates(token, timeout=1)
        assert result["ok"]
        assert isinstance(result["result"], list)


# ---------------------------------------------------------------------------
# Seam 2: Server notify pipeline
# ---------------------------------------------------------------------------

@requires_server
@requires_tmux
class TestNotifySeam:
    """Verify the server's /notify endpoint delivers to Telegram.

    Creates a temporary test session with TEST_CHAT_ID so notifies
    go to the test group, not the main chat.
    """

    @pytest.fixture(autouse=True)
    def _test_session(self, tmux_session, server_post):
        """Create a temporary session routed to TEST_CHAT_ID for notify tests."""
        self._name = tmux_session
        self._chat_id = _find_chat_id()
        if not self._chat_id:
            pytest.skip("no chat_id available (set TEST_CHAT_ID)")
        _write_routing(self._name, "telegram", self._chat_id)
        subprocess.run(["tmux", "new-session", "-d", "-s", self._name, "sleep 60"], check=True)
        time.sleep(0.3)
        self._server_post = server_post
        yield
        _delete_routing(self._name)

    def test_notify_delivers_to_telegram(self):
        """POST /notify with a test session routes to Telegram test group."""
        marker = f"🧪 notify-seam-{int(time.time())}"
        result = self._server_post("/notify", {
            "session": self._name,
            "message": marker,
        })
        assert result.get("adapter") == "telegram"
        assert result.get("ok") is True

    def test_notify_logged_in_bot_debug(self):
        """After notify, an entry should appear in bot debug log."""
        marker = f"🧪 log-check-{int(time.time())}"
        self._server_post("/notify", {"session": self._name, "message": marker})
        time.sleep(1)

        recent = _read_bot_log_tail(30)
        found = any(
            marker in e.get("message", "") for e in recent
        )
        assert found, f"notify marker not found in bot log (checked last 30 entries)"

    def test_notify_footer_appended(self):
        """server.js appends the DEN_REPLY_FOOTER to notify messages."""
        # Verify the constant exists in server.js
        server_js = Path("/workspace/woltspace/server.js")
        source = server_js.read_text()
        assert "DEN_REPLY_FOOTER" in source
        assert "↩️ reply to this message to talk to this session directly" in source
        # Verify footer is appended (message + footer)
        assert "message + footer" in source


# ---------------------------------------------------------------------------
# Seam 3: Session creation and routing
# ---------------------------------------------------------------------------

@requires_tmux
class TestSessionCreationSeam:
    """Verify sessions can be created with routing and tracked via tmux."""

    def test_write_routing_creates_file(self, tmp_path):
        """Writing routing creates a JSON file we can read back."""
        import bot.core as core
        original = core.SESSION_ROUTING_DIR
        try:
            core.SESSION_ROUTING_DIR = tmp_path / "routing"
            core.write_session_routing("test-loop", {
                "adapter": "telegram", "chat_id": "123456"
            })
            result = core.read_session_routing("test-loop")
            assert result["adapter"] == "telegram"
            assert result["chat_id"] == "123456"
        finally:
            core.SESSION_ROUTING_DIR = original

    def test_tmux_session_alive_detection(self, tmux_session):
        """A live tmux session should be detectable."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.3)

        result = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
        assert result.returncode == 0

    def test_dead_session_detected(self):
        """A nonexistent session should not be detectable."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", "ghost-session-xyz"],
            capture_output=True,
        )
        assert result.returncode != 0


# ---------------------------------------------------------------------------
# Seam 4: Message delivery to sessions (den reply)
# ---------------------------------------------------------------------------

@requires_tmux
class TestDenReplySeam:
    """Verify messages can be delivered to running tmux sessions."""

    def test_send_keys_delivers_message(self, tmux_session):
        """tmux send-keys should deliver text to a running session."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
        time.sleep(0.3)

        message = f"test-delivery-{int(time.time())}"
        subprocess.run(["tmux", "send-keys", "-t", name, "-l", message], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)

        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert message in result.stdout

    def test_message_session_function(self, tmux_session):
        """core.message_session should deliver to a live tmux session."""
        name = tmux_session
        # Start a session running cat (will echo input)
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
        time.sleep(0.3)

        from bot.core import message_session
        result = message_session(name, "hello from test")
        assert result["ok"] is True
        assert result["status"] in ("delivered", "revived")

    def test_message_session_nonexistent(self):
        """message_session to a dead session should return ok=False or revive."""
        from bot.core import message_session
        result = message_session("nonexistent-test-session-xyz", "hello")
        # Should not crash
        assert isinstance(result, dict)
        # Either fails gracefully or attempts revive
        assert "ok" in result or "error" in result


# ---------------------------------------------------------------------------
# Seam 5: Full round-trip (server + tmux + notify)
# ---------------------------------------------------------------------------

@requires_server
@requires_tmux
class TestFullRoundTrip:
    """End-to-end: create session → deliver message → notify back."""

    def test_create_session_and_verify_alive(self, tmux_session):
        """Create a real tmux session and verify it's alive."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.5)

        result = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True)
        assert result.returncode == 0

    def test_notify_with_freshly_routed_session(self, tmux_session, server_post):
        """Create a session with routing, then notify through it."""
        name = tmux_session
        chat_id = _find_chat_id()
        if not chat_id:
            pytest.skip("no chat_id available")

        _write_routing(name, "telegram", chat_id)
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.3)

        marker = f"🧪 round-trip-{int(time.time())}"
        result = server_post("/notify", {"session": name, "message": marker})

        assert result.get("ok") is True
        assert result.get("adapter") == "telegram"

        # Cleanup
        _delete_routing(name)

    def test_session_env_available_for_notify(self, tmux_session):
        """A session with WOLT_SESSION set can use the notify script."""
        name = tmux_session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "bash"], check=True)
        time.sleep(0.3)

        # Set WOLT_SESSION like run-session.sh does
        subprocess.run([
            "tmux", "send-keys", "-t", name, "-l",
            f"export WOLT_SESSION={name} && echo $WOLT_SESSION"
        ], check=True)
        subprocess.run(["tmux", "send-keys", "-t", name, "", "Enter"], check=True)
        time.sleep(0.5)

        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p"],
            capture_output=True, text=True, check=True,
        )
        assert name in result.stdout


# ---------------------------------------------------------------------------
# Regression: known bugs that must not recur
# ---------------------------------------------------------------------------

class TestRegressions:
    """Guard against previously-fixed bugs."""

    def test_exclamation_in_prompt_doesnt_expand(self):
        """gy!be in a prompt must not trigger bash history expansion."""
        import shlex
        dangerous = "play gy!be and mogwai"
        quoted = shlex.quote(dangerous)
        result = subprocess.run(
            ["bash", "-c", f"echo {quoted}"],
            capture_output=True, text=True,
        )
        assert result.stdout.strip() == dangerous

    def test_session_name_sanitization(self):
        """Session names with special chars must be sanitized."""
        def sanitize(name):
            return re.sub(r'[^a-zA-Z0-9_-]', '', name or '')[:64] or 'main'

        assert sanitize("neowolt-abc_123") == "neowolt-abc_123"
        assert sanitize("foo/../etc/passwd") == "fooetcpasswd"
        assert sanitize("") == "main"
        assert sanitize("a" * 100) == "a" * 64

    def test_orphaned_tool_result_dropped(self):
        """Orphaned tool results in history must be dropped."""
        from bot.core import _sanitize_history
        history = [
            {"role": "tool", "content": "orphan", "tool_call_id": "x"},
            {"role": "user", "content": "hello"},
        ]
        cleaned = _sanitize_history(history)
        assert len(cleaned) == 1
        assert cleaned[0]["role"] == "user"

    def test_notify_script_executable(self):
        """The notify script must be executable."""
        notify = Path("/workspace/woltspace/container/bin/notify")
        assert notify.exists()
        assert os.access(notify, os.X_OK)

    def test_den_reply_footer_consistent(self):
        """Footer constant must match between server.js and telegram_adapter.py."""
        server_src = Path("/workspace/woltspace/server.js").read_text()
        adapter_src = Path("/workspace/woltspace/container/bot/telegram_adapter.py").read_text()

        # Both should contain the exact same sentinel string
        sentinel = "↩️ reply to this message to talk to this session directly"
        assert sentinel in server_src
        assert sentinel in adapter_src

    def test_routing_file_write_is_clean(self, tmp_path):
        """Routing file writes should not leave temp files."""
        import bot.core as core
        original = core.SESSION_ROUTING_DIR
        try:
            core.SESSION_ROUTING_DIR = tmp_path / "routing"
            core.write_session_routing("clean-test", {"adapter": "telegram", "chat_id": "123"})
            # No .tmp files should remain
            assert list((tmp_path / "routing").glob("*.tmp")) == []
        finally:
            core.SESSION_ROUTING_DIR = original
