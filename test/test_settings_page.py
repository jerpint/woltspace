"""Settings page and design-system integration tests."""

import asyncio
import json

import httpx

from server import app as app_module


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _write_wolt(root, name, creature, harness=None, execution_policy=None):
    config_dir = root / name / "wolt"
    config_dir.mkdir(parents=True)
    config = {"name": name, "type": creature}
    if harness:
        config["harness"] = harness
    if execution_policy:
        config["execution_policy"] = execution_policy
    (config_dir / "wolt.json").write_text(json.dumps(config))


def test_settings_page_renders_defaults_and_overrides(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    _write_wolt(tmp_path, "brook", "beaver", "codex")
    _write_wolt(tmp_path, "fang", "dog")
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "claude"}}))

    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("GET", "/settings"))
    body = response.text

    assert response.status_code == 200
    assert "Lodge default" in body
    assert "Agent engines" in body
    assert "maple" in body
    assert "brook" in body
    assert "Pinned · codex" in body
    assert 'data-wolt="fang"' not in body
    assert 'name="default-harness"' in body
    assert "Session permissions" in body
    assert 'data-policy-select' in body
    assert 'value="auto"' in body
    assert 'role="dialog"' in body
    assert 'aria-labelledby="create-modal-title"' in body


def test_settings_assets_and_mutations_are_wired(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))

    css = asyncio.run(_request("GET", "/static/design-system.css"))
    script = asyncio.run(_request("GET", "/static/settings.js"))
    default = asyncio.run(_request("POST", "/harness/default", json={"harness": "codex"}))
    override = asyncio.run(_request("POST", "/wolts/maple/harness", json={"harness": "claude"}))
    policy = asyncio.run(_request(
        "POST",
        "/wolts/maple/execution-policy",
        json={"execution_policy": "auto"},
    ))

    assert css.status_code == 200
    assert ".ds-panel" in css.text
    assert script.status_code == 200
    assert "data-default-form" in script.text
    assert "execution-policy" in script.text
    assert default.json() == {"ok": True, "default": "codex"}
    assert override.json() == {"ok": True, "wolt": "maple", "harness": "claude", "pinned": True}
    assert policy.json() == {
        "ok": True,
        "wolt": "maple",
        "execution_policy": "auto",
        "pinned": True,
        "source": "wolt.json",
    }
    assert json.loads((tmp_path / "woltspace.json").read_text())["harness"]["default"] == "codex"
    assert json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())["harness"] == "claude"
    assert json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())["execution_policy"] == "auto"


def test_policy_mutation_clears_to_harness_default_and_validates_guarded(
    tmp_path, monkeypatch
):
    _write_wolt(tmp_path, "maple", "raccoon", "claude", "auto")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

    invalid = asyncio.run(_request(
        "POST",
        "/wolts/maple/execution-policy",
        json={"execution_policy": "guarded"},
    ))
    assert invalid.status_code == 409
    assert "Codex" in invalid.json()["error"]

    cleared = asyncio.run(_request(
        "POST",
        "/wolts/maple/execution-policy",
        json={"execution_policy": None},
    ))
    assert cleared.json() == {
        "ok": True,
        "wolt": "maple",
        "execution_policy": "prompt",
        "pinned": False,
        "source": "harness_default",
    }
    config = json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())
    assert "execution_policy" not in config

    unknown = asyncio.run(_request(
        "POST",
        "/wolts/maple/execution-policy",
        json={"execution_policy": "hope-for-the-best"},
    ))
    assert unknown.status_code == 400


def test_configured_wolts_skips_broken_entries(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    broken_dir = tmp_path / "splinter" / "wolt"
    broken_dir.mkdir(parents=True)
    (broken_dir / "wolt.json").write_text("not json")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    assert [wolt["name"] for wolt in app_module._configured_wolts()] == ["maple"]
