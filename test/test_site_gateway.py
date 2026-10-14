"""Wolt sites on the app gateway: one host per site, static files, owner livereload."""

import argparse
import asyncio
import json
import os
import re
import runpy
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.content_origins as content_origins
import server.gateway as gateway
import server.site_livereload as livereload
import server.site_server as site_server
import site_shell
from server.access import AccessTokenVerifier
from server.content_hosts import (
    ContentHost, UnsupportedWoltName, anchored_site_root, parse_content_host, site_host_label,
)
from server import app as lodge

APPS_DOMAIN = "owner.woltspace.app"
OWNER = "owner@example.com"
FRIEND = "friend@example.com"
STRANGER = "stranger@example.com"
LOCAL_HOST = "astre.sites.localhost:7117"
REMOTE_HOST = f"site-astre.{APPS_DOMAIN}"


# ---------------------------------------------------------------------------
# Fixture: two wolts with sites, one app, Access configured
# ---------------------------------------------------------------------------

def _key_and_config(root: Path, apps_aud="apps-aud"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (root / "woltspace.json").write_text(json.dumps({
        "apps_domain": APPS_DOMAIN,
        "access": {
            "team_domain": "team.cloudflareaccess.com",
            "lodge_aud": "lodge-aud",
            "apps_aud": apps_aud,
            "owner_email": OWNER,
        },
    }))
    verifier = AccessTokenVerifier()

    async def refresh(_team):
        verifier._team_domain = "team.cloudflareaccess.com"
        verifier._keys = {"key": key.public_key()}
        verifier._expires_at = time.monotonic() + 3600
        return verifier._keys

    verifier._refresh = refresh
    return key, verifier


def _token(key, aud, email=OWNER, ttl=300):
    now = int(time.time())
    return jwt.encode({
        "iss": "https://team.cloudflareaccess.com", "aud": aud,
        "email": email, "iat": now, "exp": now + ttl,
    }, key, algorithm="RS256", headers={"kid": "key"})


def _make_wolt(root: Path, name: str, *, pages: dict[str, str] | None = None):
    wolt = root / name / "wolt"
    (wolt / "memory").mkdir(parents=True)
    (wolt / "memory" / "identity.md").write_text(f"# {name}\nPRIVATE MEMORY OF {name}\n")
    (wolt / "wolt.json").write_text(json.dumps({
        "name": name, "type": "raccoon", "role": "tester", "harness": "claude",
        "model": "secret-model", "capabilities": ["x"], "api_key": "SECRET_KEY",
    }))
    site = wolt / "site"
    site.mkdir()
    pages = pages or {"index.html": f"<html><body><h1>{name} home</h1></body></html>"}
    for rel, content in pages.items():
        target = site / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root / name


def _colony(tmp_path, monkeypatch, *, apps_aud="apps-aud"):
    key, verifier = _key_and_config(tmp_path, apps_aud)
    monkeypatch.setattr(gateway, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(gateway, "access_token_verifier", verifier)
    content_origins._SEEN.clear()
    monkeypatch.delenv("WOLTSPACE_ISOLATION", raising=False)
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)
    monkeypatch.setenv("WOLTSPACE_PORT", "7777")
    _make_wolt(tmp_path, "astre", pages={
        "index.html": "<html><head><title>Astre</title></head><body><h1>astre home</h1></body></html>",
        "data.json": json.dumps({"hello": "astre"}),
        "nested/page.html": "<html><body>nested page</body></html>",
        "nested/index.html": "<html><body>nested index</body></html>",
        "notes.txt": "0123456789",
    })
    _make_wolt(tmp_path, "pine", pages={"index.html": "<html><body><h1>pine home</h1></body></html>"})
    app_dir = tmp_path / "apps" / "notes"
    app_dir.mkdir(parents=True)
    (app_dir / "woltspace.json").write_text(json.dumps({
        "name": "notes", "keeper": "astre", "port": 4321,
    }))
    return key


def _enable_remote(monkeypatch):
    """Exercise the remote path the gate hides today (see gateway docstring)."""
    monkeypatch.setattr(gateway, "REMOTE_SITES_ENABLED", True)


def _origins(root: Path) -> dict:
    path = root / ".space" / "platform" / "content-origins.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _site_shares(root: Path, data):
    platform = root / ".space" / "platform"
    platform.mkdir(parents=True, exist_ok=True)
    text = data if isinstance(data, str) else json.dumps(data)
    (platform / "site-sharing.json").write_text(text)


async def _request(method, path, *, host, token=None, client=("127.0.0.1", 123), headers=None):
    sent = {"host": host}
    if token:
        sent["cf-access-jwt-assertion"] = token
    sent.update(headers or {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app, client=client),
        base_url="https://gateway.test",
    ) as http:
        return await http.request(method, path, headers=sent)


def _get(path, **kwargs):
    return asyncio.run(_request("GET", path, **kwargs))


async def _raw_request(method, raw_path: bytes, *, host, headers=(), client=("127.0.0.1", 123), query=b""):
    """Drive the gateway with an exact request target (httpx would normalize it)."""
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": unquote(raw_path.decode("latin-1")),
        "raw_path": raw_path, "query_string": query, "root_path": "",
        "headers": [(b"host", host.encode())] + [(k.encode(), v.encode()) for k, v in headers],
        "client": client, "server": ("127.0.0.1", 7117),
    }
    messages = []
    delivered = False

    async def receive():
        # One empty body, then wait like a real connection would (a streamed
        # response listens for a disconnect while it sends).
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(message):
        messages.append(message)

    await gateway.app(scope, receive, send)
    start = messages[0]
    body = b"".join(m.get("body", b"") for m in messages[1:])
    return start["status"], {k.decode(): v.decode() for k, v in start["headers"]}, body


def _raw_get(raw_path: bytes, **kwargs):
    return asyncio.run(_raw_request("GET", raw_path, **kwargs))


def _manifest(html: str) -> dict:
    match = re.search(r"window\.__WOLT_SHELL__=(\{.*?\});</script>", html, re.S)
    assert match, "no inline manifest"
    return json.loads(match.group(1))


