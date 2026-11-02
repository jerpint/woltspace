"""A hold keeps a running session from resting while a background task runs.

The route is the only writer; the registry drops the hold on every write that
leaves "running"; the CLI is a thin client; the vulture names held sessions.
"""

import argparse
import runpy
from types import SimpleNamespace
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app  # noqa: E402
from sessions import SessionRegistry  # noqa: E402

NAME = "pal-maple-a1b2c3"
HOLD = {"until": None, "reason": "npm run dev on :3000", "since": 1}


@pytest.fixture(autouse=True)
def _vulture_state_in_tmp(tmp_path, monkeypatch):
    from creatures import vulture
    state = tmp_path / ".vulture-state"
    monkeypatch.setattr(vulture, "STATE_DIR", state)
    monkeypatch.setattr(vulture, "LOG_FILE", state / "vulture.log")
    monkeypatch.setattr(vulture, "LAST_RUN_FILE", state / "last-run")


def _registry(tmp_path, monkeypatch, **fields):
    registry = SessionRegistry(tmp_path)
    registry.create(NAME, wolt="pal")
    data = registry._read("pal", NAME)
    data.update({"status": "running", "last_activity": 1000, **fields})
    registry._write("pal", NAME, data)
    monkeypatch.setattr("sessions.SessionRegistry", lambda *_a, **_k: registry)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    return registry


def _client():
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _stored(registry):
    return registry._read("pal", NAME)


# --- the route ---------------------------------------------------------------

