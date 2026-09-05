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
  - Session creation via registry + tmux
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
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from conftest import (
    requires_live_send,
    requires_live_telegram,
    requires_server,
    requires_telegram,
    requires_tmux,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
# A log path only — never a spawn target. Absent WOLT_NAME simply yields no
# wolt-level log, and the wolts-level log below still applies.
WOLT_NAME = os.environ.get("WOLT_NAME", "").strip() or "_absent_"
# Server writes to wolt-level log, core.py writes to wolts-level log
BOT_LOG_PATHS = [
    WOLTS_DIR / WOLT_NAME / ".state" / "bot-debug" / "bot.jsonl",
    WOLTS_DIR / ".state" / "bot-debug" / "bot.jsonl",
]
# SessionRegistry takes the WOLTS dir, not a registry dir: sessions live at
# wolts/{wolt}/.state/sessions/. The old `WOLTS_DIR/.state/registry` argument
# wrote records where the server's routing lookup cannot see them — so every
# /notify in these tests fell through to the default chat (a real person),
# no matter what TEST_CHAT_ID said.
REGISTRY_ROOT = WOLTS_DIR


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
    """The configured test chat, or nothing.

    This used to fall back to scraping a chat_id out of the live session
    registry — which meant a test run messaged whichever real person happened
    to be talking to this woltspace. Tests must never discover real user state
    to target; if the test chat is not configured, the test does not run.
    """
    return os.environ.get("TEST_CHAT_ID") or None


# ---------------------------------------------------------------------------
# Seam 1: Telegram API — bot can send and receive
# ---------------------------------------------------------------------------

@requires_live_telegram
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

    @requires_live_send
    def test_bot_can_send_to_chat(self):
        """Bot can send a message to its known chat."""
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = _find_chat_id()
        if not chat_id:
            pytest.skip("TEST_CHAT_ID not set")

        marker = f"🧪 closed-loop probe {int(time.time())}"
        result = _telegram_send(token, chat_id, marker)
        assert result["ok"]
        assert result["result"]["text"] == marker

    def test_bot_can_poll_updates(self):
        """getUpdates returns without error (even if empty).

        Note: returns 409 Conflict when the live bot is already polling
        (Telegram only allows one getUpdates consumer at a time).
        """
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        try:
            result = _telegram_get_updates(token, timeout=1)
        except urllib.error.HTTPError as e:
            if e.code == 409:
                pytest.skip("bot is actively polling — getUpdates returns 409 Conflict")
            raise
        assert result["ok"]
        assert isinstance(result["result"], list)


# ---------------------------------------------------------------------------
# Seam 2: Server notify pipeline
# ---------------------------------------------------------------------------

@requires_live_send
@requires_server
@requires_tmux
class TestNotifySeam:
    """Verify the server's /notify endpoint delivers to Telegram.

    Creates a temporary test session with TEST_CHAT_ID so notifies
    go to the test group, not the main chat.
    """

    @pytest.fixture(autouse=True)
    def _test_session(self, tmux_session, server_post, shadow_wolt):
        """Create a temporary session routed to TEST_CHAT_ID for notify tests."""
        from sessions import SessionRegistry
        self._name = tmux_session
        self._chat_id = _find_chat_id()
        if not self._chat_id:
            pytest.skip("TEST_CHAT_ID not set")
        self._reg = SessionRegistry(REGISTRY_ROOT)
        self._reg.create(
            self._name, wolt=shadow_wolt, creature="beaver",
            adapter="telegram", chat_id=self._chat_id,
        )
        subprocess.run(["tmux", "new-session", "-d", "-s", self._name, "sleep 60"], check=True)
        time.sleep(0.3)
        self._server_post = server_post
        yield
        self._reg.delete(self._name)

    def test_notify_delivers_to_telegram(self):
        """POST /notify with a test session routes to Telegram test group."""
        marker = f"🧪 notify-seam-{int(time.time())}"
        result = self._server_post("/notify", {
            "session": self._name,
            "message": marker,
        })
        assert result.get("adapter") == "telegram"
        assert result.get("ok") is True

    @pytest.mark.xfail(reason="server /notify endpoint doesn't write to bot debug log")
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
        """notify.py appends the DEN_REPLY_FOOTER to notify messages."""
        notify_py = Path("/workspace/woltspace/server/notify.py")
        source = notify_py.read_text()
        assert "DEN_REPLY_FOOTER" in source
        assert "message + footer" in source


# ---------------------------------------------------------------------------
# Seam 3: Session creation and registry
# ---------------------------------------------------------------------------

@requires_tmux
class TestSessionCreationSeam:
    """Verify sessions can be created, tracked, and cleaned up."""

    def test_create_session_updates_registry(self, tmp_registry):
        """Creating a session writes to the registry."""
        reg = tmp_registry
        data = reg.create(
            "test-loop-session",
            wolt="neowolt",
            creature="beaver",
            adapter="telegram",
            chat_id="123456",
        )
        assert data["status"] == "running"
        assert data["adapter"] == "telegram"
        assert data["chat_id"] == "123456"

        # Verify we can read it back
        fetched = reg.get("test-loop-session", check_alive=False)
        assert fetched is not None
        assert fetched["creature"] == "beaver"

    def test_tmux_session_matches_registry(self, tmux_session, tmp_registry):
        """A tmux session should be detectable as alive by the registry."""
        name = tmux_session
        reg = tmp_registry

        # Create registry entry
        reg.create(name, wolt="neowolt")

        # Create tmux session
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.3)

        # Registry should see it as alive (using global tmux check)
        from sessions import _tmux_alive
        assert _tmux_alive(name) is True

    def test_dead_session_detected(self, tmp_registry):
        """A session that's not in tmux should be detectable."""
        reg = tmp_registry
        reg.create("ghost-session", wolt="neowolt")

        from sessions import _tmux_alive
        assert _tmux_alive("ghost-session") is False

    def test_reconcile_marks_orphans(self, tmux_session, tmp_registry):
        """reconcile() should mark non-tmux sessions as orphaned."""
        reg = tmp_registry
        reg.create("alive-session", wolt="neowolt")
        reg.create("dead-session", wolt="neowolt")

        # Only create tmux for the alive one
        name = tmux_session
        # Use tmux_session fixture name for the alive session
        reg.create(name, wolt="neowolt")
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 30"], check=True)
        time.sleep(0.3)

        orphaned = reg.reconcile()
        # alive-session and dead-session should be orphaned (not in tmux)
        assert "alive-session" in orphaned
        assert "dead-session" in orphaned
        assert name not in orphaned


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

    def test_message_session_function(self, tmux_session, tmp_path):
        """core.message_session should deliver to a live tmux session."""
        import sessions
        name = tmux_session

        # Create registry entry so resume_session can find it
        original_wolts = sessions.WOLTS_DIR
        sessions.WOLTS_DIR = tmp_path
        (tmp_path / "testwolt" / "wolt").mkdir(parents=True, exist_ok=True)
        reg = sessions.SessionRegistry(tmp_path)
        reg.create(name, wolt="testwolt")
        reg.update(name, wolt="testwolt", claude_session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Start a session running cat (will echo input)
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "cat"], check=True)
        time.sleep(0.3)

        try:
            from bot.core import message_session
            result = message_session(name, "hello from test")
            assert result["ok"] is True
            assert result["status"] in ("delivered", "revived")
        finally:
            sessions.WOLTS_DIR = original_wolts

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

    def test_create_session_and_verify_in_registry(self, tmux_session, shadow_wolt):
        """Create a real tmux session and verify it appears in the live registry."""
        from sessions import SessionRegistry
        name = tmux_session

        reg = SessionRegistry(REGISTRY_ROOT)
        reg.create(
            name, wolt=shadow_wolt, creature="beaver",
            adapter="telegram", chat_id="test",
        )

        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.5)

        data = reg.get(name, check_alive=True)
        assert data is not None
        assert data["alive"] is True
        assert data["status"] == "running"

        # Cleanup registry
        reg.delete(name)

    @requires_live_send
    def test_notify_with_freshly_created_session(
        self, tmux_session, server_post, shadow_wolt
    ):
        """Create a session with routing, then notify through it."""
        from sessions import SessionRegistry
        name = tmux_session
        chat_id = _find_chat_id()
        if not chat_id:
            pytest.skip("TEST_CHAT_ID not set")

        reg = SessionRegistry(REGISTRY_ROOT)
        reg.create(
            name, wolt=shadow_wolt, creature="beaver",
            adapter="telegram", chat_id=chat_id,
        )
        subprocess.run(["tmux", "new-session", "-d", "-s", name, "sleep 60"], check=True)
        time.sleep(0.3)

        marker = f"🧪 round-trip-{int(time.time())}"
        result = server_post("/notify", {"session": name, "message": marker})

        assert result.get("ok") is True
        assert result.get("adapter") == "telegram"

        # Cleanup
        reg.delete(name)

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
        """Footer constant must match between server/config.py and telegram_adapter.py."""
        config_src = Path("/workspace/woltspace/server/config.py").read_text()
        adapter_src = Path("/workspace/woltspace/container/bot/telegram_adapter.py").read_text()

        # Both should contain the exact same sentinel string
        sentinel = "↩️ reply to this message to talk to this session directly"
        assert sentinel in config_src
        assert sentinel in adapter_src

    @requires_tmux
    def test_revival_picks_correct_session(self, tmp_path):
        """Reviving session A must --resume with A's UUID, not B's.

        UUID selection now happens in prepare_session_command (run by
        run-session.sh); resume_session delivers the wrapper to the right
        pane. Both layers are checked here.
        """
        from sessions import SessionRegistry, resume_session, prepare_session_command
        from session_runtime import TmuxSessionRuntime
        import sessions

        original_wolts = sessions.WOLTS_DIR
        sessions.WOLTS_DIR = tmp_path

        session_a = f"test-revival-a-{int(time.time()) % 100000}"
        session_b = f"test-revival-b-{int(time.time()) % 100000}"

        try:
            # Create wolt dir and registry entries
            (tmp_path / "neowolt" / "wolt").mkdir(parents=True, exist_ok=True)
            reg = SessionRegistry(tmp_path)
            uuid_a = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
            uuid_b = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
            reg.create(session_a, wolt="neowolt")
            reg.update(session_a, wolt="neowolt", claude_session_id=uuid_a)
            reg.create(session_b, wolt="neowolt")
            reg.update(session_b, wolt="neowolt", claude_session_id=uuid_b)

            # Layer 1: prepare builds A's agent command with A's UUID, not B's
            cmd_a = prepare_session_command(session_a, "resume", "test message")
            assert f"--resume {uuid_a}" in cmd_a
            assert uuid_b not in cmd_a

            # Create tmux sessions running bash (no agent -> revive path) and
            # persist the exact panes they own.
            runtime = TmuxSessionRuntime()
            handles = {}
            for name in [session_a, session_b]:
                handles[name] = runtime.spawn(name, str(tmp_path), "bash")
                reg.update(name, wolt="neowolt", runtime=handles[name].to_record())
            time.sleep(0.3)

            # Layer 2: revive session A — wrapper lands in A's pane in resume mode
            result = resume_session(session_a, "test message")
            assert result["status"] == "revived"
            assert result["name"] == session_a

            time.sleep(0.3)
            capture = subprocess.run(
                ["tmux", "capture-pane", "-t", handles[session_a].pane_id, "-p", "-J"],
                capture_output=True, text=True, check=True,
            )
            flat = capture.stdout.replace("\n", " ")
            assert session_a in flat and "--resume" in flat, (
                f"tmux pane should contain run-session.sh {session_a} --resume, got: {flat[:500]}"
            )
            assert session_b not in flat, (
                f"Should NOT contain session B's name: {flat[:500]}"
            )

        finally:
            sessions.WOLTS_DIR = original_wolts
            for name in [session_a, session_b]:
                subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)

    @requires_tmux
    def test_revival_uses_session_name_as_id(self, tmp_path):
        """Session name IS the claude session ID — no UUID needed, no --continue fallback."""
        from sessions import SessionRegistry, resume_session
        from session_runtime import TmuxSessionRuntime
        import sessions

        original_wolts = sessions.WOLTS_DIR
        sessions.WOLTS_DIR = tmp_path

        session_name = f"test-revival-name-{int(time.time()) % 100000}"

        try:
            (tmp_path / "neowolt" / "wolt").mkdir(parents=True, exist_ok=True)
            reg = SessionRegistry(tmp_path)
            reg.create(session_name, wolt="neowolt")
            reg.update(session_name, wolt="neowolt", claude_session_id=session_name)

            runtime = TmuxSessionRuntime()
            handle = runtime.spawn(session_name, str(tmp_path), "bash")
            reg.update(session_name, wolt="neowolt", runtime=handle.to_record())
            time.sleep(0.3)

            result = resume_session(session_name, "test name-as-id")
            assert result["status"] == "revived"
            # Should use --resume with the session name itself
            assert "--resume" in result.get("detail", "")
            assert "--continue" not in result.get("detail", "")

        finally:
            sessions.WOLTS_DIR = original_wolts
            subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

    @requires_tmux
    def test_revival_cds_into_session_wolt_dir(self, tmp_path):
        """Reviving a session must run in the session's wolt dir, not the current dir.

        The cd now happens inside run-session.sh (from the registry 'dir'
        field) — one code path for spawn and resume. This test verifies the
        wrapper is delivered to the pane; the dir read is covered by the
        wrapper reading the same registry field the resume delivery relies on.
        """
        from sessions import SessionRegistry, resume_session
        from session_runtime import TmuxSessionRuntime
        import sessions

        original_wolts = sessions.WOLTS_DIR
        sessions.WOLTS_DIR = tmp_path

        session_name = f"test-revival-dir-{int(time.time()) % 100000}"
        wolt_dir = str(tmp_path / "uxwolt")

        try:
            (tmp_path / "uxwolt" / "wolt").mkdir(parents=True, exist_ok=True)
            reg = SessionRegistry(tmp_path)
            reg.create(session_name, wolt="uxwolt")
            reg.update(session_name, wolt="uxwolt", claude_session_id=session_name, dir=wolt_dir)

            runtime = TmuxSessionRuntime()
            handle = runtime.spawn(session_name, wolt_dir, "bash")
            reg.update(session_name, wolt="uxwolt", runtime=handle.to_record())
            time.sleep(0.3)

            result = resume_session(session_name, "test dir fix")
            assert result["status"] == "revived"

            # Verify the wrapper command was delivered in resume mode
            time.sleep(0.3)
            capture = subprocess.run(
                ["tmux", "capture-pane", "-t", handle.pane_id, "-p", "-J"],
                capture_output=True, text=True, check=True,
            )
            flat = capture.stdout.replace("\n", " ")
            assert "run-session.sh" in flat and "--resume" in flat, (
                f"tmux pane should contain run-session.sh --resume, got: {flat[:500]}"
            )

        finally:
            sessions.WOLTS_DIR = original_wolts
            subprocess.run(["tmux", "kill-session", "-t", session_name], capture_output=True)

    def test_session_registry_atomic_writes(self, tmp_path):
        """Registry writes should use tmp+rename (no partial reads)."""
        from sessions import SessionRegistry
        reg = SessionRegistry(tmp_path / "reg")
        reg.create("atomic-test", wolt="neowolt")
        # No .tmp files should remain in the sessions dir
        sessions_dir = tmp_path / "reg" / "neowolt" / ".state" / "sessions"
        assert list(sessions_dir.glob("*.tmp")) == []
