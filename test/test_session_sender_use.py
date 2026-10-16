"""A session that talks is in use: its own notify, message or spawn stamps it.

The stamp is last_activity only, on a running record only, under the session
lock: a touch that races a rest must never write running back over resting.
"""

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app  # noqa: E402
from sessions import SessionRegistry  # noqa: E402

SENDER = "pal-maple-a1b2c3"


def _registry(tmp_path, monkeypatch, **fields):
    registry = SessionRegistry(tmp_path)
    registry.create(SENDER, wolt="pal")
    data = registry._read("pal", SENDER)
    data.update({"status": "running", "last_activity": 1000, **fields})
    registry._write("pal", SENDER, data)
    monkeypatch.setattr("sessions.SessionRegistry", lambda *_a, **_k: registry)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    return registry


def _client():
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _used(registry):
    return registry._read("pal", SENDER)["last_activity"]


# --- touch() ------------------------------------------------------------------

def test_touch_never_writes_over_a_session_that_left_running(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, status="resting", rested_at=1000)
    assert registry.touch(SENDER, wolt="pal") is False
    stored = registry._read("pal", SENDER)
    assert stored["status"] == "resting"
    assert stored["last_activity"] == 1000


def test_touch_reads_and_writes_under_the_session_lock(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    held = []
    real_lock, real_read = registry._lock, registry._read

    @contextmanager
    def tracking_lock(*args):
        with real_lock(*args):
            held.append(True)
            try:
                yield
            finally:
                held.pop()

    reads_locked = []

    def tracking_read(*args):
        reads_locked.append(bool(held))
        return real_read(*args)

    registry._lock, registry._read = tracking_lock, tracking_read
    assert registry.touch(SENDER, wolt="pal") is True
    assert reads_locked and all(reads_locked)
    assert _used(registry) >= int(time.time()) - 5


# --- notify -------------------------------------------------------------------

def test_a_sessions_own_notify_is_use(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "send_notification", AsyncMock(return_value={"adapter": "telegram"}))
    before = int(time.time())
    response = _client().post("/notify", json={"message": "built it", "session": SENDER})
    assert response.status_code == 200
    assert _used(registry) >= before


def test_a_notify_that_did_not_go_is_not_use(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "send_notification",
                        AsyncMock(side_effect=server_app.NoNotificationTarget("no chat")))
    response = _client().post("/notify", json={"message": "built it", "session": SENDER})
    assert response.status_code == 409
    assert _used(registry) == 1000


def test_a_notify_never_wakes_or_rewrites_a_resting_sender(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, status="resting")
    monkeypatch.setattr(server_app, "send_notification", AsyncMock(return_value={"adapter": "telegram"}))
    assert _client().post("/notify", json={"message": "hi", "session": SENDER}).status_code == 200
    stored = registry._read("pal", SENDER)
    assert stored["status"] == "resting" and stored["last_activity"] == 1000


def test_a_notify_without_a_session_stamps_nothing(tmp_path, monkeypatch):
    _registry(tmp_path, monkeypatch)
    touched = Mock()
    monkeypatch.setattr(SessionRegistry, "touch", touched)
    monkeypatch.setattr(server_app, "send_notification", AsyncMock(return_value={"adapter": "telegram"}))
    assert _client().post("/notify", json={"message": "🦅 cleaned up", "session": ""}).status_code == 200
    touched.assert_not_called()


# --- messages and IWCL --------------------------------------------------------

def test_sending_a_message_is_use_for_the_sender(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "delivered", "session": "pal-other-ffffff"}))
    before = int(time.time())
    response = _client().post("/sessions/pal-other-ffffff/message",
                              json={"text": "done", "from_wolt": "pal", "from_session": SENDER})
    assert response.status_code == 200
    assert _used(registry) >= before


def test_a_message_that_woke_its_target_is_use_for_the_sender(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    other = registry._read("pal", SENDER) | {"name": "pal-other-ffffff", "status": "resting"}
    registry._write("pal", "pal-other-ffffff", other)
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "session-dead"}))
    monkeypatch.setattr(server_app, "resume_session", Mock(return_value={"name": "pal-other-ffffff"}))
    before = int(time.time())
    response = _client().post("/sessions/pal-other-ffffff/message", json={"text": "wake", "from_session": SENDER})
    assert response.status_code == 200
    assert _used(registry) >= before


def test_a_message_that_did_not_land_is_not_use(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "no-session"}))
    response = _client().post("/sessions/pal-other-ffffff/message", json={"text": "hi", "from_session": SENDER})
    assert response.status_code == 404
    assert _used(registry) == 1000


# --- spawn --------------------------------------------------------------------

def test_spawning_a_session_is_use_for_the_spawner(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    monkeypatch.setattr(server_app, "start_session", Mock(return_value={"name": "pal-child-abcdef"}))
    before = int(time.time())
    response = _client().post("/sessions/new/lodge", json={"wolt": "pal", "prompt": "go", "from_session": SENDER})
    assert response.status_code == 200
    assert _used(registry) >= before


@pytest.mark.parametrize("name", ["main", "../etc", "pal maple"])
def test_only_a_plain_session_id_is_stamped(tmp_path, monkeypatch, name):
    _registry(tmp_path, monkeypatch)
    touched = Mock()
    monkeypatch.setattr(SessionRegistry, "touch", touched)
    server_app._touch_origin(name)
    touched.assert_not_called()
