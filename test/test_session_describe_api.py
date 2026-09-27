from pathlib import Path
import sys

from starlette.testclient import TestClient


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import app as server_app  # noqa: E402
from sessions import SessionRegistry  # noqa: E402


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    return TestClient(server_app.app, base_url="http://localhost:7777")


def test_describe_updates_session_and_get_sessions_exposes_fields(tmp_path, monkeypatch):
    registry = SessionRegistry(tmp_path)
    registry.create("n00b-maple-a1b2c3", wolt="n00b", prompt="Original request")
    monkeypatch.setattr(server_app, "SessionRegistry", lambda *_: registry, raising=False)
    client = _client(tmp_path, monkeypatch)

    response = client.post(
        "/sessions/n00b-maple-a1b2c3/describe",
        json={"title": "  Lodge   redesign  ", "summary": "  Wiring\n session titles  "},
    )

    assert response.status_code == 200
    assert response.json()["title"] == "Lodge redesign"
    assert response.json()["summary"] == "Wiring session titles"
    listed = client.get("/sessions").json()
    assert listed[0]["title"] == "Lodge redesign"
    assert listed[0]["summary"] == "Wiring session titles"
    assert listed[0]["prompt_preview"] == "Original request"


def test_describe_rejects_missing_long_and_unknown_sessions(tmp_path, monkeypatch):
    registry = SessionRegistry(tmp_path)
    registry.create("n00b-maple-a1b2c3", wolt="n00b")
    client = _client(tmp_path, monkeypatch)

    assert client.post(
        "/sessions/n00b-maple-a1b2c3/describe", json={"title": "", "summary": "line"},
    ).status_code == 400
    assert client.post(
        "/sessions/n00b-maple-a1b2c3/describe", json={"title": "x" * 81, "summary": "line"},
    ).status_code == 400
    assert client.post(
        "/sessions/missing-maple-a1b2c3/describe", json={"title": "Title", "summary": "line"},
    ).status_code == 404
