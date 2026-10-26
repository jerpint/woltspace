"""Seeing holds: Telegram /holds, the 📌 in /sessions, and the data under them.

A hold keeps a session up for a background task. The owner sees every held
session, with its reason, and can release one; a release only lets the
session rest at the next sweep, if nobody is using it.
"""

import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import sessions  # noqa: E402
from sessions import SessionRegistry  # noqa: E402

DEV = "pal-maple-a1b2c3"
DEMO = "pal-birch-b2c3d4"
IDLE = "pal-cedar-c3d4e5"


def _today_at(hour, minute):
    return int(datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp())


def _held_rows():
    now = int(time.time())
    return [
        {"name": IDLE, "alive": True, "status": "running", "last_activity": now},
        {"name": DEV, "alive": True, "status": "running", "last_activity": now,
         "hold": {"until": _today_at(23, 59), "reason": "npm run dev on :3000", "since": now - 60}},
        {"name": DEMO, "alive": True, "status": "running", "last_activity": now,
         "hold": {"until": None, "reason": "tunnel for the demo", "since": now - 3600}},
    ]


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    from connector_loopback import TelegramFacade
    adapter = TelegramFacade(monkeypatch, tmp_path / "wolts")
    monkeypatch.setattr(adapter, "list_wolts", lambda: [{"name": "pal", "type": "beaver"}])
    monkeypatch.setattr(adapter, "is_allowed", lambda update: True)
    return adapter


def _buttons(markup):
    return [[(b.text, b.url, b.callback_data) for b in row] for row in markup.inline_keyboard]


# --- the /holds message -------------------------------------------------------

def test_holds_lists_held_sessions_oldest_hold_first(adapter):
    text, markup = adapter._holds_message(_held_rows(), "https://lodge.example")
    lines = text.splitlines()
    assert lines[0] == "📌 2 held sessions"
    assert DEMO in lines[1] and "forever" in lines[1] and "tunnel for the demo" in lines[1]
    assert DEV in lines[2] and "until 23:59" in lines[2] and "npm run dev on :3000" in lines[2]
    assert IDLE not in text
    assert _buttons(markup) == [
        [(f"🦫 {DEMO}", f"https://lodge.example/tui?session={DEMO}", None), ("🔓", None, f"hrel:{DEMO}")],
        [(f"🦫 {DEV}", f"https://lodge.example/tui?session={DEV}", None), ("🔓", None, f"hrel:{DEV}")],
    ]


def test_holds_says_when_there_are_none(adapter):
    text, markup = adapter._holds_message(_held_rows()[:1], "https://lodge.example")
    assert text == "no held sessions."
    assert markup is None


def test_a_held_session_is_pinned_in_the_sessions_list(adapter):
    _text, markup = adapter._sessions_picker_message(_held_rows(), "https://lodge.example")
    labels = [row[0][0] for row in _buttons(markup)]
    assert f"🦫 📌 {DEV}" in labels and f"🦫 📌 {DEMO}" in labels
    assert f"🦫 {IDLE}" in labels


def _update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def test_holds_answers_the_owner_and_nobody_else(adapter, monkeypatch):
    import asyncio
    monkeypatch.setattr(adapter, "list_sessions", _held_rows)
    monkeypatch.setattr(adapter, "get_tunnel_url", lambda: "")
    update = _update()
    asyncio.run(adapter.handle_holds(update, MagicMock()))
    assert update.message.reply_text.await_args.args[0].startswith("📌 2 held sessions")

    monkeypatch.setattr(adapter, "is_allowed", lambda update: False)
    monkeypatch.setattr(adapter, "list_sessions", Mock(side_effect=AssertionError("listed sessions for a stranger")))
    update = _update()
    asyncio.run(adapter.handle_holds(update, MagicMock()))
    update.message.reply_text.assert_not_called()


# --- release ------------------------------------------------------------------

def _query(data):
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


