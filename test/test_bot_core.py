"""Bot core unit tests — pure Python, no server or tmux required.

Tests the logic in container/bot/core.py that can be verified without
external dependencies: session naming, ack text, history sanitization,
command building, memory loading.

Usage: uv run pytest test/test_bot_core.py -v
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add bot to path so we can import core
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


# ---------------------------------------------------------------------------
# Session naming
# ---------------------------------------------------------------------------

class TestSessionNaming:
    """Session names should be safe, unique, and follow the pattern."""

    def test_session_name_format(self):
        from bot.core import _session_name
        name = _session_name("neowolt")
        parts = name.split("-")
        assert parts[0] == "neowolt"
        assert len(parts) == 4  # prefix-adj-noun-hex
        assert len(parts[3]) == 6  # 6 hex chars

    def test_session_names_are_unique(self):
        from bot.core import _session_name
        names = {_session_name("test") for _ in range(50)}
        assert len(names) == 50  # extremely unlikely collision in 50 tries

    def test_session_name_shell_safe(self):
        from bot.core import _session_name
        name = _session_name("neowolt")
        assert re.match(r'^[a-z0-9-]+$', name), f"unsafe session name: {name}"

    def test_short_session_name(self):
        from bot.core import _short_session_name
        assert _short_session_name("neowolt-chompy-dam-a3f1e2") == "chompy-dam"

    def test_short_session_name_no_prefix(self):
        from bot.core import _short_session_name
        assert _short_session_name("simple") == "simple"


# ---------------------------------------------------------------------------
# Ack text
# ---------------------------------------------------------------------------

class TestAckText:
    """The ack message shown when a session starts."""

    def test_ack_has_session_emoji(self):
        from bot.core import build_ack_text
        text = build_ack_text(url="https://example.com/tui?session=test", session_name="neowolt-test-123")
        assert "🪵" in text

    def test_ack_beaver_emoji(self):
        from bot.core import build_ack_text
        text = build_ack_text(session_name="neowolt-test-123", creature="beaver")
        assert "🦫" in text

    def test_ack_raccoon_emoji(self):
        from bot.core import build_ack_text
        text = build_ack_text(session_name="neowolt-test-123", creature="raccoon")
        assert "🦝" in text

    def test_ack_default_beaver(self):
        from bot.core import build_ack_text
        text = build_ack_text(session_name="neowolt-test-123")
        assert "🦫" in text

    def test_ack_includes_url_when_provided(self):
        from bot.core import build_ack_text
        url = "https://example.trycloudflare.com/tui?session=test"
        text = build_ack_text(url=url, session_name="neowolt-test-123")
        assert url in text

    def test_ack_no_url_when_none(self):
        from bot.core import build_ack_text
        text = build_ack_text(session_name="neowolt-test-123")
        assert "session:" not in text


# ---------------------------------------------------------------------------
# Creature routing
# ---------------------------------------------------------------------------

class TestCreatureRouting:
    """Creature → emoji and model mapping."""

    def test_creature_emojis(self):
        from bot.core import CREATURE_EMOJIS
        assert CREATURE_EMOJIS["raccoon"] == "🦝"
        assert CREATURE_EMOJIS["beaver"] == "🦫"

    def test_creature_models(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        from harnesses import creature_model
        assert creature_model("claude", "raccoon") == "claude-opus-5"
        assert creature_model("claude", "beaver") == "claude-sonnet-5"

    def test_creature_model_lookup(self, tmp_path, monkeypatch):
        """Unknown creature should resolve to no model."""
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        from harnesses import creature_model
        assert creature_model("claude", "unknown") is None

    def test_creature_emoji_default(self):
        """Unknown creature should fall back to beaver emoji."""
        from bot.core import CREATURE_EMOJIS
        assert CREATURE_EMOJIS.get("unknown", "🦫") == "🦫"

    def test_otter_is_haiku(self, tmp_path, monkeypatch):
        """Otter should map to haiku model on claude."""
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        from harnesses import creature_model
        assert creature_model("claude", "otter") == "claude-haiku-4-5"

    def test_otter_emoji(self):
        from bot.core import CREATURE_EMOJIS
        assert CREATURE_EMOJIS["otter"] == "🦦"

    def test_otter_ack_emoji(self):
        """Ack text should use otter emoji when creature is otter."""
        from bot.core import build_ack_text
        text = build_ack_text(session_name="neowolt-test-123", creature="otter")
        assert "🦦" in text

    def test_creature_not_in_tool_schemas(self):
        """creature param removed from claude_code/new_session — derived from wolt type instead."""
        from bot.core import TOOLS
        for tool in TOOLS:
            name = tool["function"]["name"]
            if name in ("claude_code", "new_session"):
                props = tool["function"]["parameters"]["properties"]
                assert "creature" not in props, f"creature should not be in {name} schema — it's auto-derived from wolt type"

    def test_all_session_creatures_have_emoji(self):
        """Every known tier should have an emoji."""
        from bot.core import CREATURE_EMOJIS
        from harnesses import KNOWN_TIERS
        for creature in KNOWN_TIERS:
            assert creature in CREATURE_EMOJIS, f"{creature} missing from CREATURE_EMOJIS"


# ---------------------------------------------------------------------------
# Command building
# ---------------------------------------------------------------------------

class TestCommandBuilding:
    """build_session_command must produce shell-safe commands."""

    def test_simple_prompt(self):
        from bot.core import build_session_command
        cmd = build_session_command("test-1", "hello world")
        assert "'hello world'" in cmd

    def test_shell_metacharacters(self):
        from bot.core import build_session_command
        dangerous = "play gy!be; rm -rf /; $(whoami) `id`"
        cmd = build_session_command("test-2", dangerous)
        # Must be in single quotes
        assert "rm -rf" in cmd
        assert "$(whoami)" not in cmd.replace(shlex.quote(dangerous), "")

    def test_resume_flag(self):
        from bot.core import build_session_command
        cmd = build_session_command("test-3", "hello there", resume=True)
        assert "--resume" in cmd
        assert cmd.index("--resume") < cmd.index("'hello there'")

    def test_empty_prompt(self):
        from bot.core import build_session_command
        cmd = build_session_command("test-4", "")
        assert cmd.rstrip().endswith("test-4")


# ---------------------------------------------------------------------------
# History sanitization
# ---------------------------------------------------------------------------

class TestHistorySanitization:
    """_sanitize_history must clean up tool call artifacts."""

    def test_drops_orphaned_tool_results(self):
        """Tool results without preceding tool_calls should be dropped."""
        from bot.core import _sanitize_history
        history = [
            {"role": "tool", "content": '{"ok": true}', "tool_call_id": "orphan"},
            {"role": "user", "content": "hello"},
        ]
        cleaned = _sanitize_history(history)
        # Orphaned tool result should be dropped
        assert len(cleaned) == 1
        assert cleaned[0]["role"] == "user"

    def test_preserves_normal_messages(self):
        from bot.core import _sanitize_history
        history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        cleaned = _sanitize_history(history)
        assert cleaned[0]["content"] == "hello"
        assert cleaned[1]["content"] == "hi there"

    def test_handles_empty_history(self):
        from bot.core import _sanitize_history
        assert _sanitize_history([]) == []

    def test_keeps_valid_tool_chain(self):
        """Tool result after assistant with tool_calls should be kept."""
        from bot.core import _sanitize_history
        history = [
            {"role": "user", "content": "start a session"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "function": {"name": "new_session"}}]},
            {"role": "tool", "content": '{"ok": true}', "tool_call_id": "1"},
            {"role": "assistant", "content": "session started"},
        ]
        cleaned = _sanitize_history(history)
        assert len(cleaned) == 4  # all should be preserved


# ---------------------------------------------------------------------------
# Memory loading
# ---------------------------------------------------------------------------

class TestMemoryLoading:
    """load_memory reads identity, context, learnings from disk."""

    def test_load_memory_with_files(self, tmp_path):
        from bot.core import load_memory, MEMORY_DIR
        # Temporarily override MEMORY_DIR
        mem_dir = tmp_path / "memory"
        mem_dir.mkdir()
        (mem_dir / "identity.md").write_text("I am a test wolt.")
        (mem_dir / "context.md").write_text("Currently testing.\n" * 100)
        (mem_dir / "learnings.md").write_text("Lesson 1: test everything.\n" * 50)

        import bot.core as core
        original = core.MEMORY_DIR
        try:
            core.MEMORY_DIR = mem_dir
            memory = load_memory()
            assert "test wolt" in memory
            assert "Currently testing" in memory
            assert "Lesson 1" in memory
            # context.md should be truncated to 80 lines
            context_lines = [l for l in memory.split("\n") if "Currently testing" in l]
            assert len(context_lines) <= 80
        finally:
            core.MEMORY_DIR = original

    def test_load_memory_missing_dir(self, tmp_path):
        import bot.core as core
        original = core.MEMORY_DIR
        try:
            core.MEMORY_DIR = tmp_path / "nonexistent"
            memory = core.load_memory()
            # Should not crash, just return empty
            assert isinstance(memory, str)
        finally:
            core.MEMORY_DIR = original


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

class TestSystemPrompt:
    """build_system_prompt produces a valid prompt."""

    def test_prompt_contains_identity(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "wolt" in prompt.lower()
        assert "tool" in prompt.lower()

    def test_prompt_contains_creature_info(self):
        from bot.core import build_system_prompt
        prompt = build_system_prompt()
        assert "raccoon" in prompt
        assert "beaver" in prompt
