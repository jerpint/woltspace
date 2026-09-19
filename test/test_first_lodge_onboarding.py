"""First lodge open chooses a harness; authentication belongs to the CLI."""

import json
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import harness_auth  # noqa: E402,F401
import harnesses  # noqa: E402,F401
import server.app as app_module
from server import state


def _wolt(root: Path, name: str, *, origin: str | None = None) -> None:
    path = root / name / "wolt" / "wolt.json"
    path.parent.mkdir(parents=True)
    config = {"name": name, "type": "raccoon"}
    if origin:
        config["origin"] = origin
    path.write_text(json.dumps(config))


def _client(root: Path, monkeypatch) -> TestClient:
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    monkeypatch.setattr(state, "WOLTS_DIR", root)
    monkeypatch.setattr(app_module, "WOLTS_DIR", root)
    return TestClient(app_module.app)


def test_empty_lodge_requires_an_explicit_harness_choice(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    assert client.get("/onboarding/status").json() == {
        "needs_harness_choice": True,
        "harness_selected": False,
        "has_user_wolt": False,
    }


def test_a_bundled_starter_does_not_complete_first_run(tmp_path, monkeypatch):
    _wolt(tmp_path, "starter", origin="starter")
    client = _client(tmp_path, monkeypatch)

    status = client.get("/onboarding/status").json()
    assert status["needs_harness_choice"] is True
    assert status["has_user_wolt"] is False


def test_an_existing_unmarked_wolt_migrates_as_user_owned(tmp_path, monkeypatch):
    _wolt(tmp_path, "old-friend")
    client = _client(tmp_path, monkeypatch)

    status = client.get("/onboarding/status").json()
    assert status["needs_harness_choice"] is False
    assert status["has_user_wolt"] is True


def test_picker_selection_is_one_atomic_product_action(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "harness_installed", lambda name: True)

    response = client.post("/onboarding/harness", json={"harness": "codex"})

    assert response.status_code == 200
    assert response.json()["needs_harness_choice"] is False
    config = json.loads((tmp_path / "woltspace.json").read_text())
    assert config["harness"]["default"] == "codex"
    assert config["onboarding"]["harness_selected"] is True


def test_missing_harness_cannot_complete_first_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(app_module, "harness_installed", lambda name: False)

    response = client.post("/onboarding/harness", json={"harness": "codex"})

    assert response.status_code == 409
    assert not (tmp_path / "woltspace.json").exists()


def test_legacy_onboard_url_returns_to_the_single_lodge_surface(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)

    response = client.get("/onboard", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "/"


def test_home_picker_is_registry_driven_and_auth_agnostic(tmp_path, monkeypatch):
    home = (ROOT / "templates" / "home.html").read_text()
    script = (ROOT / "public" / "static" / "lodge.js").read_text()

    assert "Pick your harness" in home
    assert "switch it anytime in Settings" in home
    assert "harnessList.forEach" in script
    assert "/onboarding/harness" in script
    assert "/onboard-status" not in script
    assert "id === 'claude'" not in script


def test_create_modal_uses_the_selected_harness_models():
    modal = (ROOT / "templates" / "partials" / "create-modal.html").read_text()
    script = (ROOT / "public" / "static" / "lodge.js").read_text()

    assert "opus" not in modal.lower()
    assert "sonnet" not in modal.lower()
    assert "haiku" not in modal.lower()
    assert 'id="create-harness-summary"' in modal
    assert 'id="create-harness"' in modal
    assert "modelLabelFor(createSelectedHarness, card.dataset.type)" in script
    assert "harness: createSelectedHarness" in script
    assert "This wolt will use" in script
    assert "modelLabelFor" in script
