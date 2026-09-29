"""The lodge's bounded session projection stays fast and semantically exact."""

import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

from starlette.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import sessions
import server.app as server_app


def _record(root: Path, wolt: str, name: str, **fields) -> None:
    target = root / wolt / ".state" / "sessions" / f"{name}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "name": name,
        "wolt": wolt,
        "status": "stopped",
        "created_at": fields.get("last_activity", 1),
        "last_activity": fields.get("last_activity", 1),
        "prompt": "must not leave the full registry",
        "prompt_preview": "must not leave either",
        "target": {"canonical_workdir": "/private/full-target"},
        "runtime": fields.pop("runtime", {}),
        **fields,
    }))


def test_lodge_view_is_bounded_slim_and_tmux_open_aware(tmp_path, monkeypatch):
    now = int(time.time())
    for index in range(10):
        _record(tmp_path, "n00b", f"old-{index}", last_activity=index + 1)
    _record(tmp_path, "n00b", "recent", last_activity=now - 60, title="Recent")
    _record(
        tmp_path, "n00b", "running", status="running", last_activity=now - 100000,
        runtime={"tmux_session_name": "running-pane"},
    )
    _record(tmp_path, "pixie", "dead-running", status="running", last_activity=1)

    agents = Mock()
    monkeypatch.setattr(sessions, "sessions_with_agent_process", agents)
    tmux = Mock(return_value={"running-pane"})
    monkeypatch.setattr(sessions, "_tmux_sessions", tmux)

    payload = sessions.SessionRegistry(tmp_path).list_lodge_view()
    rows = {row["name"]: row for row in payload["sessions"]}

    assert payload["totals"] == {"n00b": 12, "pixie": 1}
    assert "old-0" not in rows and "old-1" not in rows
    assert {f"old-{index}" for index in range(4, 10)} <= rows.keys()
    assert rows["running"]["alive"] is True
    assert rows["running"]["status"] == "running"
    assert rows["dead-running"]["status"] == "orphaned"
    assert rows["dead-running"]["alive"] is False
    agents.assert_not_called()
    tmux.assert_called_once_with()
    forbidden = {"prompt", "prompt_preview", "target", "runtime", "dir", "workdir"}
    assert all(not forbidden.intersection(row) for row in rows.values())


def test_lodge_route_is_distinct_from_the_full_sessions_contract(monkeypatch):
    full = [{"name": "full", "prompt": "kept for callers"}]
    light = {"sessions": [{"name": "light", "status": "stopped"}], "totals": {"n00b": 9}}
    monkeypatch.setattr(sessions.SessionRegistry, "list", Mock(return_value=full))
    monkeypatch.setattr(sessions.SessionRegistry, "list_lodge_view", Mock(return_value=light))
    monkeypatch.setattr(server_app, "get_idle_timeout", Mock(return_value=None))
    client = TestClient(
        server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000),
    )

    assert client.get("/sessions").json() == full
    assert client.get("/sessions?view=lodge").json() == light


def test_single_session_route_returns_agent_liveness(monkeypatch):
    record = {"name": "n00b-one", "status": "orphaned", "agent_alive": False}
    lookup = Mock(return_value=record)
    monkeypatch.setattr(sessions.SessionRegistry, "get", lookup)
    client = TestClient(
        server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000),
    )

    assert client.get("/sessions/n00b-one").json() == record
    lookup.assert_called_once_with("n00b-one", check_alive=True)


def test_single_session_route_rejects_unknown_and_unsafe_names(monkeypatch):
    lookup = Mock(return_value=None)
    monkeypatch.setattr(sessions.SessionRegistry, "get", lookup)
    client = TestClient(
        server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000),
    )

    assert client.get("/sessions/missing").status_code == 404
    assert client.get("/sessions/not%20safe").status_code == 404
    lookup.assert_called_once_with("missing", check_alive=True)
