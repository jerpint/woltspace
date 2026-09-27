import json
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import ANY, Mock

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app
import session_expiry


def _client():
    return TestClient(server_app.app, base_url="http://localhost:7777")


@contextmanager
def _unlocked(*_args):
    yield


def _registry_mock():
    registry = Mock()
    registry._lock = _unlocked
    return registry


def test_policy_defaults_to_never_and_round_trips(tmp_path, monkeypatch):
    path = tmp_path / "woltspace.json"
    monkeypatch.setattr(session_expiry, "_config_path", lambda: path)
    assert session_expiry.get_idle_timeout() is None
    assert session_expiry.set_idle_timeout(14400) == 14400
    assert session_expiry.get_idle_timeout() == 14400
    assert json.loads(path.read_text())["sessions"]["idle_timeout_seconds"] == 14400


def test_policy_rejects_unknown_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(session_expiry, "_config_path", lambda: tmp_path / "woltspace.json")
    try:
        session_expiry.set_idle_timeout(12)
    except ValueError as exc:
        assert "3600" in str(exc)
    else:
        raise AssertionError("invalid duration accepted")


def test_setting_route_is_the_write_authority(monkeypatch):
    saved = Mock(return_value=3600)
    monkeypatch.setattr(server_app, "set_idle_timeout", saved)
    response = _client().post("/settings/session-expiry", json={"idle_timeout_seconds": 3600})
    assert response.status_code == 200
    assert response.json()["idle_timeout_seconds"] == 3600
    saved.assert_called_once_with(3600)


def test_setting_route_rejects_non_object_json():
    response = _client().post("/settings/session-expiry", json=[])
    assert response.status_code == 400
    assert response.json() == {"error": "JSON object body required"}


def test_setting_route_requires_the_timeout_field():
    response = _client().post("/settings/session-expiry", json={})
    assert response.status_code == 400
    assert response.json() == {"error": "idle_timeout_seconds required"}


def test_garbage_config_means_never(tmp_path, monkeypatch):
    path = tmp_path / "woltspace.json"
    monkeypatch.setattr(session_expiry, "_config_path", lambda: path)
    for content in ("[]", '{"sessions": []}', "not-json"):
        path.write_text(content)
        assert session_expiry.get_idle_timeout() is None


def test_dead_iwcl_session_resumes_with_attribution(monkeypatch):
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "session-dead"}))
    resumed = Mock(return_value={"name": "friend", "status": "resumed"})
    monkeypatch.setattr(server_app, "resume_session", resumed)
    registry = _registry_mock()
    registry.get.return_value = {"name": "friend", "wolt": "pal", "status": "resting"}
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    response = _client().post("/sessions/friend/message", json={"text": "hello", "from_wolt": "n00b", "from_session": "n00b-one"})
    assert response.status_code == 200
    assert response.json()["status"] == "resumed"
    prompt = resumed.call_args.args[1]
    assert prompt.startswith("[message from n00b, session=n00b-one]\nhello")


def test_stopped_session_is_not_resumed_on_contact(monkeypatch):
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "session-dead", "session": "friend"}))
    resumed = Mock()
    monkeypatch.setattr(server_app, "resume_session", resumed)
    registry = _registry_mock()
    registry.get.return_value = {"name": "friend", "wolt": "pal", "status": "stopped"}
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    response = _client().post("/sessions/friend/message", json={"text": "hello"})
    assert response.status_code == 409
    resumed.assert_not_called()


def test_running_session_with_dead_tmux_is_resumed_on_contact(monkeypatch):
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "session-dead", "session": "friend"}))
    resumed = Mock(return_value={"name": "friend", "status": "resumed"})
    monkeypatch.setattr(server_app, "resume_session", resumed)
    registry = _registry_mock()
    registry.get.return_value = {"name": "friend", "wolt": "pal", "status": "running"}
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    response = _client().post("/sessions/friend/message", json={"text": "hello"})
    assert response.status_code == 200
    assert response.json()["status"] == "resumed"
    resumed.assert_called_once()


def test_by_name_message_falls_back_to_latest_resting_session(monkeypatch):
    monkeypatch.setattr(server_app, "resolve_active_session", Mock(return_value=None))
    registry = _registry_mock()
    registry.list.return_value = [
        {"name": "older", "wolt": "pal", "status": "resting", "last_activity": 10},
        {"name": "newer", "wolt": "pal", "status": "resting", "last_activity": 20},
    ]
    registry.get.return_value = registry.list.return_value[1]
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    monkeypatch.setattr(server_app, "deliver_message", Mock(return_value={"status": "session-dead", "session": "newer"}))
    resumed = Mock(return_value={"name": "newer", "status": "resumed"})
    monkeypatch.setattr(server_app, "resume_session", resumed)
    response = _client().post("/wolts/pal/message", json={"text": "wake up"})
    assert response.status_code == 200
    assert response.json()["session"] == "newer"
    resumed.assert_called_once()


