"""Per-app sharing is lodge-owned, exact, and API mediated."""

import argparse
import asyncio
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps
from server import app as server_app
from server.app_sharing import email_is_shared, read_app_shares, write_app_shares


def _manifest(root: Path):
    target = root / "apps" / "notes"
    target.mkdir(parents=True)
    (target / "woltspace.json").write_text(json.dumps({
        "name": "notes", "keeper": "n00b", "port": 4321,
    }))


async def _request(method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app),
        base_url="http://localhost:7777",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_share_entries_are_normalized_exact_and_atomic(tmp_path):
    entries = write_app_shares(tmp_path, "notes", [
        "Friend@Example.com", "@Team.Example", "friend@example.com",
    ])

    assert entries == ["@team.example", "friend@example.com"]
    assert read_app_shares(tmp_path, "notes") == entries
    assert email_is_shared("FRIEND@example.com", entries)
    assert email_is_shared("anyone@team.example", entries)
    assert not email_is_shared("friend@other.example", entries)
    assert not email_is_shared("anyone@sub.team.example", entries)
    assert not list((tmp_path / ".space" / "platform").glob("*.tmp"))


def test_invalid_share_entries_do_not_change_state(tmp_path):
    write_app_shares(tmp_path, "notes", ["friend@example.com"])
    for invalid in ("friend", "@localhost", "@bad..example", "*"):
        try:
            write_app_shares(tmp_path, "notes", [invalid])
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid entry {invalid}")
    assert read_app_shares(tmp_path, "notes") == ["friend@example.com"]


def test_sharing_api_defaults_owner_only_and_clears(tmp_path, monkeypatch):
    _manifest(tmp_path)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "get_app", lambda name: object() if name == "notes" else None)
    monkeypatch.setattr(server_app, "_app_access_settings", lambda: SimpleNamespace(
        owner_email="owner@example.com",
    ))

    initial = asyncio.run(_request("GET", "/apps/notes/sharing"))
    saved = asyncio.run(_request("PUT", "/apps/notes/sharing", json={
        "entries": ["friend@example.com", "@team.example"],
    }))
    invalid = asyncio.run(_request("PUT", "/apps/notes/sharing", json={"entries": ["*"]}))
    cleared = asyncio.run(_request("PUT", "/apps/notes/sharing", json={"entries": []}))

    assert initial.json() == {"entries": [], "enabled": True}
    assert saved.json()["entries"] == ["@team.example", "friend@example.com"]
    assert invalid.status_code == 400
    assert cleared.json() == {"ok": True, "entries": [], "enabled": True}
    assert read_app_shares(tmp_path, "notes") == []


def test_app_detail_exposes_disabled_hint_and_single_domain_hint(tmp_path, monkeypatch):
    manifest = apps.WoltspaceApp(name="notes", keeper="n00b", port=4321)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "get_app", lambda _name: manifest)
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    monkeypatch.setattr(server_app, "_app_access_settings", lambda: None)

    payload = asyncio.run(_request("GET", "/apps/notes")).json()

    assert payload["share_controls_enabled"] is False
    assert payload["share_entries"] == []
    assert payload["separate_app_domain"] is False


def test_legacy_remote_app_host_redirects_to_app_domain(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "get_apps_domain", lambda: "owner.woltspace.app")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    response = asyncio.run(_request(
        "GET", "/hello?safe=1", headers={"host": "notes.woltspace.test"},
    ))

    assert response.status_code == 302
    assert response.headers["location"] == "https://notes.owner.woltspace.app/hello?safe=1"


def test_legacy_remote_app_host_without_app_domain_has_clear_page(monkeypatch):
    monkeypatch.setattr(server_app, "get_apps_domain", lambda: None)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    response = asyncio.run(_request("GET", "/", headers={"host": "notes.woltspace.test"}))

    assert response.status_code == 404
    assert "Apps are served on the app domain" in response.text


def test_app_websocket_without_verified_shared_identity_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "_app_access_settings", lambda: SimpleNamespace(
        owner_email="owner@example.com",
    ))
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(server_app.app).websocket_connect(
            "/vite-hmr",
            headers={
                "host": "notes.woltspace.test",
                "origin": "https://notes.woltspace.test",
            },
        ):
            pass

    assert exc.value.code == 1008


def test_lodge_app_host_http_redirects_but_websocket_never_proxies(monkeypatch):
    monkeypatch.setattr(server_app, "get_app_gateway_port", lambda: 7117)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    local = asyncio.run(_request("GET", "/path", headers={"host": "notes.localhost:7777"}))
    assert local.status_code == 302
    assert local.headers["location"] == "http://notes.localhost:7117/path"
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(server_app.app).websocket_connect(
            "/vite-hmr", headers={
                "host": "notes.localhost:7777", "origin": "http://notes.localhost:7777",
            },
        ):
            pass
    assert exc.value.code == 1008


def test_app_sharing_cli_reads_then_writes_only_through_api(capsys):
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return 200, {"entries": ["old@example.com"], "enabled": True}
        return 200, {"entries": body["entries"], "enabled": True}

    client["cmd_app_sharing"].__globals__["_req"] = request
    client["cmd_app_sharing"](argparse.Namespace(
        name="notes", verb="share", entries=["new@example.com"], json=False,
    ))

    assert calls == [
        ("GET", "/apps/notes/sharing", None),
        ("PUT", "/apps/notes/sharing", {"entries": ["old@example.com", "new@example.com"]}),
    ]
    assert "new@example.com" in capsys.readouterr().out


def test_app_page_has_editable_sharing_and_clear_consequences_copy():
    source = (ROOT / "public" / "static" / "app-page.js").read_text()
    assert "Sharing needs Access token verification." in source
    assert "Removing an entry immediately removes that access." in source
    assert "For stronger isolation, set an app domain." in source
    assert "/sharing" in source
