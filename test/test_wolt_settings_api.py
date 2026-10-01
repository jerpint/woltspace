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


def test_settings_api_accepts_freeform_runtime_harness_models(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    response = _client(tmp_path, monkeypatch).patch(
        "/wolts/n00b/settings",
        json={"harness": "opencode", "model": "made-up/provider-model"},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "made-up/provider-model"
    assert json.loads(path.read_text())["model"] == "made-up/provider-model"


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


def test_rename_changes_only_the_display_name(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    client = _client(tmp_path, monkeypatch)
    response = client.patch("/wolts/n00b/settings", json={"display_name": "  Noob  the First "})
    assert response.status_code == 200
    assert response.json()["display_name"] == "Noob the First"
    saved = json.loads(path.read_text())
    assert saved["display_name"] == "Noob the First"
    # the slug, the engine and the model are untouched
    assert (saved["name"], saved["harness"], saved["model"]) == ("n00b", "claude", "opus")
    assert client.get("/wolts/n00b/settings").json()["display_name"] == "Noob the First"

    # an empty name, or the folder name itself, goes back to the plain name
    for back in ("", "n00b", None):
        client.patch("/wolts/n00b/settings", json={"display_name": "Temp"})
        assert client.patch("/wolts/n00b/settings", json={"display_name": back}).status_code == 200
        assert "display_name" not in json.loads(path.read_text())


def test_rename_is_validated_with_the_rest_before_one_write(tmp_path, monkeypatch):
    path = _layout(tmp_path)
    before = path.read_text()
    client = _client(tmp_path, monkeypatch)
    for body in (
        {"display_name": "x" * 41},
        {"display_name": ["Noob"]},
        {"display_name": "Fine Name", "model": "not-a-model"},
        {"display_name": "Fine Name", "slug": "other"},
    ):
        assert client.patch("/wolts/n00b/settings", json=body).status_code == 400, body
        assert path.read_text() == before, body

    both = client.patch("/wolts/n00b/settings", json={"display_name": "Noob", "model": "sonnet"})
    assert both.status_code == 200
    saved = json.loads(path.read_text())
    assert (saved["display_name"], saved["model"]) == ("Noob", "sonnet")


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


def test_wolt_cli_json_errors_exit_nonzero(capsys):
    cli = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    cli["cmd_wolt_set"].__globals__["_req"] = lambda *_args, **_kwargs: (
        400, {"error": "invalid pair"}
    )
    try:
        cli["cmd_wolt_set"](
            argparse.Namespace(name="n00b", harness="codex", model="opus", json=True)
        )
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError("JSON error returned success")
    assert "invalid pair" in capsys.readouterr().err


def test_lodge_settings_no_longer_edits_individual_wolts():
    template = (ROOT / "templates" / "settings.html").read_text()
    script = (ROOT / "public" / "static" / "settings.js").read_text()
    assert "Wolt harnesses" not in template
    assert "data-wolt-select" not in script


def test_wolt_settings_tab_uses_atomic_api_and_one_tap_choices():
    source = (ROOT / "public" / "static" / "wolt-page.js").read_text()
    assert "method: 'PATCH'" in source
    assert "wolt-choice" in source
    assert "applies from the next session" in source
    assert "provider/model" in source