def test_release_asks_first(adapter, monkeypatch):
    import asyncio
    released = AsyncMock()
    monkeypatch.setattr(adapter, "_release_hold", released)
    update, query = _query(f"hrel:{DEV}")
    asyncio.run(adapter.handle_callback(update, MagicMock()))
    released.assert_not_called()
    text = query.edit_message_text.await_args.args[0]
    assert DEV in text and "release" in text
    buttons = _buttons(query.edit_message_text.await_args.kwargs["reply_markup"])
    assert buttons == [[("✓ release", None, f"hrel-confirm:{DEV}"), ("cancel", None, "hrel-cancel")]]


def test_confirmed_release_deletes_the_hold_and_refreshes(adapter, monkeypatch):
    import asyncio
    released = AsyncMock(return_value=True)
    monkeypatch.setattr(adapter, "_release_hold", released)
    monkeypatch.setattr(adapter, "list_sessions", lambda: _held_rows()[:2])
    monkeypatch.setattr(adapter, "get_tunnel_url", lambda: "")
    update, query = _query(f"hrel-confirm:{DEV}")
    asyncio.run(adapter.handle_callback(update, MagicMock()))
    released.assert_awaited_once_with(DEV)
    assert query.answer.await_args.args[0] == f"released {DEV}"
    assert query.edit_message_text.await_args.args[0].startswith("📌 1 held session")


def test_release_calls_delete_on_the_lodge(adapter, monkeypatch):
    import asyncio
    import httpx
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def delete(self, url, timeout=None):
            calls.append(url)
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:7794")
    assert asyncio.run(adapter._release_hold(DEV)) is True
    assert calls == [f"http://127.0.0.1:7794/sessions/{DEV}/hold"]


# --- the data under it ----------------------------------------------------------

def _registry(tmp_path, **fields):
    registry = SessionRegistry(tmp_path)
    registry.create(DEV, wolt="pal")
    data = registry._read("pal", DEV)
    data.update({"status": "running", **fields})
    registry._write("pal", DEV, data)
    return registry


HOLD = {"until": None, "reason": "npm run dev on :3000", "since": 1}


def test_core_lists_a_hold_in_force_only(tmp_path, monkeypatch):
    from bot import core
    registry = _registry(tmp_path, hold=dict(HOLD))
    other = registry._read("pal", DEV) | {"name": DEMO, "hold": {"until": 5, "reason": "x", "since": 1}}
    registry._write("pal", DEMO, other)
    # Both alive: list_sessions projects what registry.list() says about them.
    alive = [registry._read("pal", name) | {"alive": True} for name in (DEV, DEMO)]
    monkeypatch.setattr(registry, "list", lambda *a, **k: alive)
    monkeypatch.setattr(core, "registry", registry)
    monkeypatch.setattr(core, "get_tunnel_url", lambda: "")
    rows = {row["name"]: row for row in core.list_sessions()}
    assert rows[DEV]["hold"] == HOLD
    assert "hold" not in rows[DEMO]


def test_the_lodge_view_shows_the_hold_and_no_closing_time(tmp_path, monkeypatch):
    import server.app as server_app
    registry = _registry(tmp_path, hold=dict(HOLD), last_activity=int(time.time()) - 600)
    monkeypatch.setattr("sessions.SessionRegistry", lambda *_a, **_k: registry)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "get_idle_timeout", lambda: 3600)
    runtime = Mock(); runtime.last_keys.return_value = {DEV: int(time.time()) - 600}
    monkeypatch.setattr(server_app, "get_runtime", Mock(return_value=runtime))
    monkeypatch.setattr(sessions, "_tmux_sessions", lambda *_a, **_k: {DEV})
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    rows = {row["name"]: row for row in client.get("/sessions?view=lodge").json()["sessions"]}
    assert rows[DEV]["hold"] == HOLD
    assert "closes_in_seconds" not in rows[DEV]
