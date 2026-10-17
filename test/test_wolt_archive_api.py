"""The lodge routes for archive, unarchive and delete, and create's archived-name check.

The page and the CLI only call these routes; the file work is wolt_archive's,
covered in test_wolt_archive.py. Runs on tmp_path with a fake runtime.
"""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app  # noqa: E402
import sessions  # noqa: E402
import wolts  # noqa: E402
from sessions import SessionRegistry  # noqa: E402

RUNNING = "pal-maple-a1b2c3"
RESTING = "pal-birch-d4e5f6"


@pytest.fixture
def client(tmp_path, monkeypatch, live_runtime):
    for module in (server_app, sessions, wolts):
        monkeypatch.setattr(module, "WOLTS_DIR", tmp_path)
    (tmp_path / "pal" / "wolt").mkdir(parents=True)
    (tmp_path / "pal" / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "pal", "type": "beaver", "display_name": "Pal The Builder"}))
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _session(tmp_path, name, *, status, **fields):
    registry = SessionRegistry(tmp_path)
    registry.create(name, wolt="pal")
    registry._write("pal", name, {**registry._read("pal", name), "status": status, **fields})


def test_archive_routes_round_trip(tmp_path, client, live_runtime):
    _session(tmp_path, RUNNING, status="running", harness_session_id="abc")
    live_runtime.live.add(RUNNING)

    assert client.post("/wolts/pal/archive").json() == {"archived": "pal", "stopped": [RUNNING]}
    assert "pal" not in [w["name"] for w in client.get("/wolts").json()]
    assert client.get("/wolts/archived").json() == [
        {"name": "pal", "display_name": "Pal The Builder", "type": "beaver"}]
    assert client.post("/wolts/archived/pal/unarchive").json() == {"unarchived": "pal"}
    assert "pal" in [w["name"] for w in client.get("/wolts").json()]

    assert client.post("/wolts/pal/archive").status_code == 200
    assert client.delete("/wolts/archived/pal").json() == {"deleted": "pal", "grants_revoked": 0}
    assert client.get("/wolts/archived").json() == []
    assert client.delete("/wolts/archived/pal").status_code == 404


def test_held_archive_returns_409_with_reason(tmp_path, client, live_runtime):
    _session(tmp_path, RUNNING, status="running", harness_session_id="abc",
             hold={"until": None, "reason": "npm run dev", "since": 1})
    live_runtime.live.add(RUNNING)

    response = client.post("/wolts/pal/archive")

    assert response.status_code == 409 and response.json()["error"]
    assert response.json()["held"] == [{"session": RUNNING, "reason": "npm run dev"}]
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()


@pytest.mark.parametrize("method, path", [
    ("post", "/wolts/.archive/archive"),
    ("post", "/wolts/%2e%2e/archive"),
    ("post", "/wolts/archived/.archive/unarchive"),
    ("delete", "/wolts/archived/..%2Fpal"),
])
def test_bad_name_is_refused_and_creates_nothing(tmp_path, client, method, path):
    assert getattr(client, method)(path).status_code in (400, 404, 405)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pal"]


@pytest.mark.parametrize("half_deleted", [False, True])
def test_create_refuses_a_name_the_archive_holds(tmp_path, client, half_deleted):
    """T3: also a delete that failed halfway, with no wolt.json left."""
    assert client.post("/wolts/pal/archive").status_code == 200
    if half_deleted:
        (tmp_path / ".archive" / "pal" / "wolt" / "wolt.json").unlink()

    response = client.post("/sessions/new/create", json={"name": "Pal", "type": "beaver"})

    assert response.status_code == 409
    assert response.json()["detail"] == "an archived wolt is named pal: unarchive or delete it first"
    with pytest.raises(ValueError, match="an archived wolt is named pal"):
        wolts.create_creature_wolt("pal", "beaver")
    assert not (tmp_path / "pal").exists()


@pytest.mark.parametrize("typed, slug", [("Apps", "apps"), ("projects", "projects")])
def test_create_refuses_the_apps_folders_names(tmp_path, client, typed, slug):
    """#575 review: typing "Apps" in the create dialog gave the apps root's own name."""
    response = client.post("/sessions/new/create", json={"name": typed, "type": "beaver"})

    assert response.status_code == 409
    assert response.json()["detail"] == f"{slug} is reserved for the lodge's apps; pick another name"
    assert not (tmp_path / slug).exists()


def test_old_session_links_do_not_recreate_the_folder(tmp_path, client):
    """Review Focus 5."""
    _session(tmp_path, RESTING, status="resting", harness_session_id="abc")
    assert client.post("/wolts/pal/archive").status_code == 200

    assert client.post(f"/sessions/{RESTING}/message", json={"text": "hi"}).status_code == 404
    assert client.post("/wolts/pal/message", json={"text": "hi"}).status_code == 404
    assert client.post(f"/sessions/{RESTING}/stop").status_code == 404
    assert not (tmp_path / "pal").exists()
