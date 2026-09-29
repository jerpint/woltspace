"""Cloudflare Access token verification stays host-specific and offline-testable."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import app as server_app
from server.access import AccessSettings, AccessTokenVerifier
import server.access as access_module


SETTINGS = AccessSettings(
    team_domain="team.cloudflareaccess.com",
    lodge_aud="lodge-audience",
    apps_aud="apps-audience",
    owner_email="owner@example.com",
)


def _token(private_key, audience, email="owner@example.com", kid="key-1"):
    now = int(time.time())
    return jwt.encode({
        "iss": "https://team.cloudflareaccess.com",
        "aud": audience,
        "email": email,
        "iat": now,
        "exp": now + 300,
    }, private_key, algorithm="RS256", headers={"kid": kid})


async def _request(
    path="/sessions", *, host="owner.woltspace.test", token=None,
    client=("127.0.0.1", 123),
):
    headers = {"host": host}
    if token is not None:
        headers["cf-access-jwt-assertion"] = token
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server_app.app, client=client),
        base_url="https://owner.woltspace.test",
    ) as client:
        return await client.get(path, headers=headers)


def _configured(monkeypatch, public_key):
    verifier = AccessTokenVerifier()

    async def refresh(team_domain):
        assert team_domain == SETTINGS.team_domain
        verifier._team_domain = team_domain
        verifier._keys = {"key-1": public_key}
        verifier._expires_at = time.monotonic() + 3600
        return verifier._keys

    monkeypatch.setattr(verifier, "_refresh", refresh)
    monkeypatch.setattr(server_app, "access_token_verifier", verifier)
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: SETTINGS)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")


def test_unset_keeps_edge_trust_behavior(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: None)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    assert asyncio.run(_request()).status_code == 200


def test_lodge_requires_lodge_audience_and_owner_email(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _configured(monkeypatch, key.public_key())

    accepted = asyncio.run(_request(token=_token(key, SETTINGS.lodge_aud)))
    wrong_audience = asyncio.run(_request(token=_token(key, SETTINGS.apps_aud)))
    wrong_owner = asyncio.run(_request(token=_token(
        key, SETTINGS.lodge_aud, email="friend@example.com",
    )))

    assert accepted.status_code == 200
    assert wrong_audience.status_code == 403
    assert wrong_audience.json() == {"error": "invalid Access token"}
    assert wrong_owner.status_code == 403
    assert wrong_owner.json() == {"error": "owner identity required"}


def test_app_host_requires_apps_audience(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _configured(monkeypatch, key.public_key())

    accepted = asyncio.run(_request(
        path="/", host="notes.woltspace.test",
        token=_token(key, SETTINGS.apps_aud, email="friend@example.com"),
    ))
    lodge_token = asyncio.run(_request(
        path="/", host="notes.woltspace.test",
        token=_token(key, SETTINGS.lodge_aud),
    ))

    assert accepted.status_code == 404
    assert "Apps are served on the app domain" in accepted.text
    assert lodge_token.status_code == 403


def test_missing_malformed_and_email_less_tokens_fail_closed(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _configured(monkeypatch, key.public_key())
    now = int(time.time())
    no_email = jwt.encode({
        "iss": "https://team.cloudflareaccess.com", "aud": SETTINGS.lodge_aud,
        "iat": now, "exp": now + 300,
    }, key, algorithm="RS256", headers={"kid": "key-1"})

    assert asyncio.run(_request()).json() == {"error": "Access token required"}
    assert asyncio.run(_request(token="broken")).json() == {"error": "invalid Access token"}
    assert asyncio.run(_request(token=no_email)).json() == {"error": "Access token has no email"}


def test_loopback_host_and_peer_are_exempt_when_verification_is_configured(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: SETTINGS)
    response = asyncio.run(_request(host="localhost:7777"))
    assert response.status_code == 200


def test_loopback_host_from_lan_peer_requires_access(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: SETTINGS)
    response = asyncio.run(_request(
        host="localhost:7777", client=("192.168.1.50", 123),
    ))
    assert response.status_code == 403
    assert response.json() == {"error": "Access token required"}


def test_public_host_from_loopback_peer_requires_access(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: SETTINGS)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    response = asyncio.run(_request(host="owner.woltspace.test"))
    assert response.status_code == 403
    assert response.json() == {"error": "Access token required"}


def test_unrecognized_host_fails_closed_for_http_and_websocket(monkeypatch):
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: None)

    response = asyncio.run(_request(host="attacker.example"))
    assert response.status_code == 403
    assert response.json() == {"error": "untrusted request host"}

    for path in (
        "/tui?session=main",
        "/livereload",
        "/wolt/n00b/site/livereload",
    ):
        with pytest.raises(WebSocketDisconnect) as exc:
            with TestClient(server_app.app).websocket_connect(
                path,
                headers={
                    "host": "attacker.example",
                    "origin": "https://attacker.example",
                },
            ):
                pass
        assert exc.value.code == 1008


def test_malformed_present_configuration_fails_closed(monkeypatch):
    def broken(_root):
        raise RuntimeError("invalid")

    monkeypatch.setattr(server_app, "load_access_settings", broken)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    response = asyncio.run(_request())
    assert response.status_code == 403
    assert response.json() == {"error": "Access verification is misconfigured"}


def test_unreadable_config_names_the_remote_fix_but_localhost_still_works(monkeypatch):
    def broken(_root):
        raise RuntimeError("woltspace.json unreadable")

    monkeypatch.setattr(server_app, "load_access_settings", broken)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    remote = asyncio.run(_request())
    local = asyncio.run(_request(host="localhost:7777"))

    assert remote.status_code == 403
    assert remote.json() == {"error": (
        "woltspace.json is unreadable - fix it or remove the access block; "
        "localhost still works"
    )}
    assert local.status_code == 200


def test_same_kid_key_rotation_refreshes_and_retries(monkeypatch):
    old_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    new_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = AccessTokenVerifier()
    verifier._team_domain = SETTINGS.team_domain
    verifier._keys = {"key-1": old_key.public_key()}
    verifier._expires_at = time.monotonic() + 3600
    refreshes = []

    async def refresh(team_domain):
        refreshes.append(team_domain)
        verifier._keys = {"key-1": new_key.public_key()}
        verifier._expires_at = time.monotonic() + 3600
        return verifier._keys

    monkeypatch.setattr(verifier, "_refresh", refresh)
    claims = asyncio.run(verifier.verify(
        _token(new_key, SETTINGS.lodge_aud), SETTINGS, SETTINGS.lodge_aud,
    ))

    assert claims["email"] == SETTINGS.owner_email
    assert refreshes == [SETTINGS.team_domain]


def test_forced_key_refresh_is_rate_limited(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = AccessTokenVerifier(forced_refresh_interval=60)
    verifier._team_domain = SETTINGS.team_domain
    verifier._keys = {"known": key.public_key()}
    verifier._expires_at = time.monotonic() + 3600
    refreshes = []

    async def refresh(team_domain):
        refreshes.append(team_domain)
        return verifier._keys

    monkeypatch.setattr(verifier, "_refresh", refresh)
    bogus = _token(key, SETTINGS.lodge_aud, kid="unknown")
    for _ in range(2):
        with pytest.raises(jwt.InvalidTokenError):
            asyncio.run(verifier.verify(bogus, SETTINGS, SETTINGS.lodge_aud))

    assert refreshes == [SETTINGS.team_domain]


@pytest.mark.parametrize("path,host", [
    ("/tui?session=main", "owner.woltspace.test"),
    ("/livereload", "owner.woltspace.test"),
    ("/wolt/n00b/site/livereload", "owner.woltspace.test"),
    ("/vite-hmr", "notes.woltspace.test"),
])
def test_remote_websockets_require_access_token(path, host, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _configured(monkeypatch, key.public_key())

    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(server_app.app).websocket_connect(
            path, headers={"host": host, "origin": f"https://{host}"},
        ):
            pass

    assert exc.value.code == 1008


def test_valid_owner_token_allows_lodge_terminal_websocket(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    _configured(monkeypatch, key.public_key())
    attached = []

    class Attachment:
        async def read(self): return b""
        async def write(self, _data): pass
        async def close(self): pass
        def resize(self, _cols, _rows): pass

    async def attach(session, _root):
        attached.append(session)
        return Attachment()

    monkeypatch.setattr(server_app, "attach_tmux", attach)
    with TestClient(server_app.app).websocket_connect(
        "/tui?session=main",
        headers={
            "host": "owner.woltspace.test",
            "origin": "https://owner.woltspace.test",
            "cf-access-jwt-assertion": _token(key, SETTINGS.lodge_aud),
        },
    ):
        pass

    assert attached == ["main"]


def test_access_settings_route_validates_and_writes_atomically(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({"harness": {"default": "codex"}}))
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)

    async def post(payload):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server_app.app),
            base_url="http://localhost:7777",
        ) as client:
            return await client.post("/settings/access", json=payload)

    saved = asyncio.run(post({"access": {
        "team_domain": "TEAM.cloudflareaccess.com.",
        "lodge_aud": "lodge-audience",
        "apps_aud": "apps-audience",
        "owner_email": "Owner@Example.com",
    }}))
    invalid = asyncio.run(post({"access": {
        "team_domain": "https://team.cloudflareaccess.com",
        "lodge_aud": "same", "apps_aud": "same", "owner_email": "bad",
    }}))

    assert saved.status_code == 200
    assert saved.json()["access"]["owner_email"] == "owner@example.com"
    assert invalid.status_code == 400
    assert not list(tmp_path.glob("*.tmp"))
    assert json.loads((tmp_path / "woltspace.json").read_text())["harness"] == {"default": "codex"}


def test_access_settings_cache_invalidates_on_file_mtime(tmp_path, monkeypatch):
    path = tmp_path / "woltspace.json"
    path.write_text("{}")
    access_module._CONFIG_CACHE.clear()
    original = access_module._read_root
    reads = []

    def counted(root):
        reads.append(root)
        return original(root)

    monkeypatch.setattr(access_module, "_read_root", counted)
    assert access_module.load_access_settings(tmp_path) is None
    assert access_module.load_access_settings(tmp_path) is None
    path.write_text(json.dumps({"access": {
        "team_domain": SETTINGS.team_domain,
        "lodge_aud": SETTINGS.lodge_aud,
        "apps_aud": SETTINGS.apps_aud,
        "owner_email": SETTINGS.owner_email,
    }}))
    assert access_module.load_access_settings(tmp_path) == SETTINGS
    assert len(reads) == 2