def test_rest_route_preserves_resume_identity(monkeypatch):
    record = {"name": "friend", "wolt": "pal", "status": "running", "harness_session_id": "fixed-id"}
    registry = _registry_mock()
    registry.get.return_value = record
    registry_type = Mock(return_value=registry)
    monkeypatch.setattr("sessions.SessionRegistry", registry_type)
    runtime = Mock()
    runtime.capture.return_value = "unchanged"
    monkeypatch.setattr(server_app, "get_runtime", Mock(return_value=runtime))
    import hashlib
    digest = hashlib.sha256(b"unchanged").hexdigest()
    response = _client().post("/sessions/friend/rest", json={"pane_digest": digest})
    assert response.status_code == 200
    runtime.stop.assert_called_once()
    written = registry._write.call_args.args[2]
    assert written["status"] == "resting"
    assert written["harness_session_id"] == "fixed-id"


def test_main_session_can_never_rest():
    response = _client().post("/sessions/main/rest", json={})
    assert response.status_code == 403
    assert response.json() == {"error": "main session cannot rest"}


def test_rest_aborts_if_pane_changed_after_idle_observation(monkeypatch):
    record = {"name": "friend", "wolt": "pal", "status": "running"}
    registry = _registry_mock(); registry.get.return_value = record
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    runtime = Mock(); runtime.capture.return_value = "new output"
    monkeypatch.setattr(server_app, "get_runtime", Mock(return_value=runtime))
    response = _client().post("/sessions/friend/rest", json={"pane_digest": "0" * 64})
    assert response.status_code == 409
    runtime.stop.assert_not_called()
    registry._write.assert_not_called()


def test_rest_aborts_if_recapture_fails(monkeypatch):
    record = {"name": "friend", "wolt": "pal", "status": "running"}
    registry = _registry_mock(); registry.get.return_value = record
    monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))
    runtime = Mock(); runtime.capture.side_effect = RuntimeError("tmux unavailable")
    monkeypatch.setattr(server_app, "get_runtime", Mock(return_value=runtime))
    response = _client().post("/sessions/friend/rest", json={"pane_digest": "0" * 64})
    assert response.status_code == 409
    runtime.stop.assert_not_called()


def test_vulture_requires_a_full_unchanged_window(tmp_path, monkeypatch):
    from creatures import vulture

    activity = tmp_path / "activity.json"
    monkeypatch.setattr(vulture, "ACTIVITY_FILE", activity)
    monkeypatch.setattr(vulture, "STATE_DIR", tmp_path)
    runtime = Mock()
    runtime.capture.return_value = "same pane"
    monkeypatch.setattr(vulture, "get_runtime", Mock(return_value=runtime))
    rest = Mock(return_value=True)
    monkeypatch.setattr(vulture, "_rest_via_api", rest)
    registry = Mock()
    registry.list.return_value = [{
        "name": "friend", "wolt": "pal", "status": "running",
        "created_at": 1, "runtime": {"tmux_session_name": "friend"},
    }]

    assert vulture._rest_idle_sessions(registry, {"friend"}, 1000, 3600, False) == []
    assert vulture._rest_idle_sessions(registry, {"friend"}, 4599, 3600, False) == []
    assert vulture._rest_idle_sessions(registry, {"friend"}, 4600, 3600, False) == ["friend"]
    rest.assert_called_once_with("friend", ANY)


def test_pane_change_resets_idle_window(tmp_path, monkeypatch):
    from creatures import vulture

    monkeypatch.setattr(vulture, "ACTIVITY_FILE", tmp_path / "activity.json")
    monkeypatch.setattr(vulture, "STATE_DIR", tmp_path)
    runtime = Mock()
    runtime.capture.side_effect = ["first", "changed", "changed"]
    monkeypatch.setattr(vulture, "get_runtime", Mock(return_value=runtime))
    rest = Mock(return_value=True)
    monkeypatch.setattr(vulture, "_rest_via_api", rest)
    registry = Mock()
    registry.list.return_value = [{"name": "friend", "status": "running", "created_at": 1}]

    vulture._rest_idle_sessions(registry, {"friend"}, 1000, 3600, False)
    vulture._rest_idle_sessions(registry, {"friend"}, 4600, 3600, False)
    assert vulture._rest_idle_sessions(registry, {"friend"}, 8199, 3600, False) == []
    rest.assert_not_called()


def test_capture_failure_leaves_session_open(tmp_path, monkeypatch):
    from creatures import vulture
    monkeypatch.setattr(vulture, "ACTIVITY_FILE", tmp_path / "activity.json")
    monkeypatch.setattr(vulture, "STATE_DIR", tmp_path)
    runtime = Mock(); runtime.capture.side_effect = RuntimeError("no pane")
    monkeypatch.setattr(vulture, "get_runtime", Mock(return_value=runtime))
    rest = Mock(return_value=True); monkeypatch.setattr(vulture, "_rest_via_api", rest)
    registry = Mock(); registry.list.return_value = [{"name": "friend", "status": "running", "created_at": 1}]
    assert vulture._rest_idle_sessions(registry, {"friend"}, 5000, 3600, False) == []
    rest.assert_not_called()
