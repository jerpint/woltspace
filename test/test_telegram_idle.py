"""Telegram /idle: show and pick how long idle sessions stay up.

The same lodge-wide setting as the Settings page's "Close idle sessions after":
one value in woltspace.json, read and written through /settings/session-expiry.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

API = "http://127.0.0.1:7794"


class FakeLodge:
    """The lodge's /settings/session-expiry, as httpx.AsyncClient sees it."""

    def __init__(self, value=14400):
        self.value = value
        self.calls = []

    def client(self):
        lodge = self

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, timeout=None):
                lodge.calls.append(("GET", url, None))
                return SimpleNamespace(status_code=200, json=lambda: {"idle_timeout_seconds": lodge.value})

            async def post(self, url, json=None, timeout=None):
                lodge.calls.append(("POST", url, json))
                lodge.value = json["idle_timeout_seconds"]
                return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "idle_timeout_seconds": lodge.value})

        return Client


@pytest.fixture
def adapter(monkeypatch):
    from bot import telegram_adapter as adapter
    monkeypatch.setattr(adapter, "is_allowed", lambda update: True)
    monkeypatch.setenv("WOLTSPACE_API", API)
    return adapter


def _install(monkeypatch, lodge):
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", lodge.client())


def _buttons(markup):
    return [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]


def _command():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def _tap(data):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def test_idle_shows_the_window_and_marks_the_current_choice(adapter, monkeypatch):
    lodge = FakeLodge(value=14400)
    _install(monkeypatch, lodge)
    update = _command()
    asyncio.run(adapter.handle_idle(update, MagicMock()))
    args, kwargs = update.message.reply_text.await_args
    assert args[0] == "Idle sessions rest after 4 hours (whole lodge)."
    assert _buttons(kwargs["reply_markup"]) == [[
        ("Never", "idle:never"), ("1 hour", "idle:3600"), ("✓ 4 hours", "idle:14400"), ("1 day", "idle:86400"),
    ]]
    assert lodge.calls == [("GET", f"{API}/settings/session-expiry", None)]


@pytest.mark.parametrize("data,value,text", [
    ("idle:86400", 86400, "Idle sessions now rest after 1 day."),
    ("idle:never", None, "Idle sessions now stay open."),
])
def test_a_tap_saves_the_choice_and_says_so(adapter, monkeypatch, data, value, text):
    lodge = FakeLodge(value=14400)
    _install(monkeypatch, lodge)
    update, query = _tap(data)
    asyncio.run(adapter.handle_callback(update, MagicMock()))
    assert ("POST", f"{API}/settings/session-expiry", {"idle_timeout_seconds": value}) in lodge.calls
    args, kwargs = query.edit_message_text.await_args
    assert args[0] == text
    marked = [callback for label, callback in _buttons(kwargs["reply_markup"])[0] if label.startswith("✓ ")]
    assert marked == [data]


def test_an_unknown_choice_is_never_sent(adapter, monkeypatch):
    lodge = FakeLodge()
    _install(monkeypatch, lodge)
    update, query = _tap("idle:12")
    asyncio.run(adapter.handle_callback(update, MagicMock()))
    assert not [call for call in lodge.calls if call[0] == "POST"]


def test_a_stranger_gets_nothing(adapter, monkeypatch):
    lodge = FakeLodge()
    _install(monkeypatch, lodge)
    monkeypatch.setattr(adapter, "is_allowed", lambda update: False)
    update = _command()
    asyncio.run(adapter.handle_idle(update, MagicMock()))
    update.message.reply_text.assert_not_called()
    tap, query = _tap("idle:never")
    asyncio.run(adapter.handle_callback(tap, MagicMock()))
    query.edit_message_text.assert_not_called()
    assert lodge.calls == []
