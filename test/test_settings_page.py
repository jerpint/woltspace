"""Settings page and design-system integration tests."""

import asyncio
import json

import httpx

from server import app as app_module


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _write_wolt(root, name, creature, harness=None):
    config_dir = root / name / "wolt"
    config_dir.mkdir(parents=True)
    config = {"name": name, "type": creature}
    if harness:
        config["harness"] = harness
    (config_dir / "wolt.json").write_text(json.dumps(config))


def test_settings_page_renders_defaults_and_overrides(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    _write_wolt(tmp_path, "brook", "beaver", "codex")
    _write_wolt(tmp_path, "fang", "dog")
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "claude"}}))

    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

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
    assert 'role="dialog"' in body
    assert 'aria-labelledby="create-modal-title"' in body


def test_settings_assets_and_mutations_are_wired(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

    css = asyncio.run(_request("GET", "/static/design-system.css"))
    script = asyncio.run(_request("GET", "/static/settings.js"))
    default = asyncio.run(_request("POST", "/harness/default", json={"harness": "codex"}))
    override = asyncio.run(_request("POST", "/wolts/maple/harness", json={"harness": "claude"}))

    assert css.status_code == 200
    assert ".ds-panel" in css.text
    assert script.status_code == 200
    assert "data-default-form" in script.text
    assert default.json() == {"ok": True, "default": "codex"}
    assert override.json() == {"ok": True, "wolt": "maple", "harness": "claude", "pinned": True}
    assert json.loads((tmp_path / "woltspace.json").read_text())["harness"]["default"] == "codex"
    assert json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())["harness"] == "claude"


def test_configured_wolts_skips_broken_entries(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    broken_dir = tmp_path / "splinter" / "wolt"
    broken_dir.mkdir(parents=True)
    (broken_dir / "wolt.json").write_text("not json")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    assert [wolt["name"] for wolt in app_module._configured_wolts()] == ["maple"]
