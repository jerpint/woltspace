"""Settings page and design-system integration tests."""

import asyncio
import json

import httpx

from server import app as app_module


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:7777",
    ) as client:
        return await client.request(method, path, **kwargs)


def _write_wolt(root, name, creature, harness=None):
    config_dir = root / name / "wolt"
    config_dir.mkdir(parents=True)
    config = {"name": name, "type": creature}
    if harness:
        config["harness"] = harness
    (config_dir / "wolt.json").write_text(json.dumps(config))


def test_settings_page_keeps_only_lodge_wide_harness_controls(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    _write_wolt(tmp_path, "brook", "beaver", "codex")
    _write_wolt(tmp_path, "fang", "dog")
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "claude"}}))

    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("GET", "/settings"))
    body = response.text

    assert response.status_code == 200
    assert "Lodge harness" in body
    assert "Harnesses" in body
    assert "maple" not in body
    assert "brook" not in body
    assert "Use lodge default (Claude Code)" not in body
    assert "Uses ·" not in body
    assert "Follows lodge ·" not in body
    assert ">opencode<" in body
    assert "Available" not in body
    assert "Not installed" not in body
    assert "GPT-5.5" not in body
    assert "GPT-4o" not in body
    assert 'data-wolt=' not in body
    assert 'name="default-harness"' in body
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

    assert css.status_code == 200
    assert ".ds-panel" in css.text
    assert script.status_code == 200
    assert "data-default-form" in script.text
    assert default.json() == {"ok": True, "default": "codex"}
    assert override.json()["configured"] == {"harness": "claude", "model": "opus"}
    assert override.json()["applies"] == "next session"
    assert override.json()["pinned"] is True
    assert json.loads((tmp_path / "woltspace.json").read_text())["harness"]["default"] == "codex"
    assert json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())["harness"] == "claude"


def test_apps_domain_setting_is_validated_and_written_atomically(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "codex"}}))
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    saved = asyncio.run(_request("POST", "/settings/apps-domain", json={
        "apps_domain": "Owner.Woltspace.App.",
    }))
    invalid = asyncio.run(_request("POST", "/settings/apps-domain", json={
        "apps_domain": "https://owner.woltspace.app/path",
    }))

    assert saved.status_code == 200
    assert saved.json() == {"ok": True, "apps_domain": "owner.woltspace.app"}
    assert invalid.status_code == 400
    config = json.loads((tmp_path / "woltspace.json").read_text())
    assert config == {"harness": {"default": "codex"}, "apps_domain": "owner.woltspace.app"}
    assert not list(tmp_path.glob("*.tmp"))

    cleared = asyncio.run(_request("POST", "/settings/apps-domain", json={"apps_domain": None}))
    assert cleared.json() == {"ok": True, "apps_domain": None}
    assert "apps_domain" not in json.loads((tmp_path / "woltspace.json").read_text())


def test_apps_domain_is_rendered_in_settings(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({"apps_domain": "owner.woltspace.app"}))
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    response = asyncio.run(_request("GET", "/settings"))

    assert response.status_code == 200
    assert 'value="owner.woltspace.app"' in response.text
    assert "Optionally serve every app on a dedicated domain." in response.text


def test_apps_domain_rejects_lodge_hostname_parent_and_loopback(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text("{}")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(app_module.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")

    for domain in ("owner.woltspace.test", "woltspace.test", "localhost", "127.0.0.1"):
        response = asyncio.run(_request("POST", "/settings/apps-domain", json={
            "apps_domain": domain,
        }))
        assert response.status_code == 400

    assert json.loads((tmp_path / "woltspace.json").read_text()) == {}


def test_settings_accepts_any_registered_harness(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    default = asyncio.run(_request("POST", "/harness/default", json={"harness": "opencode"}))
    override = asyncio.run(_request("POST", "/wolts/maple/harness", json={"harness": "opencode"}))

    assert default.status_code == 200
    assert override.status_code == 200
    assert json.loads((tmp_path / "woltspace.json").read_text())["harness"]["default"] == "opencode"
    assert json.loads((tmp_path / "maple" / "wolt" / "wolt.json").read_text())["harness"] == "opencode"


def test_configured_wolts_skips_broken_entries(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    broken_dir = tmp_path / "splinter" / "wolt"
    broken_dir.mkdir(parents=True)
    (broken_dir / "wolt.json").write_text("not json")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    assert [wolt["name"] for wolt in app_module._configured_wolts()] == ["maple"]
