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


def test_settings_page_renders_tier_defaults(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps(
        {"harness": {"default": "claude", "tiers": {"otter": "codex"}}}))
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("GET", "/settings"))
    body = response.text

    assert response.status_code == 200
    assert "Tier defaults" in body
    assert "raccoon · thinker" in body
    assert "beaver · builder" in body
    assert "otter · quick" in body
    # pinned tier shows its engine + that engine's tier model
    assert "Pinned · codex · gpt-5.6-luna" in body
    # unpinned tiers follow the lodge default
    assert "Follows lodge · claude · opus" in body
    # catalogs embedded for the cascading selects
    assert "data-harness-data" in body


def test_tier_endpoint_sets_engine_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

    pin = asyncio.run(_request("POST", "/harness/tiers",
                               json={"tier": "otter", "harness": "codex"}))
    assert pin.json() == {"ok": True, "tier": "otter", "pinned": True,
                          "harness": "codex", "model": "gpt-5.6-luna"}

    model = asyncio.run(_request("POST", "/harness/tiers",
                                 json={"tier": "otter", "model": "gpt-5.5"}))
    assert model.json()["model"] == "gpt-5.5"

    cfg = json.loads((tmp_path / "woltspace.json").read_text())
    assert cfg["harness"]["tiers"]["otter"] == "codex"
    assert cfg["harness"]["models"]["codex"]["tiers"]["otter"] == "gpt-5.5"

    clear = asyncio.run(_request("POST", "/harness/tiers",
                                 json={"tier": "otter", "harness": None}))
    assert clear.json()["pinned"] is False
    assert clear.json()["harness"] == "claude"  # back to lodge default


def test_tier_endpoint_rejects_bad_input(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

    bad_tier = asyncio.run(_request("POST", "/harness/tiers",
                                    json={"tier": "wolf", "harness": "claude"}))
    bad_harness = asyncio.run(_request("POST", "/harness/tiers",
                                       json={"tier": "otter", "harness": "winamp"}))
    bad_model = asyncio.run(_request("POST", "/harness/tiers",
                                     json={"tier": "otter", "model": "gpt-5.5"}))  # not a claude model
    list_harness = asyncio.run(_request("POST", "/harness/tiers",
                                        json={"tier": "otter", "harness": ["claude"]}))
    list_model = asyncio.run(_request("POST", "/harness/tiers",
                                      json={"tier": "otter", "harness": "opencode",
                                            "model": ["ollama/qwen3"]}))
    assert bad_tier.status_code == 400
    assert bad_harness.status_code == 400
    assert bad_model.status_code == 400
    assert list_harness.status_code == 400
    assert list_model.status_code == 400


def test_tier_endpoint_rejects_combined_body_atomically(tmp_path, monkeypatch):
    """A bad model must not leave a half-applied engine change behind."""
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("POST", "/harness/tiers",
                                    json={"tier": "otter", "harness": "codex", "model": "opus"}))
    assert response.status_code == 400
    assert not (tmp_path / "woltspace.json").exists()  # nothing was written


def test_configured_wolts_skips_broken_entries(tmp_path, monkeypatch):
    _write_wolt(tmp_path, "maple", "raccoon")
    broken_dir = tmp_path / "splinter" / "wolt"
    broken_dir.mkdir(parents=True)
    (broken_dir / "wolt.json").write_text("not json")
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)

    assert [wolt["name"] for wolt in app_module._configured_wolts()] == ["maple"]
