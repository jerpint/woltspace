import argparse
import json
import runpy
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import harnesses
from server import app as server_app


def _layout(tmp_path, *, harness="claude", model="opus", creature="beaver"):
    home = tmp_path / "n00b" / "wolt"
    home.mkdir(parents=True)
    data = {"name": "n00b", "type": creature, "harness": harness, "model": model}
    path = home / "wolt.json"
    path.write_text(json.dumps(data, indent=2) + "\n")
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "claude"}}))
    return path


def _client(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "get_default_harness", lambda: "claude")
    monkeypatch.setattr(harnesses, "_woltspace_json_path", lambda: tmp_path / "woltspace.json")
    return TestClient(server_app.app, base_url="http://localhost:7777")


def test_get_returns_configured_and_effective_pair(tmp_path, monkeypatch):
    _layout(tmp_path)
    payload = _client(tmp_path, monkeypatch).get("/wolts/n00b/settings").json()
    assert payload["configured"] == {"harness": "claude", "model": "opus"}
    assert payload["harness"] == "claude"
    assert payload["model"] == "opus"
    assert payload["applies"] == "next session"


def test_harness_change_resets_model_to_new_tier_default(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    response = _client(tmp_path, monkeypatch).patch(
        "/wolts/n00b/settings", json={"harness": "codex"}
    )
    assert response.status_code == 200
    assert response.json()["model"] == "gpt-5.6-terra"
    assert json.loads(path.read_text())["model"] == "gpt-5.6-terra"


def test_pair_is_validated_before_one_atomic_write(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    before = path.read_bytes()
    response = _client(tmp_path, monkeypatch).patch(
        "/wolts/n00b/settings", json={"harness": "codex", "model": "opus"}
    )
    assert response.status_code == 400
    assert "valid options" in response.json()["error"]
    assert path.read_bytes() == before
    assert not path.with_suffix(".tmp").exists()


def test_settings_api_catalog_gates_freeform_runtime_harnesses(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    before = path.read_bytes()
    response = _client(tmp_path, monkeypatch).patch(
        "/wolts/n00b/settings",
        json={"harness": "opencode", "model": "made-up/provider-model"},
    )
    assert response.status_code == 400
    assert "valid options" in response.json()["error"]
    assert path.read_bytes() == before


def test_valid_atomic_pair_and_model_only_update(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    client = _client(tmp_path, monkeypatch)
    response = client.patch(
        "/wolts/n00b/settings", json={"harness": "codex", "model": "gpt-5.6-sol"}
    )
    assert response.status_code == 200
    assert response.json()["configured"] == {"harness": "codex", "model": "gpt-5.6-sol"}
    response = client.patch("/wolts/n00b/settings", json={"model": "gpt-5.5"})
    assert response.status_code == 200
    assert json.loads(path.read_text())["model"] == "gpt-5.5"


def test_patch_failure_shapes_are_400_and_do_not_write(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    before = path.read_bytes()
    client = _client(tmp_path, monkeypatch)
    for kwargs in (
        {"content": "bad", "headers": {"content-type": "application/json"}},
        {"json": []},
        {"json": {}},
        {"json": {"secret": "no"}},
        {"json": {"harness": "unknown"}},
    ):
        assert client.patch("/wolts/n00b/settings", **kwargs).status_code == 400
    assert path.read_bytes() == before


def test_old_harness_route_is_an_alias_to_atomic_pair_update(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    response = _client(tmp_path, monkeypatch).post(
        "/wolts/n00b/harness", json={"harness": "codex"}
    )
    assert response.status_code == 200
    saved = json.loads(path.read_text())
    assert saved["harness"] == "codex"
    assert saved["model"] == "gpt-5.6-terra"


def test_wolt_cli_is_a_thin_patch_client(capsys):
    cli = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    calls = []
    cli["cmd_wolt_set"].__globals__["_req"] = lambda method, path, body=None: (
        calls.append((method, path, body)) or
        (200, {"wolt": "n00b", "harness": "codex", "model": "gpt-5.5"})
    )
    cli["cmd_wolt_set"](
        argparse.Namespace(name="n00b", harness="codex", model="gpt-5.5", json=False)
    )
    assert calls == [("PATCH", "/wolts/n00b/settings", {"harness": "codex", "model": "gpt-5.5"})]
    assert "next session" in capsys.readouterr().out


def test_lodge_settings_no_longer_edits_individual_wolts():
    template = (ROOT / "templates" / "settings.html").read_text()
    script = (ROOT / "public" / "static" / "settings.js").read_text()
    assert "Wolt harnesses" not in template
    assert "data-wolt-select" not in script
