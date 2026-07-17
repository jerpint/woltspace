"""Wolt-to-wolt messaging — unit tests for attribution, resolution, delivery.

Pure-Python; no server or tmux needed (tmux calls are monkeypatched).

Usage: uv run pytest test/test_wolt_messaging.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
import sessions  # noqa: E402
from sessions import (  # noqa: E402
    format_attributed_message,
    resolve_active_session,
    deliver_message,
)


# ---------------------------------------------------------------------------
# Attribution contract
# ---------------------------------------------------------------------------

class TestAttribution:
    def test_full_attribution_has_header_and_reply(self):
        out = format_attributed_message("hello", "uxwolt", "uxwolt-bushy-fur-224aa5")
        assert out.startswith("[message from uxwolt, session=uxwolt-bushy-fur-224aa5]\n")
        assert "hello" in out
        # reply routes back by SESSION ID, not wolt name
        assert 'Reply with: woltspace session send uxwolt-bushy-fur-224aa5 "your reply"' in out

    def test_no_sender_returns_text_unchanged(self):
        assert format_attributed_message("plain nudge", "", "") == "plain nudge"

    def test_sender_without_session_omits_reply_line(self):
        out = format_attributed_message("hi", "jerpint", "")
        assert out.startswith("[message from jerpint]\n")
        assert "Reply with:" not in out


# ---------------------------------------------------------------------------
# Wolt-name -> active session resolution
# ---------------------------------------------------------------------------

class TestResolveActiveSession:
    def test_picks_most_recently_active_live_session(self, tmp_registry, monkeypatch):
        reg = tmp_registry
        reg.create("codexw-old-oak-aaaaaa", wolt="codexw")
        reg.create("codexw-new-elm-bbbbbb", wolt="codexw")
        # update() auto-stamps last_activity=now, so set it directly for a
        # deterministic ordering.
        old = reg.get("codexw-old-oak-aaaaaa", check_alive=False)
        old["last_activity"] = 100
        reg._write("codexw", "codexw-old-oak-aaaaaa", old)
        new = reg.get("codexw-new-elm-bbbbbb", check_alive=False)
        new["last_activity"] = 999
        reg._write("codexw", "codexw-new-elm-bbbbbb", new)
        # both live
        monkeypatch.setattr(sessions, "_tmux_sessions",
                            lambda: {"codexw-old-oak-aaaaaa", "codexw-new-elm-bbbbbb"})
        assert resolve_active_session("codexw", registry=reg) == "codexw-new-elm-bbbbbb"

    def test_ignores_dead_sessions(self, tmp_registry, monkeypatch):
        reg = tmp_registry
        reg.create("codexw-dead-oak-aaaaaa", wolt="codexw")
        reg.update("codexw-dead-oak-aaaaaa", last_activity=999)
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())  # none alive
        assert resolve_active_session("codexw", registry=reg) is None

    def test_no_sessions_returns_none(self, tmp_registry, monkeypatch):
        monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())
        assert resolve_active_session("ghostwolt", registry=tmp_registry) is None


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

class TestDeliverMessage:
    def test_no_session(self, tmp_registry, monkeypatch):
        monkeypatch.setattr(sessions, "_tmux_alive", lambda n: False)
        res = deliver_message("nope-xxx-yyy-zzzzzz", "hi", registry=tmp_registry)
        assert res["status"] == "no-session"

    def test_session_dead(self, tmp_registry, monkeypatch):
        reg = tmp_registry
        reg.create("codexw-cold-oak-aaaaaa", wolt="codexw", harness="codex")
        monkeypatch.setattr(sessions, "_tmux_alive", lambda n: False)  # exists but not alive
        res = deliver_message("codexw-cold-oak-aaaaaa", "hi", registry=reg)
        assert res["status"] == "session-dead"

    def test_delivered_pastes_attributed_body_with_harness_settle(self, tmp_registry, monkeypatch):
        reg = tmp_registry
        reg.create("codexw-warm-oak-aaaaaa", wolt="codexw", harness="codex")
        monkeypatch.setattr(sessions, "_tmux_alive", lambda n: True)
        captured = {}

        def fake_paste(target, text, settle=0.0):
            captured["target"] = target
            captured["text"] = text
            captured["settle"] = settle

        monkeypatch.setattr(sessions, "_tmux_paste", fake_paste)
        res = deliver_message(
            "codexw-warm-oak-aaaaaa", "let's talk",
            from_wolt="uxwolt", from_session="uxwolt-bushy-fur-224aa5", registry=reg,
        )
        assert res["status"] == "delivered"
        assert res["harness"] == "codex"
        assert captured["target"] == "codexw-warm-oak-aaaaaa"
        # attribution was applied
        assert captured["text"].startswith("[message from uxwolt, session=uxwolt-bushy-fur-224aa5]")
        assert "let's talk" in captured["text"]
        # codex harness → non-zero paste settle (its TUI folds an immediate Enter)
        assert captured["settle"] == 0.5

    def test_claude_harness_zero_settle(self, tmp_registry, monkeypatch):
        reg = tmp_registry
        reg.create("uxwolt-warm-oak-aaaaaa", wolt="uxwolt", harness="claude")
        monkeypatch.setattr(sessions, "_tmux_alive", lambda n: True)
        captured = {}
        monkeypatch.setattr(sessions, "_tmux_paste",
                            lambda t, x, settle=0.0: captured.update(settle=settle))
        deliver_message("uxwolt-warm-oak-aaaaaa", "hi", registry=reg)
        assert captured["settle"] == 0.0