def _assert_site_headers(response):
    headers = response.headers if hasattr(response, "headers") else response
    assert headers["content-security-policy"] == "sandbox allow-scripts allow-same-origin"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "private, no-store"


# ---------------------------------------------------------------------------
# Host parser
# ---------------------------------------------------------------------------

LONG = "a" * 58


@pytest.mark.parametrize(("host", "domain", "expected"), [
    ("astre.sites.localhost:7117", None, ContentHost("site", "astre", True)),
    ("ASTRE.Sites.Localhost.:7117", None, ContentHost("site", "astre", True)),
    ("notes.localhost:7117", None, ContentHost("app", "notes", True)),
    ("Notes.LOCALHOST.", None, ContentHost("app", "notes", True)),
    ("site-astre.localhost:7117", None, ContentHost("reserved", "site-astre", True)),
    ("site-astre.owner.woltspace.app", APPS_DOMAIN, ContentHost("site", "astre", False)),
    ("Site-Astre.Owner.Woltspace.App.:443", APPS_DOMAIN, ContentHost("site", "astre", False)),
    ("notes.owner.woltspace.app", APPS_DOMAIN, ContentHost("app", "notes", False)),
    (f"site-{LONG}.owner.woltspace.app", APPS_DOMAIN, ContentHost("site", LONG, False)),
    (f"site-{LONG}a.owner.woltspace.app", APPS_DOMAIN, None),
    (f"{LONG}.sites.localhost", None, ContentHost("site", LONG, True)),
    (f"{LONG}a.sites.localhost", None, None),
    ("site-my_wolt.owner.woltspace.app", APPS_DOMAIN, None),
    ("site--bad.owner.woltspace.app", APPS_DOMAIN, None),
    ("site-bad-.owner.woltspace.app", APPS_DOMAIN, None),
    ("site-.owner.woltspace.app", APPS_DOMAIN, None),
    ("my_wolt.sites.localhost", None, None),
    ("a.b.sites.localhost", None, None),
    ("site-astre.other.example", APPS_DOMAIN, None),
    ("site-astre.owner.woltspace.app", None, None),
    ("owner.woltspace.app", APPS_DOMAIN, None),
    ("localhost:7117", None, None),
    ("sites.localhost", None, ContentHost("app", "sites", True)),
    ("", None, None),
    ("[::1]:7117", None, None),
])
def test_parse_content_host_table(host, domain, expected):
    assert parse_content_host(host, domain) == expected


def test_site_host_label_normalizes_case_and_refuses_non_dns_names():
    assert site_host_label("Astre") == "astre"
    assert site_host_label("n00b") == "n00b"
    with pytest.raises(UnsupportedWoltName, match="not a DNS label"):
        site_host_label("my_wolt")
    with pytest.raises(UnsupportedWoltName, match="at most 58"):
        site_host_label("a" * 59)
    with pytest.raises(UnsupportedWoltName):
        site_host_label("-dash")


def test_site_root_is_anchored_to_the_wolt(tmp_path):
    good = _make_wolt(tmp_path, "good")
    assert anchored_site_root(good) == (good / "wolt" / "site").resolve()

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "index.html").write_text("leak")
    linky = _make_wolt(tmp_path, "linky")
    shutil.rmtree(linky / "wolt" / "site")
    (linky / "wolt" / "site").symlink_to(outside, target_is_directory=True)
    assert anchored_site_root(linky) is None

    selfish = _make_wolt(tmp_path, "selfish")
    shutil.rmtree(selfish / "wolt" / "site")
    (selfish / "wolt" / "site").symlink_to(selfish / "wolt", target_is_directory=True)
    assert anchored_site_root(selfish) is None

    # Inside the wolt is not enough either: the root is the real directory
    # at wolt/site, or nothing.
    inner = _make_wolt(tmp_path, "inner")
    shutil.rmtree(inner / "wolt" / "site")
    (inner / "wolt" / "pages").mkdir()
    (inner / "wolt" / "site").symlink_to(inner / "wolt" / "pages", target_is_directory=True)
    assert anchored_site_root(inner) is None

    leaky = _make_wolt(tmp_path, "leaky")
    shutil.rmtree(leaky / "wolt" / "site")
    (leaky / "wolt" / "site").symlink_to("memory", target_is_directory=True)
    assert anchored_site_root(leaky) is None

    plain = _make_wolt(tmp_path, "plain")
    (plain / "wolt" / "site" / "index.html").unlink()
    (plain / "wolt" / "site").rmdir()
    (plain / "wolt" / "site").write_text("not a directory")
    assert anchored_site_root(plain) is None


# ---------------------------------------------------------------------------
# Two sites and an app side by side
# ---------------------------------------------------------------------------

def test_two_sites_and_an_app_each_answer_on_their_own_host(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)

    async def proxy(_request, name):
        return PlainTextResponse(f"{name}:APP_OK")

    monkeypatch.setattr(gateway, "proxy_app_http", proxy)
    astre = _get("/", host=LOCAL_HOST)
    pine = _get("/", host="pine.sites.localhost:7117")
    notes = _get("/", host="notes.localhost:7117")
    missing = _get("/", host="nobody.sites.localhost:7117")

    assert astre.status_code == 200 and "astre home" in astre.text
    assert pine.status_code == 200 and "pine home" in pine.text
    assert "astre" not in pine.text
    assert notes.text == "notes:APP_OK"
    assert missing.status_code == 404
    for response in (astre, pine, missing):
        _assert_site_headers(response)
    assert astre.headers["content-type"] == "text/html; charset=utf-8"


