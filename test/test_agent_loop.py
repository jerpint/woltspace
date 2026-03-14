"""Agent loop tests — Claude (haiku) in the loop, making real decisions.

These tests call core.get_response() directly, which hits the haiku LLM API.
Each test costs real API tokens. They verify the agent makes correct decisions:
tool calls, creature routing, session spawning, error handling.

Tiers:
- Haiku decision tests: verify tool calls without spawning actual sessions (mock tool execution)
- Live agent tests: full loop including session spawn + notify (slow, expensive)

Usage:
  uv run pytest test/test_agent_loop.py -v                    # all
  uv run pytest test/test_agent_loop.py -k "decision" -v      # fast: haiku decisions only (mocked tools)
  uv run pytest test/test_agent_loop.py -k "live" -v           # slow: real sessions spawned

Cost: ~$0.01-0.05 per test (haiku calls), ~$0.10-0.50 for live tests (haiku + sonnet)
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from conftest import requires_server, requires_tmux


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
WOLT_NAME = os.environ.get("WOLT_NAME", "neowolt")
REGISTRY_DIR = WOLTS_DIR / ".state" / "registry"
BOT_LOG_PATHS = [
    WOLTS_DIR / WOLT_NAME / ".state" / "bot-debug" / "bot.jsonl",
    WOLTS_DIR / ".state" / "bot-debug" / "bot.jsonl",
]
TRANSCRIPT_LOG = WOLTS_DIR / ".state" / "test-transcripts" / "agent-loop.jsonl"
TEST_VERBOSE = os.environ.get("TEST_VERBOSE", "1") == "1"


# ---------------------------------------------------------------------------
# Transcript logging
# ---------------------------------------------------------------------------

def _log_transcript(test_name: str, entries: list[dict]):
    """Write test transcript to log file. Send to telegram if verbose."""
    # Always write to file
    TRANSCRIPT_LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "test": test_name,
        "turns": entries,
    }
    with open(TRANSCRIPT_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")

    # Send to telegram if verbose
    if TEST_VERBOSE:
        _tg_send_transcript(test_name, entries)


def _tg_send_transcript(test_name: str, entries: list[dict]):
    """Send conversation transcript to test group."""
    chat_id = os.environ.get("TEST_CHAT_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not chat_id or not token:
        return

    short_name = test_name.split("::")[-1] if "::" in test_name else test_name
    lines = [f"📝 {short_name}"]
    for e in entries:
        lines.append(f"  👤 {e.get('user', '')[:120]}")
        response = e.get('response', '')[:200]
        if response:
            lines.append(f"  🦦 {response}")
        tools = e.get('tools', [])
        if tools:
            tool_str = ", ".join(f"{t['tool']}({json.dumps(t.get('args', {}), default=str)[:60]})" for t in tools)
            lines.append(f"  🔧 {tool_str}")

    msg = "\n".join(lines)
    # Telegram 4096 char limit
    if len(msg) > 4000:
        msg = msg[:3997] + "..."

    try:
        body = json.dumps({"chat_id": chat_id, "text": msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass  # Don't fail tests because of telegram


def _haiku_available() -> bool:
    """Check if we can make haiku API calls (via whatever provider is configured)."""
    try:
        from bot.core import LLM_MODEL
        from litellm import completion
        resp = completion(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": "say 'ok'"}],
            max_tokens=10,
        )
        return bool(resp.choices[0].message.content)
    except Exception:
        return False


requires_haiku = pytest.mark.skipif(not _haiku_available(), reason="haiku API not available")


def _get_response_with_mock_tools(user_message: str, mock_session_result: dict = None) -> dict:
    """Call get_response but intercept tool execution so we don't actually spawn sessions.

    Returns the full result dict plus the tool calls that were attempted.
    """
    captured_tool_calls = []

    if mock_session_result is None:
        mock_session_result = {
            "name": "test-mock-session-abc123",
            "url": "https://example.com/tui?session=test-mock-session-abc123",
            "wolt": WOLT_NAME,
        }

    original_execute = None
    import bot.core as core
    original_execute = core._execute_tool

    def mock_execute(name, args, routing):
        captured_tool_calls.append({"tool": name, "args": args})
        # For session-creating tools, return mock result
        if name in ("claude_code", "new_session"):
            result = dict(mock_session_result)
            if args.get("creature"):
                result["creature"] = args["creature"]
                from bot.core import CREATURE_MODELS
                result["model"] = CREATURE_MODELS.get(args["creature"], "")
            return json.dumps(result)
        # For other tools, use real implementation
        return original_execute(name, args, routing)

    try:
        core._execute_tool = mock_execute
        routing = {"adapter": "telegram", "chat_id": os.environ.get("TEST_CHAT_ID", "test-123")}
        result = core.get_response(user_message, routing=routing)
        result["_captured_tool_calls"] = captured_tool_calls
        return result
    finally:
        core._execute_tool = original_execute


def _find_bot_log_entries(event: str = None, after_ts: str = None, limit: int = 50) -> list[dict]:
    """Read recent bot log entries, optionally filtered."""
    entries = []
    for log_path in BOT_LOG_PATHS:
        if not log_path.exists():
            continue
        for line in log_path.read_text().strip().split("\n")[-limit:]:
            try:
                e = json.loads(line)
                if event and e.get("event") != event:
                    continue
                if after_ts and e.get("ts", "") < after_ts:
                    continue
                entries.append(e)
            except json.JSONDecodeError:
                continue
    return entries


# ---------------------------------------------------------------------------
# Haiku decision tests — verify tool calls without spawning sessions
# ---------------------------------------------------------------------------

def _transcript_entry(user_msg: str, result: dict) -> dict:
    """Build a transcript entry from a user message and response."""
    tools = result.get("_captured_tool_calls", [])
    return {
        "user": user_msg,
        "response": result.get("text", ""),
        "type": result.get("type", "text"),
        "tools": [{"tool": t["tool"], "args": t.get("args", {})} for t in tools],
    }


@requires_haiku
class TestHaikuDecisions:
    """Test that haiku makes correct tool call decisions.

    These mock the tool execution so no real sessions are spawned.
    Each test makes 1-2 haiku API calls (~$0.01).
    """

    def test_session_request_triggers_tool_call(self, request):
        """Asking to build something should trigger claude_code or new_session."""
        msg = "build me a hello world html page"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        tools = result["_captured_tool_calls"]
        assert len(tools) >= 1, f"expected tool call, got none. response: {result.get('text', '')[:200]}"
        tool_names = [t["tool"] for t in tools]
        assert any(n in ("claude_code", "new_session") for n in tool_names), \
            f"expected session tool, got: {tool_names}"

    def test_beaver_creature_when_requested(self, request):
        """Asking for a beaver should set creature=beaver."""
        msg = "fire up a beaver to write a python script"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        tools = result["_captured_tool_calls"]
        session_tools = [t for t in tools if t["tool"] in ("claude_code", "new_session")]
        assert len(session_tools) >= 1
        creature = session_tools[0]["args"].get("creature", "")
        assert creature == "beaver", f"expected beaver, got: {creature}"

    def test_raccoon_creature_when_requested(self, request):
        """Asking for a raccoon should set creature=raccoon."""
        msg = "spin up a raccoon to plan the architecture"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        tools = result["_captured_tool_calls"]
        session_tools = [t for t in tools if t["tool"] in ("claude_code", "new_session")]
        assert len(session_tools) >= 1
        creature = session_tools[0]["args"].get("creature", "")
        assert creature == "raccoon", f"expected raccoon, got: {creature}"

    def test_simple_question_no_session(self, request):
        """A simple question should NOT spawn a session."""
        msg = "what time is it?"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        tools = result["_captured_tool_calls"]
        session_tools = [t for t in tools if t["tool"] in ("claude_code", "new_session")]
        assert len(session_tools) == 0, \
            f"unexpected session spawn for simple question. tools: {[t['tool'] for t in tools]}"

    def test_list_sessions_request(self, request):
        """Asking about sessions should call list_sessions, not spawn a new one."""
        msg = "what sessions are running?"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        tools = result["_captured_tool_calls"]
        tool_names = [t["tool"] for t in tools]
        assert "list_sessions" in tool_names, f"expected list_sessions, got: {tool_names}"

    def test_response_has_text(self, request):
        """Every response should have text (the ack or answer)."""
        msg = "hey, how's it going?"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        assert result.get("text"), "response should have text"
        assert isinstance(result["text"], str)

    def test_response_has_history(self, request):
        """Every response should include history_messages for storage."""
        msg = "hello"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        assert "history_messages" in result
        assert isinstance(result["history_messages"], list)

    def test_prompt_with_special_chars(self, request):
        """Prompts with special characters should not crash the agent loop."""
        msg = "build something with gy!be & $HOME && `whoami`"
        result = _get_response_with_mock_tools(msg)
        _log_transcript(request.node.nodeid, [_transcript_entry(msg, result)])
        # Should not crash — either spawns session or responds
        assert result.get("text") is not None or result.get("_captured_tool_calls")


# ---------------------------------------------------------------------------
# Conversation simulator
# ---------------------------------------------------------------------------

class ConversationSimulator:
    """Simulate a multi-turn conversation between a user and the otter (haiku).

    Each turn: send a message, get a response, optionally assert on the result.
    Maintains conversation history across turns (like the real telegram adapter).
    Tool execution is mocked by default (no real sessions).

    Usage:
        sim = ConversationSimulator()
        sim.say("build me a hello world page")
        assert sim.last_tool_calls  # haiku should have called a tool
        assert any(t["tool"] == "claude_code" for t in sim.last_tool_calls)

        sim.say("what sessions are running?")
        assert any(t["tool"] == "list_sessions" for t in sim.last_tool_calls)
    """

    def __init__(self, mock_tools: bool = True):
        self.history: list[dict] = []
        self.turns: list[dict] = []
        self.mock_tools = mock_tools
        self.last_result: dict = {}
        self.last_tool_calls: list[dict] = []

    def say(self, message: str, assertions: list[str] = None) -> dict:
        """Send a message and get a response.

        Args:
            message: The user message
            assertions: Optional list of assertion names to check:
                - 'session_created': a session tool was called
                - 'no_session': no session tool was called
                - 'has_text': response has non-empty text
                - 'tool:NAME': specific tool was called

        Returns: The full result dict
        """
        if self.mock_tools:
            result = _get_response_with_mock_tools(message)
            tool_calls = result.get("_captured_tool_calls", [])
        else:
            import bot.core as core
            routing = {"adapter": "telegram", "chat_id": os.environ.get("TEST_CHAT_ID", "test-sim")}
            result = core.get_response(
                message,
                conversation_history=list(self.history),
                routing=routing,
            )
            tool_calls = result.get("tool_calls_log", [])

        # Update history
        self.history.append({"role": "user", "content": message})
        for msg in result.get("history_messages", []):
            self.history.append(msg)

        # Store turn
        turn = {
            "user": message,
            "response": result.get("text", ""),
            "type": result.get("type", "text"),
            "tool_calls": tool_calls,
        }
        self.turns.append(turn)
        self.last_result = result
        self.last_tool_calls = tool_calls

        # Run assertions
        if assertions:
            self._check_assertions(assertions, turn)

        return result

    def _check_assertions(self, assertions: list[str], turn: dict):
        tool_names = [t["tool"] for t in turn["tool_calls"]]
        for a in assertions:
            if a == "session_created":
                assert any(n in ("claude_code", "new_session") for n in tool_names), \
                    f"expected session creation, tools called: {tool_names}"
            elif a == "no_session":
                assert not any(n in ("claude_code", "new_session") for n in tool_names), \
                    f"unexpected session creation, tools called: {tool_names}"
            elif a == "has_text":
                assert turn["response"], "expected non-empty response text"
            elif a.startswith("tool:"):
                expected_tool = a.split(":", 1)[1]
                assert expected_tool in tool_names, \
                    f"expected tool '{expected_tool}', got: {tool_names}"
            elif a.startswith("creature:"):
                expected_creature = a.split(":", 1)[1]
                session_tools = [t for t in turn["tool_calls"]
                                 if t["tool"] in ("claude_code", "new_session")]
                assert session_tools, "no session tool called"
                actual = session_tools[0]["args"].get("creature", "")
                assert actual == expected_creature, \
                    f"expected creature '{expected_creature}', got '{actual}'"

    def log_transcript(self, test_name: str):
        """Write all turns to transcript log and optionally telegram."""
        entries = []
        for turn in self.turns:
            entries.append({
                "user": turn["user"],
                "response": turn["response"],
                "type": turn["type"],
                "tools": [{"tool": t["tool"], "args": t.get("args", {})} for t in turn["tool_calls"]],
            })
        _log_transcript(test_name, entries)

    def summary(self) -> str:
        """Return a human-readable summary of the conversation."""
        lines = []
        for i, turn in enumerate(self.turns):
            tools = ", ".join(t["tool"] for t in turn["tool_calls"]) or "none"
            lines.append(f"Turn {i+1}:")
            lines.append(f"  User: {turn['user'][:100]}")
            lines.append(f"  Bot: {turn['response'][:100]}")
            lines.append(f"  Tools: {tools}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Conversation scenario tests (use simulator)
# ---------------------------------------------------------------------------

@requires_haiku
class TestConversationScenarios:
    """Multi-turn conversation scenarios using the simulator."""

    def test_greeting_then_task(self, request):
        """Greeting should not spawn session, follow-up task should."""
        sim = ConversationSimulator()
        sim.say("hey nw, how's it going?", assertions=["no_session", "has_text"])
        sim.say("build me a simple todo app", assertions=["session_created"])
        sim.log_transcript(request.node.nodeid)

    def test_creature_request_respected(self, request):
        """Explicit creature request should be honored."""
        sim = ConversationSimulator()
        sim.say("fire up a raccoon to review our codebase", assertions=["creature:raccoon"])
        sim.log_transcript(request.node.nodeid)

    def test_session_inquiry(self, request):
        """Asking about sessions should use list_sessions."""
        sim = ConversationSimulator()
        sim.say("what sessions are running right now?", assertions=["tool:list_sessions"])
        sim.log_transcript(request.node.nodeid)

    def test_multi_turn_context(self, request):
        """Bot should maintain context across turns."""
        sim = ConversationSimulator()
        sim.say("build me a hello world page", assertions=["session_created"])
        # Follow-up should reference the session, not start a new one
        sim.say("what did you just start?")
        # Should have text (answering about the session)
        assert sim.last_result.get("text"), "expected response about the session"
        sim.log_transcript(request.node.nodeid)


# ---------------------------------------------------------------------------
# Live agent tests (real sessions spawned — slow, expensive)
# ---------------------------------------------------------------------------

@requires_haiku
@requires_server
@requires_tmux
class TestLiveAgentLoop:
    """Full agent loop: haiku spawns a real session, we verify it exists.

    These tests spawn actual Claude Code sessions. Each costs ~$0.10-0.50
    (haiku call + sonnet session). Sessions are cleaned up after.
    """

    @pytest.fixture(autouse=True)
    def _cleanup_sessions(self):
        """Track and clean up any sessions created during the test."""
        before = self._live_tmux_sessions()
        yield
        after = self._live_tmux_sessions()
        new_sessions = after - before
        for name in new_sessions:
            if name.startswith("test-") or name.startswith(WOLT_NAME):
                try:
                    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
                except Exception:
                    pass

    def _live_tmux_sessions(self) -> set[str]:
        try:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, check=True,
            )
            return {n for n in result.stdout.strip().split("\n") if n and n != "main"}
        except subprocess.CalledProcessError:
            return set()

    def test_session_actually_spawns(self, request):
        """Full loop: ask haiku to build something, verify tmux session appears."""
        import bot.core as core
        routing = {"adapter": "telegram", "chat_id": os.environ.get("TEST_CHAT_ID", "test-live")}
        before = self._live_tmux_sessions()

        msg = "create a beaver session that just echoes hello world and exits"
        result = core.get_response(msg, routing=routing)

        # Give tmux a moment to create the session
        time.sleep(2)
        after = self._live_tmux_sessions()
        new_sessions = after - before

        _log_transcript(request.node.nodeid, [{
            "user": msg,
            "response": result.get("text", ""),
            "type": result.get("type", "text"),
            "tools": [{"tool": "live_session", "args": {"new_sessions": list(new_sessions)}}],
        }])

        assert result.get("type") == "session" or len(new_sessions) > 0, \
            f"expected a session to spawn. type={result.get('type')}, new_sessions={new_sessions}"

    def test_spawned_session_in_registry(self, request):
        """Spawned session should appear in the session registry."""
        import bot.core as core
        from sessions import SessionRegistry

        routing = {"adapter": "telegram", "chat_id": os.environ.get("TEST_CHAT_ID", "test-live")}

        msg = "start a beaver to create a test file"
        result = core.get_response(msg, routing=routing)

        time.sleep(2)

        # Check registry for new sessions
        reg = SessionRegistry(REGISTRY_DIR)
        sessions = reg.list()
        recent = [s for s in sessions if s.get("created_at", 0) > time.time() - 30]

        _log_transcript(request.node.nodeid, [{
            "user": msg,
            "response": result.get("text", ""),
            "type": result.get("type", "text"),
            "tools": [{"tool": "registry_check", "args": {"recent_count": len(recent)}}],
        }])

        assert len(recent) > 0, "no new sessions in registry after get_response"
        newest = recent[0]
        assert newest.get("adapter") == "telegram"
        expected_chat = os.environ.get("TEST_CHAT_ID", "test-live")
        assert newest.get("chat_id") == expected_chat