def test_a_hold_for_a_duration_records_until_reason_and_since(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    before = int(time.time())
    response = _client().post(f"/sessions/{NAME}/hold", json={"seconds": 21600, "reason": "npm run dev on :3000"})
    after = int(time.time())
    assert response.status_code == 200, response.json()
    hold = _stored(registry)["hold"]
    assert isinstance(hold["until"], int)
    assert before + 21600 <= hold["until"] <= after + 21600
    assert before <= hold["since"] <= after
    assert hold["reason"] == "npm run dev on :3000"
    assert response.json()["hold"] == hold
    assert _stored(registry)["last_activity"] >= before  # taking a hold is use


def test_a_hold_without_seconds_is_forever(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    response = _client().post(f"/sessions/{NAME}/hold", json={"seconds": None, "reason": "tunnel for the demo"})
    assert response.status_code == 200
    assert _stored(registry)["hold"]["until"] is None


def test_the_reason_is_cleaned_like_a_description(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    response = _client().post(f"/sessions/{NAME}/hold", json={"seconds": 60, "reason": "  npm\x00  run\n dev\x1b "})
    assert response.status_code == 200
    assert _stored(registry)["hold"]["reason"] == "npm run dev"


@pytest.mark.parametrize("body", [
    {"reason": "build"},                      # no seconds at all
    {"seconds": 0, "reason": "build"},
    {"seconds": "6h", "reason": "build"},
    {"seconds": True, "reason": "build"},
    {"seconds": 10 ** 12, "reason": "build"},  # past any printable date: use null
    {"seconds": 60, "reason": "\x00\x1b"},  # no reason left once cleaned
    {"seconds": 60, "reason": "x" * 201},
    [60, "build"],                            # not an object
])
def test_a_bad_hold_is_refused(tmp_path, monkeypatch, body):
    registry = _registry(tmp_path, monkeypatch)
    response = _client().post(f"/sessions/{NAME}/hold", json=body)
    assert response.status_code == 400
    assert "hold" not in _stored(registry)


def test_a_session_that_is_not_running_cannot_hold(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, status="resting")
    response = _client().post(f"/sessions/{NAME}/hold", json={"seconds": 60, "reason": "build"})
    assert response.status_code == 409
    assert "hold" not in _stored(registry)


def test_release_drops_the_hold_without_counting_as_use(tmp_path, monkeypatch):
    """Releasing lets an idle session rest on the next sweep, not a window later."""
    registry = _registry(tmp_path, monkeypatch, hold=dict(HOLD))
    response = _client().delete(f"/sessions/{NAME}/hold")
    assert response.status_code == 200
    stored = _stored(registry)
    assert "hold" not in stored
    assert stored["last_activity"] == 1000


def test_one_session_shows_its_hold_only_while_in_force(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, hold=dict(HOLD))
    monkeypatch.setattr(registry, "get", lambda *a, **k: SessionRegistry.get(registry, *a, **{**k, "check_alive": False}))
    assert _client().get(f"/sessions/{NAME}").json()["hold"]["reason"] == "npm run dev on :3000"
    data = _stored(registry)
    data["hold"] = {"until": 5, "reason": "old build", "since": 1}
    registry._write("pal", NAME, data)
    assert "hold" not in _client().get(f"/sessions/{NAME}").json()


# --- a hold ends when the session leaves running -----------------------------

@pytest.mark.parametrize("status", ["resting", "reaped", "stopped", "completed", "failed", "orphaned", "archived"])
def test_every_write_that_leaves_running_drops_the_hold(tmp_path, monkeypatch, status):
    registry = _registry(tmp_path, monkeypatch, hold=dict(HOLD))
    data = _stored(registry)
    data["status"] = status
    registry._write("pal", NAME, data)
    assert "hold" not in _stored(registry)


def test_a_running_write_keeps_the_hold(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch, hold=dict(HOLD))
    registry.update(NAME, wolt="pal", title="still working")
    assert _stored(registry)["hold"] == HOLD


def test_a_dead_held_session_settles_without_its_hold(tmp_path, monkeypatch):
    from creatures import vulture
    registry = _registry(tmp_path, monkeypatch, hold=dict(HOLD), harness_session_id="conv-1", created_at=1)
    data = _stored(registry)
    vulture._settle_dead(data, int(time.time()), "tmux-session-missing")
    registry._write("pal", NAME, data)
    stored = _stored(registry)
    assert stored["status"] == "resting"
    assert "hold" not in stored


# --- the vulture names held sessions ------------------------------------------

def test_the_vulture_names_a_held_session_once_an_hour(tmp_path, monkeypatch):
    from creatures import vulture
    monkeypatch.setattr(vulture, "_held_logged", {})
    runtime = Mock(); runtime.last_keys.return_value = {NAME: 1000}
    monkeypatch.setattr(vulture, "get_runtime", Mock(return_value=runtime))
    rest = Mock(return_value=True); monkeypatch.setattr(vulture, "_rest_via_api", rest)
    registry = Mock(); registry.list.return_value = [
        {"name": NAME, "status": "running", "created_at": 1, "harness_session_id": "conv-1", "hold": dict(HOLD)}]

    for now in (100000, 100600, 103599, 103600):
        assert vulture._rest_idle_sessions(registry, {NAME}, now, 3600, False) == []
    lines = [line for line in vulture.LOG_FILE.read_text().splitlines() if "held" in line]
    assert len(lines) == 2
    assert f"held (forever): {NAME}: npm run dev on :3000" in lines[0]
    rest.assert_not_called()


def test_the_held_line_prints_until_in_utc_like_its_stamp(tmp_path, monkeypatch):
    """Every log line is stamped in UTC; an until in local time read as expired."""
    import os
    from datetime import datetime, timezone
    from creatures import vulture
    until = int(time.time()) + 7200
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"  # any zone that is not UTC
    time.tzset()
    try:
        monkeypatch.setattr(vulture, "_held_logged", {})
        vulture._log_held({"name": NAME, "hold": {"until": until, "reason": "build", "since": 1}}, 100000)
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    utc = datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M")
    assert f"held (until {utc}): {NAME}: build" in vulture.LOG_FILE.read_text()


# --- a hold no clock can print is no hold ---------------------------------------

HUGE = {"until": 10 ** 18, "reason": "hand-edited", "since": 1}
# A valid hold with a hand-edited since: /holds prints since, so it must be dropped.
BAD_SINCE = {"until": None, "reason": "hand-edited since", "since": 10 ** 18}


def test_an_until_past_the_routes_cap_is_no_hold():
    """Only the route writes holds, and it caps them at 100 years. A hand-edited
    until beyond that is malformed: no hold, and nothing to print."""
    import session_expiry
    now = int(time.time())
    assert not session_expiry.hold_active({"status": "running", "hold": dict(HUGE)}, now)
    assert session_expiry.public_hold(dict(HUGE)) is None
    edge = {"until": now + session_expiry.HOLD_MAX_SECONDS, "reason": "longest", "since": now}
    assert session_expiry.hold_active({"status": "running", "hold": edge}, now)
    assert session_expiry.public_hold(edge) == edge


def test_no_printer_raises_on_a_hand_edited_huge_until(tmp_path, monkeypatch):
    from bot import core
    import lodge_commands
    from creatures import vulture

    registry = _registry(tmp_path, monkeypatch, hold=dict(HUGE), harness_session_id="conv-1", created_at=1)

    # The vulture: no hold, so the idle session rests, and the pass goes on.
    monkeypatch.setattr(vulture, "_held_logged", {})
    runtime = Mock(); runtime.last_keys.return_value = {NAME: 1000}
    monkeypatch.setattr(vulture, "get_runtime", Mock(return_value=runtime))
    rest = Mock(return_value=True); monkeypatch.setattr(vulture, "_rest_via_api", rest)
    listed = Mock(); listed.list.return_value = [_stored(registry)]
    assert vulture._rest_idle_sessions(listed, {NAME}, 100000, 3600, False) == [NAME]

    # Telegram /holds: nothing held.
    alive = [_stored(registry) | {"alive": True}]
    monkeypatch.setattr(registry, "list", lambda *a, **k: alive)
    monkeypatch.setattr(core, "registry", registry)
    monkeypatch.setattr(core, "get_tunnel_url", lambda: "")
    ports = SimpleNamespace(session_emoji=lambda name: "🦫", shown=lambda name: name)
    reply = lodge_commands.holds_list(ports, core.list_sessions(), None)
    assert reply.text == "no held sessions." and not reply.choices

    # The CLI's `hold` show, fed what the lodge answers for this session.
    monkeypatch.setattr(registry, "get", lambda *a, **k: SessionRegistry.get(registry, *a, **{**k, "check_alive": False}))
    answer = _client().get(f"/sessions/{NAME}").json()
    assert "hold" not in answer
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    client["cmd_session_hold"].__globals__["_req"] = lambda *a, **k: (200, answer)
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", NAME)
    client["cmd_session_hold"](_hold_args())

    # A hold with a since no clock can print still holds; /holds just leaves since out.
    data = _stored(registry)
    data["hold"] = dict(BAD_SINCE)
    registry._write("pal", NAME, data)
    alive[:] = [_stored(registry) | {"alive": True}]
    text = lodge_commands.holds_list(ports, core.list_sessions(), None).text
    assert "hand-edited since" in text and "since" not in text.split("hand-edited since", 1)[1]


# --- the CLI ------------------------------------------------------------------

def _cli(monkeypatch, answer=(200, {})):
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    sent = []

    def fake_req(method, path, body=None):
        sent.append((method, path, body))
        return answer

    client["cmd_session_hold"].__globals__["_req"] = fake_req
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", NAME)
    return client, sent


def _hold_args(*words, session=""):
    return argparse.Namespace(words=list(words), session=session, json=False)


@pytest.mark.parametrize("duration,seconds", [("90m", 5400), ("forever", None)])
def test_cli_hold_posts_the_duration_and_reason(monkeypatch, capsys, duration, seconds):
    client, sent = _cli(monkeypatch, (200, {"hold": {"until": None, "reason": "build", "since": 1}}))
    client["cmd_session_hold"](_hold_args(duration, "npm", "run", "build"))
    assert sent == [("POST", f"/sessions/{NAME}/hold", {"seconds": seconds, "reason": "npm run build"})]
    assert NAME in capsys.readouterr().out


def test_cli_hold_off_releases(monkeypatch, capsys):
    client, sent = _cli(monkeypatch)
    client["cmd_session_hold"](_hold_args("off"))
    assert sent == [("DELETE", f"/sessions/{NAME}/hold", None)]
    assert "released" in capsys.readouterr().out


def test_cli_hold_alone_shows_the_hold(monkeypatch, capsys):
    client, sent = _cli(monkeypatch, (200, {"name": NAME, "status": "running", "hold": HOLD}))
    client["cmd_session_hold"](_hold_args())
    assert sent == [("GET", f"/sessions/{NAME}", None)]
    out = capsys.readouterr().out
    assert "forever" in out and "npm run dev on :3000" in out
    client, _ = _cli(monkeypatch, (200, {"name": NAME, "status": "running"}))
    client["cmd_session_hold"](_hold_args())
    assert "no hold" in capsys.readouterr().out


@pytest.mark.parametrize("words", [("6h",), ("soon", "build"), ("off", "now")])
def test_cli_hold_refuses_a_bad_request_before_sending(monkeypatch, capsys, words):
    client, sent = _cli(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        client["cmd_session_hold"](_hold_args(*words))
    assert stopped.value.code == 2
    assert sent == []
    assert capsys.readouterr().err
