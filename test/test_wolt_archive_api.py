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
import session_runtime  # noqa: E402
import sessions  # noqa: E402
import wolts  # noqa: E402
from sessions import SessionRegistry  # noqa: E402

from conftest import FakeSessionRuntime  # noqa: E402

RUNNING = "pal-maple-a1b2c3"
RESTING = "pal-birch-d4e5f6"


class LiveRuntime(FakeSessionRuntime):
    """A tmux session is alive while its exact name is in `live`."""

    def __init__(self):
        super().__init__()
        self.live = set()

    def is_alive(self, handle) -> bool:
        return handle.tmux_session_name in self.live

    def list_session_names(self, include_main: bool = False) -> set[str]:
        return set(self.live)

    def stop(self, handle) -> bool:
        self.live.discard(handle.tmux_session_name)
        return super().stop(handle)


@pytest.fixture
def runtime():
    fake = LiveRuntime()
    session_runtime.set_runtime(fake)
    yield fake
    session_runtime.set_runtime(None)


@pytest.fixture
def client(tmp_path, monkeypatch, runtime):
    for module in (server_app, sessions, wolts):
        monkeypatch.setattr(module, "WOLTS_DIR", tmp_path)
    return TestClient(server_app.app, base_url="http://localhost:7777")


def _wolt(tmp_path, name, **config):
    wolt_json = tmp_path / name / "wolt" / "wolt.json"
    wolt_json.parent.mkdir(parents=True)
    wolt_json.write_text(json.dumps({"name": name, "type": "beaver", **config}))


def _session(tmp_path, name, *, status, **fields):
    registry = SessionRegistry(tmp_path)
    registry.create(name, wolt="pal")
    registry._write("pal", name, {**registry._read("pal", name), "status": status, **fields})


def test_archive_routes_round_trip(tmp_path, client, runtime):
    _wolt(tmp_path, "pal", display_name="Pal The Builder")
    _session(tmp_path, RUNNING, status="running", harness_session_id="abc")
    runtime.live.add(RUNNING)

    response = client.post("/wolts/pal/archive")
    assert response.status_code == 200
    assert response.json() == {"archived": "pal", "stopped": [RUNNING]}
    assert "pal" not in [w["name"] for w in client.get("/wolts").json()]
    assert client.get("/wolts/archived").json() == [
        {"name": "pal", "display_name": "Pal The Builder", "type": "beaver"}]

    assert client.post("/wolts/archived/pal/unarchive").json() == {"unarchived": "pal"}
    assert "pal" in [w["name"] for w in client.get("/wolts").json()]
    assert client.get("/wolts/archived").json() == []

    assert client.post("/wolts/pal/archive").status_code == 200
    assert client.delete("/wolts/archived/pal").json() == {"deleted": "pal", "grants_revoked": 0}
    assert not (tmp_path / ".archive" / "pal").exists()
    assert client.get("/wolts/archived").json() == []


def test_held_archive_returns_409_with_reason(tmp_path, client, runtime):
    _wolt(tmp_path, "pal")
    _session(tmp_path, RUNNING, status="running", harness_session_id="abc",
             hold={"until": None, "reason": "npm run dev", "since": 1})
    runtime.live.add(RUNNING)

    response = client.post("/wolts/pal/archive")

    assert response.status_code == 409
    assert response.json()["held"] == [{"session": RUNNING, "reason": "npm run dev"}]
    assert response.json()["error"]
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()


def test_refusals_carry_the_status_and_an_error(tmp_path, client):
    _wolt(tmp_path, "pal")
    assert client.post("/wolts/ghost/archive").status_code == 404
    assert client.post("/wolts/archived/pal/unarchive").status_code == 404
    response = client.delete("/wolts/archived/pal")
    assert response.status_code == 404 and "pal" in response.json()["error"]
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()


@pytest.mark.parametrize("method, path", [
    ("post", "/wolts/.archive/archive"),
    ("post", "/wolts/%2e%2e/archive"),
    ("post", "/wolts/archived/.archive/unarchive"),
    ("post", "/wolts/archived/%2e%2e/unarchive"),
    ("delete", "/wolts/archived/.archive"),
    ("delete", "/wolts/archived/%2e%2e"),
    ("delete", "/wolts/archived/..%2Fpal"),
])
def test_bad_name_is_refused_and_creates_nothing(tmp_path, client, method, path):
    _wolt(tmp_path, "pal")
    response = getattr(client, method)(path)
    assert response.status_code in (400, 404, 405), response.text
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pal"]
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()


def test_create_refuses_an_archived_name(tmp_path, client):
    _wolt(tmp_path, "pal")
    assert client.post("/wolts/pal/archive").status_code == 200

    response = client.post("/sessions/new/create", json={"name": "Pal", "type": "beaver"})

    assert response.status_code == 409
    assert response.json()["detail"] == "an archived wolt is named pal: unarchive or delete it first"
    assert not (tmp_path / "pal").exists()
    with pytest.raises(ValueError, match="an archived wolt is named pal"):
        wolts.create_creature_wolt("pal", "beaver")
    assert not (tmp_path / "pal").exists()


def test_old_session_links_do_not_recreate_the_folder(tmp_path, client):
    _wolt(tmp_path, "pal")
    _session(tmp_path, RESTING, status="resting", harness_session_id="abc")
    assert client.post("/wolts/pal/archive").status_code == 200

    assert client.post(f"/sessions/{RESTING}/message", json={"text": "hi"}).status_code == 404
    assert client.post("/wolts/pal/message", json={"text": "hi"}).status_code == 404
    assert client.post(f"/sessions/{RESTING}/stop").status_code == 404
    assert not (tmp_path / "pal").exists()


def test_create_refuses_a_name_a_half_deleted_wolt_still_holds(tmp_path, client):
    (tmp_path / ".archive" / "pal" / "data").mkdir(parents=True)  # no wolt.json left

    response = client.post("/sessions/new/create", json={"name": "Pal", "type": "beaver"})

    assert response.status_code == 409
    assert response.json()["detail"] == "an archived wolt is named pal: unarchive or delete it first"
    with pytest.raises(ValueError, match="an archived wolt is named pal"):
        wolts.create_creature_wolt("pal", "beaver")
    assert not (tmp_path / "pal").exists()