def test_site_serves_json_and_text_with_correct_mime(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    data = _get("/data.json", host=LOCAL_HOST)
    text = _get("/notes.txt", host=LOCAL_HOST)
    assert data.status_code == 200 and data.json() == {"hello": "astre"}
    assert data.headers["content-type"] == "application/json; charset=utf-8"
    assert text.headers["content-type"] == "text/plain; charset=utf-8"
    _assert_site_headers(data)
    _assert_site_headers(text)


def test_directory_redirects_then_serves_index(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    status, headers, _body = _raw_get(b"/nested", host=LOCAL_HOST, query=b"a=1")
    assert status == 308 and headers["location"] == "/nested/?a=1"
    _assert_site_headers(headers)
    index = _get("/nested/", host=LOCAL_HOST)
    assert index.status_code == 200 and "nested index" in index.text
    page = _get("/nested/page.html", host=LOCAL_HOST)
    assert "nested page" in page.text
    # A protocol-relative target can never become a redirect off this host.
    status, headers, _body = _raw_get(b"//evil.example/", host=LOCAL_HOST)
    assert status == 404 and "location" not in headers


def test_head_range_and_conditional_requests(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    head = asyncio.run(_request("HEAD", "/notes.txt", host=LOCAL_HOST))
    assert head.status_code == 200 and head.content == b"" and head.headers["content-length"] == "10"
    partial = _get("/notes.txt", host=LOCAL_HOST, headers={"range": "bytes=2-5"})
    assert partial.status_code == 206 and partial.content == b"2345"
    assert partial.headers["content-range"] == "bytes 2-5/10"
    tail = _get("/notes.txt", host=LOCAL_HOST, headers={"range": "bytes=-3"})
    assert tail.status_code == 206 and tail.content == b"789"
    bad = _get("/notes.txt", host=LOCAL_HOST, headers={"range": "bytes=50-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == "bytes */10"
    etag = partial.headers["etag"]
    cached = _get("/notes.txt", host=LOCAL_HOST, headers={"if-none-match": etag})
    assert cached.status_code == 304
    for response in (head, partial, bad, cached):
        _assert_site_headers(response)


# ---------------------------------------------------------------------------
# Identity and ACL
# ---------------------------------------------------------------------------

def test_local_owner_needs_a_loopback_peer_and_no_forwarding(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    assert _get("/", host=LOCAL_HOST).status_code == 200
    assert _get("/", host=LOCAL_HOST, client=("192.0.2.10", 123)).status_code == 403
    assert _get("/", host=LOCAL_HOST, headers={"x-forwarded-for": "203.0.113.4"}).status_code == 403
    # Container external mode trusts the launcher's loopback-only port publishing.
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    assert _get("/", host=LOCAL_HOST, client=("192.168.65.1", 123)).status_code == 200


def test_remote_site_hosts_are_gated_for_every_request(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _site_shares(tmp_path, {"astre": [FRIEND]})
    assert gateway.REMOTE_SITES_ENABLED is False
    token = _token(key, "apps-aud")
    # The local site host is served as built; the gate is remote-only.
    assert _get("/", host=LOCAL_HOST).status_code == 200

    def never(*_args, **_kwargs):
        raise AssertionError("identity or serving ran behind the remote gate")

    # Nothing past the gate runs: no identity check, no file, no socket.
    monkeypatch.setattr(gateway, "request_identity", never)
    monkeypatch.setattr(gateway, "serve_site", never)
    monkeypatch.setattr(gateway, "serve_site_livereload", never)
    for method, path in (("GET", "/"), ("HEAD", "/"), ("GET", "/data.json"), ("POST", "/"),
                         ("GET", "/_woltspace/assets/shell.js"), ("GET", "/_woltspace/livereload")):
        response = asyncio.run(_request(method, path, host=REMOTE_HOST, token=token))
        assert response.status_code == 503, (method, path)
        assert method == "HEAD" or "not served yet" in response.text
        _assert_site_headers(response)
    assert asyncio.run(_request("GET", "/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND))).status_code == 503
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client().websocket_connect(
            site_server.LIVERELOAD_PATH, headers=_ws_headers(REMOTE_HOST, f"https://{REMOTE_HOST}", token),
        ):
            pass
    assert exc.value.code == 1008
    assert _origins(tmp_path).get(REMOTE_HOST) is None


def test_remote_identity_matrix(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _site_shares(tmp_path, {"astre": [FRIEND]})
    owner = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud"))
    friend = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND))
    stranger = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", STRANGER))
    lodge_token = _get("/", host=REMOTE_HOST, token=_token(key, "lodge-aud"))
    anonymous = _get("/", host=REMOTE_HOST)
    garbage = _get("/", host=REMOTE_HOST, token="garbage")
    # Another site's share list says nothing about this one.
    _site_shares(tmp_path, {"pine": [FRIEND]})
    other_list = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND))

    assert owner.status_code == 200 and "astre home" in owner.text
    assert friend.status_code == 200 and "astre home" in friend.text
    assert stranger.status_code == 403
    assert lodge_token.status_code == 403
    assert anonymous.status_code == 403
    assert garbage.status_code == 403
    assert other_list.status_code == 403


def test_empty_apps_audience_refuses_every_remote_visitor(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch, apps_aud="")
    _enable_remote(monkeypatch)
    _site_shares(tmp_path, {"astre": [FRIEND]})
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud")).status_code == 403
    assert _get("/", host=REMOTE_HOST, token=_token(key, "lodge-aud")).status_code == 403
    # The owner on loopback is unaffected.
    assert _get("/", host=LOCAL_HOST).status_code == 200


def test_malformed_share_state_fails_closed_for_readers_not_the_owner(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    monkeypatch.setattr(lodge, "WOLTS_DIR", tmp_path)
    for broken in ("{not json", json.dumps(["list"]), json.dumps({"astre": "friend@example.com"})):
        _site_shares(tmp_path, broken)
        friend = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND))
        assert friend.status_code == 403
        # The share list adds readers; the owner's authority never came from it.
        owner = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud"))
        assert owner.status_code == 200 and "astre home" in owner.text
        assert _get("/", host=LOCAL_HOST).status_code == 200
        # The management route says the state is corrupt instead of hiding it.
        state = asyncio.run(_lodge("GET", "/sites/astre/sharing"))
        assert state.status_code == 500 and "unreadable" in state.json()["error"]
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": APPS_DOMAIN, "access": {"broken": True},
    }))
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud")).status_code == 403


def test_acl_is_applied_before_any_file_is_touched(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    stranger = _token(key, "apps-aud", STRANGER)

    def never(*_args, **_kwargs):
        raise AssertionError("a site file was opened for a refused visitor")

    # Neither the file server nor the root lookup runs for a refused visitor.
    monkeypatch.setattr(gateway, "serve_site", never)
    monkeypatch.setattr(gateway, "anchored_site_root", never)
    monkeypatch.setattr(site_server, "_open_regular", never)
    cases = [
        ("GET", "/", {}),
        ("HEAD", "/", {}),
        ("GET", "/notes.txt", {"range": "bytes=0-3"}),
        ("GET", "/notes.txt", {"if-none-match": 'W/"x"'}),
        ("GET", "/nested", {}),
        ("GET", "/_woltspace/assets/shell.js", {}),
    ]
    for method, path, headers in cases:
        response = asyncio.run(_request(method, path, host=REMOTE_HOST, token=stranger, headers=headers))
        assert response.status_code == 403, (method, path)
    anonymous = asyncio.run(_request("GET", "/nested", host=REMOTE_HOST))
    assert anonymous.status_code == 403


# ---------------------------------------------------------------------------
# Path containment
# ---------------------------------------------------------------------------

def test_traversal_symlink_and_special_files_are_not_found(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    site = tmp_path / "astre" / "wolt" / "site"
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET")
    (site / "escape-link").symlink_to(secret)
    (site / "escape-dir").symlink_to(tmp_path / "astre" / "wolt" / "memory", target_is_directory=True)
    (site / "inside-link").symlink_to(site / "notes.txt")
    os.mkfifo(site / "fifo")
    refused = [
        b"/../../astre/wolt/memory/identity.md",
        b"/..%2F..%2Fastre/wolt/memory/identity.md",
        b"/%2e%2e/%2e%2e/astre/wolt/memory/identity.md",
        b"/nested/..%2f..%2f..%2fsecret.txt",
        b"/nested%2F..%2F..%2Fsecret.txt",
        b"/nested%2Fpage.html",
        b"/nested%2fpage.html",
        b"/nested%5Cpage.html",
        b"/escape-link",
        b"/escape-dir/identity.md",
        b"/fifo",
        b"/notes.txt%00.html",
        b"/%ff",
        b"/nested//index.html",
        b"/./index.html",
    ]
    for raw in refused:
        status, _headers, body = _raw_get(raw, host=LOCAL_HOST)
        assert status == 404, raw
        assert b"SECRET" not in body and b"PRIVATE MEMORY" not in body, raw
    status, _headers, body = _raw_get(b"/inside-link", host=LOCAL_HOST)
    assert status == 200 and body == b"0123456789"


def test_root_symlink_escape_is_refused_for_the_whole_site(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "index.html").write_text("LEAKED")
    linky = _make_wolt(tmp_path, "linky")
    shutil.rmtree(linky / "wolt" / "site")
    (linky / "wolt" / "site").symlink_to(outside, target_is_directory=True)
    response = _get("/", host="linky.sites.localhost:7117")
    assert response.status_code == 404 and "LEAKED" not in response.text
    assert _get("/index.html", host="linky.sites.localhost:7117").status_code == 404
    leaky = _make_wolt(tmp_path, "leaky")
    (leaky / "wolt" / "memory" / "context.md").write_text("PRIVATE CONTEXT")
    shutil.rmtree(leaky / "wolt" / "site")
    (leaky / "wolt" / "site").symlink_to("memory", target_is_directory=True)
    for path in ("/", "/context.md", "/identity.md"):
        response = _get(path, host="leaky.sites.localhost:7117")
        assert response.status_code == 404 and "PRIVATE" not in response.text, path


def test_case_ambiguous_wolt_directories_share_no_host(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    (tmp_path / "CaseProbe").mkdir()
    if (tmp_path / "caseprobe").exists():
        pytest.skip("case-insensitive filesystem: two such directories cannot exist")
    _make_wolt(tmp_path, "Pine")
    assert _get("/", host="pine.sites.localhost:7117").status_code == 404


# ---------------------------------------------------------------------------
# No lodge on a site host
# ---------------------------------------------------------------------------

LODGE_LIKE = [
    "/settings", "/sessions", "/notify", "/tui", "/apps", "/history/x",
    "/wolt/astre/_/memory.json", "/wolt/astre/_/manifest.json", "/wolt/astre/_/",
    "/sites", "/sites/astre/sharing", "/wolts", "/health",
]


@pytest.mark.parametrize("path", LODGE_LIKE)
def test_absent_lodge_like_paths_are_not_found(tmp_path, monkeypatch, path):
    _colony(tmp_path, monkeypatch)
    response = _get(path, host=LOCAL_HOST)
    assert response.status_code == 404
    _assert_site_headers(response)


def test_sentinel_files_named_like_lodge_routes_are_inert_content(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    site = tmp_path / "astre" / "wolt" / "site"
    for name in ("settings", "sessions", "notify", "tui", "apps"):
        (site / name).write_text(f"sentinel:{name}")
    (site / "wolt" / "astre" / "_").mkdir(parents=True)
    (site / "wolt" / "astre" / "_" / "memory.json").write_text('{"sentinel": "memory"}')
    for name in ("settings", "sessions", "notify", "tui", "apps"):
        response = _get(f"/{name}", host=LOCAL_HOST)
        assert response.status_code == 200 and response.text == f"sentinel:{name}"
        assert response.headers["content-type"] == "application/octet-stream"
        _assert_site_headers(response)
    memory = _get("/wolt/astre/_/memory.json", host=LOCAL_HOST)
    assert memory.json() == {"sentinel": "memory"}
    assert "PRIVATE MEMORY" not in memory.text
    # The gateway does not know the lodge app at all.
    source = (ROOT / "server" / "gateway.py").read_text()
    assert "from .app import" not in source and "server.app" not in source
    for module in ("server.site_server", "server.site_livereload", "server.content_hosts"):
        assert "from .app import" not in (ROOT / module.replace(".", "/")).with_suffix(".py").read_text()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@pytest.mark.parametrize("path", ["/", "/settings", "/index.html", "/_woltspace/livereload", "/sessions"])
def test_writes_are_refused_regardless_of_filename(tmp_path, monkeypatch, method, path):
    _colony(tmp_path, monkeypatch)
    (tmp_path / "astre" / "wolt" / "site" / "settings").write_text("sentinel")
    response = asyncio.run(_request(
        method, path, host=LOCAL_HOST,
        headers={"origin": "http://astre.sites.localhost:7117"},
    ))
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    _assert_site_headers(response)


# ---------------------------------------------------------------------------
# Reserved namespace and shell assets
# ---------------------------------------------------------------------------

def test_reserved_namespace_cannot_be_shadowed_by_site_files(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    site = tmp_path / "astre" / "wolt" / "site"
    (site / "_woltspace" / "assets").mkdir(parents=True)
    (site / "_woltspace" / "assets" / "shell.js").write_text("EVIL_SHELL")
    (site / "_woltspace" / "assets" / "wolt.html").write_text("EVIL_PAGE")
    (site / "_woltspace" / "other.txt").write_text("EVIL_OTHER")
    (site / "_woltspace" / "livereload").write_text("EVIL_SOCKET")

    shell_js = _get("/_woltspace/assets/shell.js", host=LOCAL_HOST)
    assert shell_js.status_code == 200
    assert "EVIL" not in shell_js.text and "wolt site shell" in shell_js.text
    assert shell_js.headers["content-type"] == "text/javascript; charset=utf-8"
    _assert_site_headers(shell_js)
    shell_css = _get("/_woltspace/assets/shell.css", host=LOCAL_HOST)
    assert shell_css.status_code == 200 and shell_css.headers["content-type"] == "text/css; charset=utf-8"
    for path in ("/_woltspace/assets/wolt.html", "/_woltspace/other.txt", "/_woltspace/livereload",
                 "/_woltspace/", "/_woltspace", "/_woltspace/assets/", "/_woltspace/assets/missing.js"):
        response = _get(path, host=LOCAL_HOST)
        assert response.status_code == 404, path
        assert "EVIL" not in response.text
    status, _headers, body = _raw_get(b"/_woltspace/assets/..%2F..%2Findex.html", host=LOCAL_HOST)
    assert status == 404
    status, _headers, body = _raw_get(b"/_woltspace/assets/shell.js/../shell.css", host=LOCAL_HOST)
    assert status == 404


def test_shell_assets_come_only_from_the_bounded_map():
    assert set(site_server.SHELL_ASSETS) == {"shell.js", "shell.css"}
    for path in site_server.SHELL_ASSETS.values():
        assert path.parent == ROOT / "public" / "static" / "wolt-shell"
        assert path.is_file()


# ---------------------------------------------------------------------------
# Shell injection and the public manifest
# ---------------------------------------------------------------------------

def test_owner_page_gets_public_manifest_reload_client_and_lodge_links(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    page = _get("/", host=LOCAL_HOST)
    html = page.text
    manifest = _manifest(html)
    assert set(manifest) == {"v", "wolt", "site", "tree", "base", "assets", "builtin"}
    assert manifest["wolt"] == {"name": "astre", "type": "raccoon", "role": "tester"}
    assert manifest["base"] == "/" and manifest["assets"] == "/_woltspace/assets/"
    assert manifest["builtin"] == "http://localhost:7777/wolt/astre/_/"
    assert [item["path"] for item in manifest["tree"] if "path" in item][0] == "index.html"
    assert re.search(r'<script src="/_woltspace/assets/shell\.js\?v=\d+-\d+"></script>', html)
    assert "/static/wolt-shell/" not in html
    assert site_server.LIVERELOAD_PATH in html
    assert html.index("</h1>") < html.index("livereload") < html.rindex("</body>")
    for private in ("secret-model", "SECRET_KEY", "harness", "PRIVATE MEMORY", str(tmp_path), "memory"):
        assert private not in html, private


def test_shared_viewer_gets_content_navigation_only(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _site_shares(tmp_path, {"astre": [FRIEND]})
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://owner.woltspace.com")
    friend = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND)).text
    owner = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud")).text
    shared = _manifest(friend)
    assert "builtin" not in shared
    assert site_server.LIVERELOAD_PATH not in friend
    assert "localhost" not in friend and "woltspace.com" not in friend
    assert _manifest(owner)["builtin"] == "https://owner.woltspace.com/wolt/astre/_/"
    assert site_server.LIVERELOAD_PATH in owner


def test_remote_owner_without_a_tunnel_address_gets_no_lodge_links(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    owner = _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud")).text
    assert "builtin" not in _manifest(owner)
    assert site_server.LIVERELOAD_PATH in owner


def test_public_manifest_has_no_private_fields(tmp_path):
    wolt = _make_wolt(tmp_path, "astre")
    (wolt / "wolt" / "site" / "site.json").write_text(json.dumps({"title": "Astre!", "shell": True}))
    private = site_shell.shell_manifest("astre", wolt, wolt / "wolt" / "site")
    public = site_shell.public_shell_manifest("astre", wolt, wolt / "wolt" / "site")
    assert "harness" in private["wolt"] and "harness" not in public["wolt"]
    assert set(public["wolt"]) <= set(site_shell.PUBLIC_WOLT_FIELDS)
    assert "builtin" not in public and public["base"] == "/"
    assert public["site"] == {"title": "Astre!"}
    text = json.dumps(public)
    assert str(tmp_path) not in text and "memory" not in text


def test_shell_can_be_turned_off_per_site(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    (tmp_path / "astre" / "wolt" / "site" / "site.json").write_text(json.dumps({"shell": False}))
    html = _get("/", host=LOCAL_HOST).text
    assert "__WOLT_SHELL__" not in html
    assert site_server.LIVERELOAD_PATH in html


# ---------------------------------------------------------------------------
# Service workers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/sw.js", "/nested/worker.js", "/index.html", "/", "/_woltspace/assets/shell.js", "/module.mjs"])
def test_service_worker_scripts_are_refused_on_every_path(tmp_path, monkeypatch, path):
    _colony(tmp_path, monkeypatch)
    site = tmp_path / "astre" / "wolt" / "site"
    (site / "sw.js").write_text("self.addEventListener('fetch', () => {})")
    (site / "nested" / "worker.js").write_text("// worker")
    (site / "module.mjs").write_text("export default 1")
    plain = _get(path, host=LOCAL_HOST)
    assert plain.status_code == 200
    refused = _get(path, host=LOCAL_HOST, headers={"service-worker": "script"})
    assert refused.status_code == 403
    _assert_site_headers(refused)
    assert "service-worker-allowed" not in {k.lower() for k in plain.headers}


# ---------------------------------------------------------------------------
# Collisions with reserved app names
# ---------------------------------------------------------------------------

def test_used_origin_is_never_reused_for_a_site(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _make_wolt(tmp_path, "notes")
    host = f"site-notes.{APPS_DOMAIN}"
    apps = {"site-notes": object()}
    monkeypatch.setattr(gateway, "get_app", lambda name: apps.get(name))
    token = _token(key, "apps-aud")

    collision = _get("/", host=host, token=token)
    assert collision.status_code == 409
    assert "never reused" in collision.text and "notes home" not in collision.text
    assert "Rename" not in collision.text
    _assert_site_headers(collision)
    # Renaming or removing the app changes nothing once the host has history.
    apps.clear()
    (tmp_path / ".space" / "platform").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".space" / "platform" / "content-origins.json").write_text(json.dumps({
        host: {"app": "2026-10-01T00:00:00+00:00"},
    }))
    assert _get("/", host=host, token=token).status_code == 409
    content_origins._SEEN.clear()  # a gateway restart reads the same record
    assert _get("/", host=host, token=token).status_code == 409
    # A history that cannot be read is not a clean history.
    (tmp_path / ".space" / "platform" / "content-origins.json").write_text("{broken")
    assert _get("/", host=host, token=token).status_code == 409
    # Only a host with no app history at all is served.
    (tmp_path / ".space" / "platform" / "content-origins.json").write_text(json.dumps({
        host: {"site": "2026-10-01T00:00:00+00:00"},
    }))
    served = _get("/", host=host, token=token)
    assert served.status_code == 200 and "notes home" in served.text
    # The reserved name is never an app locally either, and says why.
    local = _get("/", host="site-notes.localhost:7117")
    assert local.status_code == 404 and "reserved" in local.text
    assert _get("/", host="notes.sites.localhost:7117").status_code == 200


def test_every_served_host_is_recorded_once(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)

    async def proxy(_request, name):
        return PlainTextResponse(f"{name}:APP")

    monkeypatch.setattr(gateway, "proxy_app_http", proxy)
    record = tmp_path / ".space" / "platform" / "content-origins.json"
    assert _get("/", host=LOCAL_HOST).status_code == 200
    assert _get("/", host="notes.localhost:7117").status_code == 200
    assert _get("/", host=f"notes.{APPS_DOMAIN}", token=_token(key, "apps-aud")).status_code == 200
    assert _get("/", host="nobody.sites.localhost:7117").status_code == 404
    assert _get("/", host=f"notes.{APPS_DOMAIN}", token=_token(key, "apps-aud", STRANGER)).status_code == 403
    data = _origins(tmp_path)
    assert set(data) == {"astre.sites.localhost", "notes.localhost", f"notes.{APPS_DOMAIN}"}
    assert set(data["astre.sites.localhost"]) == {"site"}
    assert set(data["notes.localhost"]) == set(data[f"notes.{APPS_DOMAIN}"]) == {"app"}
    first = record.stat().st_mtime_ns
    assert _get("/data.json", host=LOCAL_HOST).status_code == 200
    assert record.stat().st_mtime_ns == first
    assert content_origins.host_kinds(tmp_path, "ASTRE.sites.localhost.") == {"site"}
    assert content_origins.host_kinds(tmp_path, "other.localhost") == set()
    # A broken record is never overwritten, and serving goes on.
    record.write_text("{broken")
    content_origins._SEEN.clear()
    assert _get("/", host="pine.sites.localhost:7117").status_code == 200
    assert record.read_text() == "{broken"
    with pytest.raises(RuntimeError):
        content_origins.host_kinds(tmp_path, LOCAL_HOST)
    assert not list(record.parent.glob("*.tmp"))


def test_doctor_warns_about_site_prefixed_apps(tmp_path):
    sys.path.insert(0, str(ROOT / "src"))
    from woltspace.doctor import run_doctor
    from woltspace.layout import RuntimeLayout, installation_root

    layout = RuntimeLayout(
        wolts_dir=tmp_path / "wolts", install_root=installation_root(),
        host="127.0.0.1", port=18777, isolation="host",
    )
    app_dir = tmp_path / "wolts" / "apps" / "site-notes"
    app_dir.mkdir(parents=True)
    (app_dir / "woltspace.json").write_text(json.dumps({"name": "site-notes", "port": 4321}))
    checks = {check.name: check for check in run_doctor(layout, check_port=False)}
    assert checks["app-names"].status == "warn"
    assert "site-notes" in checks["app-names"].detail
    assert "reserved" in checks["app-names"].remedy and "Rename" not in checks["app-names"].remedy
    shutil.rmtree(app_dir)
    assert "app-names" not in {check.name for check in run_doctor(layout, check_port=False)}


# ---------------------------------------------------------------------------
# Livereload
# ---------------------------------------------------------------------------

def _ws_client():
    return TestClient(gateway.app, client=("127.0.0.1", 123))


def _ws_headers(host=LOCAL_HOST, origin="http://astre.sites.localhost:7117", token=None):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    if token:
        headers["cf-access-jwt-assertion"] = token
    return headers


def _fast_watch(monkeypatch):
    monkeypatch.setattr(livereload, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(livereload, "RECHECK_SECONDS", 0.2)


def test_owner_receives_a_reload_after_a_file_change(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    _fast_watch(monkeypatch)
    index = tmp_path / "astre" / "wolt" / "site" / "index.html"
    with _ws_client().websocket_connect(site_server.LIVERELOAD_PATH, headers=_ws_headers()) as ws:
        time.sleep(0.2)
        index.write_text("<html><body>changed</body></html>")
        os.utime(index, ns=(time.time_ns(), time.time_ns()))
        message = json.loads(ws.receive_text())
        assert message == {"type": "reload", "revision": 1}
        ws.send_text("ignored")  # nothing from the browser is read as a command
        (tmp_path / "astre" / "wolt" / "site" / "new.html").write_text("<html></html>")
        assert json.loads(ws.receive_text()) == {"type": "reload", "revision": 2}
    assert livereload.hub.watches == {}


def test_remote_owner_gets_livereload_and_shared_viewer_is_denied(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _fast_watch(monkeypatch)
    _site_shares(tmp_path, {"astre": [FRIEND]})
    origin = f"https://{REMOTE_HOST}"
    with _ws_client().websocket_connect(
        site_server.LIVERELOAD_PATH,
        headers=_ws_headers(REMOTE_HOST, origin, _token(key, "apps-aud")),
    ) as ws:
        time.sleep(0.2)  # let the watcher take its first snapshot
        (tmp_path / "astre" / "wolt" / "site" / "x.html").write_text("<html></html>")
        assert json.loads(ws.receive_text())["type"] == "reload"
    # The friend may view the site, yet never gets the owner's reload stream.
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND)).status_code == 200
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client().websocket_connect(
            site_server.LIVERELOAD_PATH,
            headers=_ws_headers(REMOTE_HOST, origin, _token(key, "apps-aud", FRIEND)),
        ):
            pass
    assert exc.value.code == 1008
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client().websocket_connect(
            site_server.LIVERELOAD_PATH,
            headers=_ws_headers(REMOTE_HOST, origin, _token(key, "apps-aud", STRANGER)),
        ):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/socket", "/livereload", "/_woltspace/livereload/", "/_woltspace/other", "/"])
def test_other_websocket_paths_are_rejected_before_accept(tmp_path, monkeypatch, path):
    _colony(tmp_path, monkeypatch)
    accepted = []
    monkeypatch.setattr(gateway, "serve_site_livereload", lambda *a, **k: accepted.append(1))
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client().websocket_connect(path, headers=_ws_headers()):
            pass
    assert exc.value.code == 1008 and not accepted


@pytest.mark.parametrize("origin", [
    None, "null", "http://pine.sites.localhost:7117", "http://notes.localhost:7117",
    "http://localhost:7777", "http://astre.sites.localhost:7118", "http://astre.sites.localhost",
    "https://astre.sites.localhost:7117", "http://evil.example", "http://astre.sites.localhost:7117/path",
])
def test_livereload_rejects_every_origin_but_the_sites_own(tmp_path, monkeypatch, origin):
    _colony(tmp_path, monkeypatch)
    with pytest.raises(WebSocketDisconnect) as exc:
        with _ws_client().websocket_connect(site_server.LIVERELOAD_PATH, headers=_ws_headers(origin=origin)):
            pass
    assert exc.value.code == 1008


def test_remote_livereload_origin_must_be_https_on_the_default_port(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _fast_watch(monkeypatch)
    token = _token(key, "apps-aud")
    for bad in (f"http://{REMOTE_HOST}", f"https://{REMOTE_HOST}:8443", f"https://notes.{APPS_DOMAIN}"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with _ws_client().websocket_connect(
                site_server.LIVERELOAD_PATH, headers=_ws_headers(REMOTE_HOST, bad, token),
            ):
                pass
        assert exc.value.code == 1008, bad
    with _ws_client().websocket_connect(
        site_server.LIVERELOAD_PATH, headers=_ws_headers(REMOTE_HOST, f"https://{REMOTE_HOST}:443", token),
    ):
        pass


def test_livereload_closes_when_the_token_expires(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    _fast_watch(monkeypatch)
    with _ws_client().websocket_connect(
        site_server.LIVERELOAD_PATH,
        headers=_ws_headers(REMOTE_HOST, f"https://{REMOTE_HOST}", _token(key, "apps-aud", ttl=1)),
    ) as ws:
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == 1008


def test_livereload_closes_when_the_site_is_removed(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    _fast_watch(monkeypatch)
    with _ws_client().websocket_connect(site_server.LIVERELOAD_PATH, headers=_ws_headers()) as ws:
        shutil.rmtree(tmp_path / "astre" / "wolt" / "site")
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == 1001


def test_livereload_closes_when_the_owner_identity_changes(tmp_path, monkeypatch):
    _colony(tmp_path, monkeypatch)
    _fast_watch(monkeypatch)
    with _ws_client().websocket_connect(site_server.LIVERELOAD_PATH, headers=_ws_headers()) as ws:
        config = json.loads((tmp_path / "woltspace.json").read_text())
        config["access"]["owner_email"] = "someone-else@example.com"
        (tmp_path / "woltspace.json").write_text(json.dumps(config))
        message = ws.receive()
        assert message["type"] == "websocket.close" and message["code"] == 1008


def test_livereload_subscribers_are_bounded(tmp_path, monkeypatch):
    # One event loop, as in the gateway process (every TestClient socket
    # would run its own loop, which the hub treats as a fresh start).
    monkeypatch.setattr(livereload, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(livereload, "MAX_SUBSCRIBERS_PER_SITE", 1)
    root, other = str(tmp_path / "a"), str(tmp_path / "b")
    for path in (root, other):
        os.mkdir(path)

    async def scenario():
        hub = livereload.LivereloadHub()
        monkeypatch.setattr(livereload, "hub", hub)
        first = hub.subscribe(root)
        assert first is not None
        assert hub.subscribe(root) is None
        closed = []

        class Stub:
            async def accept(self):
                raise AssertionError("accepted past the bound")

            async def close(self, code=1000):
                closed.append(code)

        async def yes():
            return True

        await livereload.serve_site_livereload(Stub(), root, still_authorized=yes)
        assert closed == [1013]
        monkeypatch.setattr(livereload, "MAX_SUBSCRIBERS_TOTAL", 1)
        assert hub.subscribe(other) is None
        hub.unsubscribe(root, first)
        assert hub.watches == {}
        await hub.shutdown()

    asyncio.run(scenario())


def test_reload_traffic_does_not_postpone_the_authorization_recheck(monkeypatch):
    monkeypatch.setattr(livereload, "RECHECK_SECONDS", 0.03)

    async def scenario(auth_answers):
        queue: asyncio.Queue = asyncio.Queue()
        calls, sent, closed = [], [], []

        class Hub:
            def subscribe(self, _root):
                return queue

            def unsubscribe(self, _root, _queue):
                pass

        class WS:
            async def accept(self):
                pass

            async def receive(self):
                await asyncio.sleep(0.25)
                return {"type": "websocket.disconnect"}

            async def send_text(self, text):
                sent.append(text)

            async def close(self, code=1000):
                closed.append(code)

        async def auth():
            calls.append(time.monotonic())
            return auth_answers(len(calls))

        async def feed():
            for index in range(60):
                queue.put_nowait({"type": "reload", "revision": index})
                await asyncio.sleep(0.005)

        monkeypatch.setattr(livereload, "hub", Hub())
        feeder = asyncio.create_task(feed())
        started = time.monotonic()
        await livereload.serve_site_livereload(WS(), "unused", still_authorized=auth)
        feeder.cancel()
        return calls, sent, closed, time.monotonic() - started

    calls, sent, closed, _elapsed = asyncio.run(scenario(lambda _n: True))
    assert len(calls) >= 5, len(calls)          # about every 0.03 s over 0.25 s
    assert len(sent) >= 30 and closed == []
    # Authorization lost under steady traffic: closed within one interval.
    calls, sent, closed, elapsed = asyncio.run(scenario(lambda n: n < 2))
    assert closed == [1008] and len(calls) == 2
    assert elapsed < 0.15, elapsed
    assert all(json.loads(text)["type"] == "reload" for text in sent)


def test_watcher_caps_the_files_it_tracks(tmp_path, monkeypatch):
    monkeypatch.setattr(livereload, "MAX_FILES", 5)
    for index in range(20):
        (tmp_path / f"{index}.html").write_text("x")
    assert len(livereload.snapshot_tree(str(tmp_path))) == 5
    assert livereload.snapshot_tree(str(tmp_path / "missing")) is None


# ---------------------------------------------------------------------------
# Lodge owner API and CLI for site shares
# ---------------------------------------------------------------------------

async def _lodge(method, path, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=lodge.app), base_url="http://localhost:7777",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_lodge_site_sharing_routes_mirror_app_sharing(tmp_path, monkeypatch):
    key = _colony(tmp_path, monkeypatch)
    _enable_remote(monkeypatch)
    monkeypatch.setattr(lodge, "WOLTS_DIR", tmp_path)
    _make_wolt(tmp_path, "my_wolt")

    empty = asyncio.run(_lodge("GET", "/sites/astre/sharing"))
    assert empty.status_code == 200
    assert empty.json() == {"entries": [], "enabled": True, "host": "astre", "unsupported": None}
    saved = asyncio.run(_lodge("PUT", "/sites/astre/sharing", json={"entries": ["Friend@Example.com", "@team.example"]}))
    assert saved.status_code == 200
    assert saved.json()["entries"] == ["@team.example", FRIEND]
    state = json.loads((tmp_path / ".space" / "platform" / "site-sharing.json").read_text())
    assert state == {"astre": ["@team.example", FRIEND]}
    assert not (tmp_path / ".space" / "platform" / "app-sharing.json").exists()

    assert asyncio.run(_lodge("GET", "/sites/nobody/sharing")).status_code == 404
    assert asyncio.run(_lodge("GET", "/sites/..%2Fastre/sharing")).status_code in {400, 404}
    assert asyncio.run(_lodge("PUT", "/sites/astre/sharing", json={"entries": ["nope"]})).status_code == 400
    assert asyncio.run(_lodge("PUT", "/sites/astre/sharing", json={"other": []})).status_code == 400
    unsupported = asyncio.run(_lodge("GET", "/sites/my_wolt/sharing")).json()
    assert unsupported["host"] is None and "DNS label" in unsupported["unsupported"]

    # What the lodge wrote is what the gateway enforces.
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND)).status_code == 200
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", "anyone@team.example")).status_code == 200
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", STRANGER)).status_code == 403
    asyncio.run(_lodge("PUT", "/sites/astre/sharing", json={"entries": []}))
    assert _get("/", host=REMOTE_HOST, token=_token(key, "apps-aud", FRIEND)).status_code == 403


def test_cli_site_sharing_talks_to_the_site_routes():
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    calls = []

    def request(method, path, body=None):
        calls.append((method, path, body))
        if method == "GET":
            return 200, {"entries": [FRIEND], "enabled": True, "host": "astre", "unsupported": None}
        return 200, {"ok": True, "entries": body["entries"], "enabled": True}

    client["_sharing"].__globals__["_req"] = request
    client["cmd_site_sharing"](argparse.Namespace(
        name="astre", verb="share", entries=["new@example.com"], json=False,
    ))
    client["cmd_site_sharing"](argparse.Namespace(name="astre", verb="unshare", entries=[FRIEND], json=False))
    assert calls == [
        ("GET", "/sites/astre/sharing", None),
        ("PUT", "/sites/astre/sharing", {"entries": [FRIEND, "new@example.com"]}),
        ("GET", "/sites/astre/sharing", None),
        ("PUT", "/sites/astre/sharing", {"entries": []}),
    ]
    assert "site" in client["LOCAL_NOUNS"]
    parser = client["build_parser"]() if "build_parser" in client else None
    if parser is not None:
        args = parser.parse_args(["site", "sharing", "astre"])
        assert args.func is client["cmd_site_sharing"]


def test_new_test_file_is_in_the_focused_release_checks():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "test/test_site_gateway.py" in workflow
