"""Lodge auth: keys for programs, a one-time pairing for browsers (opt-in)."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_auth
import server.app as server_app


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """Lodge auth on, against a throwaway data root; returns the keys."""
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    monkeypatch.setattr(server_app, "_pairing_failures", lodge_auth.FailureLimit())
    keys = lodge_auth.ensure_keys(tmp_path)
    return tmp_path, keys


def _client(**kw) -> TestClient:
    return TestClient(server_app.app, base_url="http://localhost:7777", **kw)


# --- The store -----------------------------------------------------------------

def test_keys_are_private_and_stable(tmp_path):
    first = lodge_auth.ensure_keys(tmp_path)
    again = lodge_auth.ensure_keys(tmp_path)
    directory = lodge_auth.auth_dir(tmp_path)

    assert first == again
    assert first["owner"] != first["wolt"] and len(first["owner"]) >= 32
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for scope in ("owner", "wolt"):
        assert stat.S_IMODE((directory / f"{scope}.key").stat().st_mode) == 0o600
        assert lodge_auth.key_scope(tmp_path, first[scope]) == scope
    assert lodge_auth.key_scope(tmp_path, "nope") is None
    assert lodge_auth.key_scope(tmp_path, "") is None


def test_a_pairing_code_works_once_and_expires(tmp_path):
    code = lodge_auth.new_pairing_code(tmp_path, now=1000)
    token = lodge_auth.redeem_pairing_code(tmp_path, code, now=1001)

    assert token and lodge_auth.browser_paired(tmp_path, token, now=1002)
    assert lodge_auth.redeem_pairing_code(tmp_path, code, now=1003) is None

    late = lodge_auth.new_pairing_code(tmp_path, now=2000)
    assert lodge_auth.redeem_pairing_code(tmp_path, late, now=2000 + lodge_auth.PAIRING_TTL + 1) is None

    assert not lodge_auth.browser_paired(tmp_path, token, now=1001 + lodge_auth.BROWSER_TTL + 1)


def test_only_hashes_of_codes_and_browser_tokens_are_stored(tmp_path):
    code = lodge_auth.new_pairing_code(tmp_path)
    stored = (lodge_auth.auth_dir(tmp_path) / "pairing.json").read_text()
    assert code not in stored
    token = lodge_auth.redeem_pairing_code(tmp_path, code)
    assert token not in (lodge_auth.auth_dir(tmp_path) / "browsers.json").read_text()


def test_forgetting_browsers_unpairs_them(tmp_path):
    token = lodge_auth.redeem_pairing_code(tmp_path, lodge_auth.new_pairing_code(tmp_path))
    lodge_auth.forget_browsers(tmp_path)
    assert not lodge_auth.browser_paired(tmp_path, token)


def test_clients_send_the_wolt_key_from_a_session_and_the_owner_key_otherwise(tmp_path):
    keys = lodge_auth.ensure_keys(tmp_path)
    root = {"WOLTSPACE_WOLTS_DIR": str(tmp_path)}

    assert lodge_auth.client_key(root) == keys["owner"]
    assert lodge_auth.client_key({**root, "WOLTSPACE_WOLT_SESSION": "softie-x-abc123"}) == keys["wolt"]
    assert lodge_auth.client_key({**root, "WOLTSPACE_LODGE_KEY": "explicit"}) == "explicit"
    assert lodge_auth.client_key({}) == ""
    assert lodge_auth.curl_config(root) == f'header = "X-Woltspace-Key: {keys["owner"]}"\n'
    assert lodge_auth.curl_config({}) == ""
    assert lodge_auth.client_headers(root) == {"X-Woltspace-Key": keys["owner"]}


def test_the_failure_limit_blocks_after_ten_misses_a_minute():
    limit = lodge_auth.FailureLimit()
    for i in range(10):
        assert not limit.blocked(now=i)
        limit.record(now=i)
    assert limit.blocked(now=10)
    assert not limit.blocked(now=75)


def test_app_processes_do_not_inherit_a_lodge_key(monkeypatch):
    import apps
    monkeypatch.setenv("WOLTSPACE_LODGE_KEY", "secret")
    assert "WOLTSPACE_LODGE_KEY" not in set(apps.app_environment())


# --- The gate --------------------------------------------------------------------

def test_off_by_default_nothing_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    assert _client().get("/version").status_code == 200
    assert _client().get("/pair?code=x").status_code == 404


def test_a_request_without_a_key_is_refused(lodge):
    response = _client().get("/version")
    assert response.status_code == 401
    assert response.json() == {"error": "lodge key or paired browser required"}


def test_an_unpaired_browser_gets_the_pairing_page(lodge):
    response = _client().get("/", headers={"accept": "text/html"})
    assert response.status_code == 401
    assert "woltspace pair" in response.text


def test_a_wrong_key_is_refused(lodge):
    assert _client().get("/version", headers={"x-woltspace-key": "wrong"}).status_code == 401
    assert _client().get("/version", headers={"authorization": "Bearer wrong"}).status_code == 401


def test_owner_and_wolt_keys_open_the_api(lodge):
    _, keys = lodge
    for key in keys.values():
        assert _client().get("/version", headers={"x-woltspace-key": key}).status_code == 200
        assert _client().get("/version", headers={"authorization": f"Bearer {key}"}).status_code == 200


@pytest.mark.parametrize("path", [
    "/auto-grants/grant", "/auto-grants/revoke", "/apps/blog/share",
    "/settings/session-expiry", "/harness/default", "/harness/tiers",
])
def test_a_wolt_key_cannot_do_owner_only_things(lodge, path):
    _, keys = lodge
    refused = _client().post(path, json={}, headers={"x-woltspace-key": keys["wolt"]})
    assert refused.status_code == 403
    assert refused.json() == {"error": "only the owner can do this"}

    owner = _client().post(path, json={}, headers={"x-woltspace-key": keys["owner"]})
    assert owner.status_code != 401
    assert owner.json() != {"error": "only the owner can do this"}


def test_health_and_static_assets_stay_open(lodge):
    assert _client().get("/health").status_code == 200
    assert _client().get("/static/does-not-exist.css").status_code == 404


def test_app_hosts_are_not_gated_here(lodge, monkeypatch):
    import apps
    monkeypatch.setattr(apps, "running_apps", lambda: [])
    response = _client().get("/", headers={"host": "notes.localhost:7777"})
    assert response.status_code == 503  # the app isn't running; the gate let it through


def test_pairing_turns_a_code_into_a_cookie_once(lodge):
    root, _ = lodge
    code = lodge_auth.new_pairing_code(root)
    client = _client()

    paired = client.get(f"/pair?code={code}", follow_redirects=False)
    assert paired.status_code == 303
    assert paired.headers["location"] == "/"
    assert paired.headers["referrer-policy"] == "no-referrer"
    cookie = paired.headers["set-cookie"]
    assert cookie.startswith(f"{lodge_auth.COOKIE_PLAIN}=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Secure" not in cookie

    assert client.get("/version").status_code == 200  # the cookie opens the lodge
    assert _client().get(f"/pair?code={code}").status_code == 403  # spent


def test_pairing_over_https_sets_a_secure_host_cookie(lodge):
    root, _ = lodge
    code = lodge_auth.new_pairing_code(root)
    paired = _client().get(f"/pair?code={code}", follow_redirects=False,
                           headers={"x-forwarded-proto": "https"})
    cookie = paired.headers["set-cookie"]
    assert cookie.startswith(f"{lodge_auth.COOKIE_SECURE}=")
    assert "Secure" in cookie and "Domain" not in cookie and "Path=/" in cookie


def test_guessing_pairing_codes_is_rate_limited(lodge):
    for _ in range(10):
        assert _client().get("/pair?code=guess").status_code == 403
    assert _client().get("/pair?code=guess").status_code == 429


def test_a_forged_or_expired_cookie_is_refused(lodge):
    client = _client()
    client.cookies.set(lodge_auth.COOKIE_PLAIN, "forged")
    assert client.get("/version").status_code == 401


def test_a_paired_browser_still_needs_a_same_origin_write(lodge):
    root, _ = lodge
    client = _client()
    client.get(f"/pair?code={lodge_auth.new_pairing_code(root)}")
    response = client.post("/current?session=main", json={"url": "/"},
                           headers={"origin": "https://evil.example"})
    assert response.status_code == 403


def test_websockets_need_a_key_or_paired_browser(lodge):
    with pytest.raises(WebSocketDisconnect) as refused:
        with _client().websocket_connect("/tui?session=main"):
            pass
    assert refused.value.code == 1008


# --- The CLI ---------------------------------------------------------------------

def test_pair_prints_a_link_and_refuses_inside_a_wolt_session(tmp_path, monkeypatch, capsys):
    from woltspace import cli
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    monkeypatch.delenv("WOLTSPACE_WOLT_SESSION", raising=False)
    monkeypatch.delenv("WOLT_SESSION", raising=False)
    monkeypatch.setattr("woltspace.lifecycle.read_tunnel_url", lambda layout: "https://lodge.example")

    assert cli.main(["pair", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    local, remote = out["links"]
    assert "/pair?code=" in local and remote.startswith("https://lodge.example/pair?code=")
    code = local.split("code=", 1)[1]
    assert lodge_auth.redeem_pairing_code(tmp_path, code)

    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "softie-x-abc123")
    assert cli.main(["pair", "--json"]) == 1
