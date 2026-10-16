"""The lodge-wide IWCL log: one `.space/logs/iwcl.jsonl` line per attempt."""

import asyncio
import json

import httpx
import pytest

from server import app as app_module
from server import iwcl_log


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:7777") as client:
        return await client.request(method, path, **kwargs)


@pytest.fixture
def wolts(tmp_path, monkeypatch):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    (wolts / "maple" / "wolt").mkdir(parents=True)
    monkeypatch.setattr(app_module, "WOLTS_DIR", wolts)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    monkeypatch.delenv("WOLTSPACE_IWCL_LOG", raising=False)

    class Registry:
        def __init__(self, *_a, **_k):
            pass

        def get(self, name, **_k):
            return {"name": name, "wolt": "maple", "status": "running"}

    monkeypatch.setattr(sessions, "SessionRegistry", Registry)
    return wolts


def _lines(wolts):
    path = iwcl_log.log_path(wolts)
    return [json.loads(line) for line in path.read_text().splitlines()]


def _send(monkeypatch, status, **body):
    monkeypatch.setattr(
        app_module, "deliver_message",
        lambda safe, text, **_k: {"status": status, "session": safe},
    )
    return asyncio.run(_request("POST", "/sessions/maple-s1/message", json={"text": "hi maple", **body}))


def test_a_delivered_message_is_one_full_line(wolts, monkeypatch):
    response = _send(monkeypatch, "delivered", from_wolt="softie", from_session="softie-s9")
    assert response.status_code == 200
    [line] = _lines(wolts)
    assert line["v"] == 1 and line["event"] == "message" and line["claimed"] is True
    assert (line["from_wolt"], line["from_session"]) == ("softie", "softie-s9")
    assert (line["to_wolt"], line["to_session"]) == ("maple", "maple-s1")
    assert (line["text"], line["status"]) == ("hi maple", "delivered")
    assert "error" not in line and line["id"] and line["ts"] > 0


def test_a_refused_message_is_logged_with_its_status(wolts, monkeypatch):
    response = _send(monkeypatch, "no-session", from_wolt="softie")
    assert response.status_code == 404
    [line] = _lines(wolts)
    assert line["status"] == "no-session"


def test_messages_to_a_wolt_name_are_logged(wolts, monkeypatch):
    monkeypatch.setattr(app_module, "resolve_active_session", lambda *_a, **_k: "maple-s1")
    monkeypatch.setattr(app_module, "deliver_message", lambda safe, text, **_k: {"status": "delivered"})
    response = asyncio.run(_request("POST", "/wolts/maple/message", json={"text": "yo", "from_wolt": "softie"}))
    assert response.status_code == 200
    [line] = _lines(wolts)
    assert (line["to_session"], line["text"]) == ("maple-s1", "yo")


def test_meta_mode_drops_the_text_and_off_writes_nothing(wolts, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_IWCL_LOG", "meta")
    _send(monkeypatch, "delivered", from_wolt="softie")
    [line] = _lines(wolts)
    assert "text" not in line and line["status"] == "delivered"
    monkeypatch.setenv("WOLTSPACE_IWCL_LOG", "off")
    _send(monkeypatch, "delivered", from_wolt="softie")
    assert len(_lines(wolts)) == 1


def test_a_wolt_started_spawn_is_logged_and_a_ui_spawn_is_not(wolts, monkeypatch):
    monkeypatch.setattr(app_module, "start_session", lambda **_k: {"name": "maple-new", "wolt": "maple"})
    asyncio.run(_request("POST", "/sessions/new/lodge", json={"wolt": "maple", "prompt": "build it"}))
    assert not iwcl_log.log_path(wolts).exists()
    asyncio.run(_request("POST", "/sessions/new/lodge", json={
        "wolt": "maple", "prompt": "build it", "from_wolt": "softie", "from_session": "softie-s9",
    }))
    [line] = _lines(wolts)
    assert (line["event"], line["status"], line["text"]) == ("spawn", "started", "build it")
    assert (line["to_wolt"], line["to_session"], line["from_wolt"]) == ("maple", "maple-new", "softie")


def test_the_log_rotates_one_generation(wolts, monkeypatch):
    monkeypatch.setattr(iwcl_log, "ROTATE_BYTES", 400)
    for _ in range(4):
        iwcl_log.record(wolts, "message", text="x" * 150, status="delivered")
    path = iwcl_log.log_path(wolts)
    assert path.with_name("iwcl.jsonl.1").exists()
    assert path.stat().st_size <= 400
    for line in path.read_text().splitlines():
        assert json.loads(line)["v"] == 1


def test_a_broken_log_never_fails_the_delivery(wolts, monkeypatch):
    (wolts / ".space").mkdir()
    (wolts / ".space" / "logs").write_text("not a directory")
    response = _send(monkeypatch, "delivered", from_wolt="softie")
    assert response.status_code == 200
