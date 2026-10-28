"""The switch-over: sites and apps through the tunnel go to the Mill once
Cloudflare Access vouches for the owner; old addresses redirect; nothing of a
wolt's runs on the lodge's origin."""

import json
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.access as access
import server.app as server_app
import server.mill as mill_server
from server.access import AccessSettings

DOMAIN = "woltspace.test"
LODGE_HOST = f"owner.{DOMAIN}"
SETTINGS = AccessSettings("team.cloudflareaccess.com", "lodge-aud", "apps-aud", "owner@example.com")


class _Seen:
    requests: list = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, body: bytes):
        _Seen.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(b"board says hi")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._reply(b"posted")

    def log_message(self, *args):
        pass


class FakeApps:
    def __init__(self, running):
        self.running = running

    def running_apps(self):
        return [{"name": n, "port": p} for n, p in self.running.items()]

    def get_app(self, name):
        return SimpleNamespace(can_start=lambda: True) if name in self.running else None

    def app_dir(self, name):
        raise KeyError(name)


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """A real Mill on a unix socket, a running app, a tunnel domain, and Access settings."""
    import uvicorn
    _Seen.requests = []
    app_server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=app_server.serve_forever, daemon=True).start()
    site = tmp_path / "softie" / "wolt" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<p>softie's site</p>")
    path = Path(tempfile.mkdtemp(prefix="mill", dir="/tmp")) / "mill.sock"
    fake = FakeApps({"board": app_server.server_address[1]})
    mill = uvicorn.Server(uvicorn.Config(mill_server.Mill(tmp_path, apps=fake), uds=str(path),
                                         log_level="error", lifespan="on"))
    thread = threading.Thread(target=mill.run, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.05)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app.mill_server, "socket_path", lambda _root: path)
    monkeypatch.setattr(server_app, "get_app", fake.get_app)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", LODGE_HOST)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", DOMAIN)
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    monkeypatch.setattr(access, "load_access_settings", lambda root: SETTINGS)

    async def verify(token, settings, audience):
        if token == "good" and audience == "apps-aud":
            return {"email": "Owner@Example.com", "exp": time.time() + 600}
        if token == "stranger" and audience == "apps-aud":
            return {"email": "someone@example.com", "exp": time.time() + 600}
        raise ValueError("bad token")

    monkeypatch.setattr(access.access_token_verifier, "verify", verify)
    yield tmp_path
    mill.should_exit = True
    app_server.shutdown()
    thread.join(5)


def _through_tunnel(host: str) -> TestClient:
    # cloudflared runs on this machine: a tunnel request comes from loopback,
    # with Cloudflare's forwarding headers.
    return TestClient(server_app.app, base_url=f"https://{host}", client=("127.0.0.1", 50000),
                      headers={"cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9"})


# --- Apps and sites through the tunnel -------------------------------------------------------

def test_an_app_through_the_tunnel_opens_for_the_owners_access_token(lodge):
    response = _through_tunnel(f"board.{DOMAIN}").get("/", headers={
        "cf-access-jwt-assertion": "good", "cookie": "CF_Authorization=good; app=1"})
    assert response.status_code == 200 and response.text == "board says hi"
    _method, _path, sent = _Seen.requests[-1]
    assert "cf-access-jwt-assertion" not in sent and sent["cookie"] == "app=1"
    assert mill_server.IDENTITY_HEADER not in sent


@pytest.mark.parametrize("headers", [
    {}, {"cf-access-jwt-assertion": "forged"}, {"cf-access-jwt-assertion": "stranger"},
    {mill_server.IDENTITY_HEADER: "owner"},
])
def test_without_the_owners_access_token_nobody_gets_in(lodge, headers):
    assert _through_tunnel(f"board.{DOMAIN}").get("/", headers=headers).status_code == 403


def test_without_access_settings_nothing_through_the_tunnel_opens(lodge, monkeypatch):
    monkeypatch.setattr(access, "load_access_settings", lambda root: None)
    response = _through_tunnel(f"board.{DOMAIN}").get("/", headers={"cf-access-jwt-assertion": "good"})
    assert response.status_code == 403


def test_a_write_from_the_apps_public_page_is_its_own_page(lodge):
    ok = _through_tunnel(f"board.{DOMAIN}").post("/x", headers={
        "cf-access-jwt-assertion": "good", "origin": f"https://board.{DOMAIN}"})
    assert ok.status_code == 200 and ok.text == "posted"
    foreign = _through_tunnel(f"board.{DOMAIN}").post("/x", headers={
        "cf-access-jwt-assertion": "good", "origin": "https://evil.example"})
    assert foreign.status_code == 403


def _remote_sites_on(root):
    (root / "woltspace.json").write_text(json.dumps({"mill": {"remote_sites": True}}))


def test_a_wolts_site_through_the_tunnel(lodge):
    _remote_sites_on(lodge)
    response = _through_tunnel(f"site-softie.{DOMAIN}").get("/", headers={"cf-access-jwt-assertion": "good"})
    assert response.status_code == 200 and "softie's site" in response.text
    assert _through_tunnel(f"site-softie.{DOMAIN}").get("/").status_code == 403


def test_the_lodge_through_the_tunnel_is_still_the_lodge(lodge):
    assert _through_tunnel(LODGE_HOST).get("/version").status_code == 200


def test_opening_from_the_lodge_through_the_tunnel_goes_to_the_public_address(lodge):
    _remote_sites_on(lodge)
    lodge_client = _through_tunnel(LODGE_HOST)
    site = lodge_client.get("/sites/open?wolt=softie&path=/a.html", follow_redirects=False)
    assert site.headers["location"] == f"https://site-softie.{DOMAIN}/a.html"
    app = lodge_client.get("/apps/open?app=board&path=/x", follow_redirects=False)
    assert app.headers["location"] == f"https://board.{DOMAIN}/x"


def test_site_names_are_reserved_for_sites():
    from server.content_hosts import parse_app_host
    assert parse_app_host("site-softie.localhost:7777") is None


# --- What's gone from the lodge's origin ------------------------------------------------------

def _local(host="localhost:7777"):
    return TestClient(server_app.app, base_url=f"http://{host}", client=("127.0.0.1", 50000))


def test_old_app_links_redirect_to_the_apps_own_origin(lodge):
    old = _local().get("/app/board/x/y", follow_redirects=False)
    assert old.status_code == 303 and old.headers["location"] == "/apps/open?app=board&path=/x/y"


def test_per_app_quick_tunnels_are_gone():
    import apps
    paths = {route.path for route in server_app.app.routes}
    assert not {"/apps/{name}/share", "/apps/{name}/unshare", "/apps/unshare-all"} & paths
    assert not hasattr(apps, "share_app") and not hasattr(apps, "SHARING_ENABLED")


def test_sparks_run_opaque_whether_or_not_auth_is_on(monkeypatch):
    async def spark(spark_id):
        return {"id": spark_id, "html": "<script>1</script>", "version": 1, "totalVersions": 1}

    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    monkeypatch.setattr(server_app, "get_spark_with_chain", spark)
    csp = _local().get("/history/abc").headers["content-security-policy"]
    assert csp.startswith("sandbox") and "allow-same-origin" not in csp


def test_the_lodge_root_never_serves_a_wolts_files(tmp_path, monkeypatch):
    site = tmp_path / "site"
    site.mkdir()
    (site / "evil.html").write_text("<script>fetch('/sessions')</script>")
    monkeypatch.setattr(server_app, "SITE_DIR", site)
    assert "<script>" not in _local().get("/evil.html").text


# --- doctor ---------------------------------------------------------------------------------

def _layout(root):
    from woltspace.layout import RuntimeLayout
    return RuntimeLayout.from_env({"WOLTSPACE_WOLTS_DIR": str(root)})


def _manifest(root, folder, name, start):
    (root / "apps" / folder).mkdir(parents=True)
    (root / "apps" / folder / "woltspace.json").write_text(json.dumps(
        {"name": name, "port": 4000, "keeper": "softie", "start": start}))


def test_doctor_names_wide_binds_and_shared_names(tmp_path):
    from woltspace import doctor
    _manifest(tmp_path, "blog", "blog", "astro dev --port $PORT --host 0.0.0.0")
    _manifest(tmp_path, "site-a", "woltspace-site", "npm run dev")
    _manifest(tmp_path, "site-b", "woltspace-site", "npm run dev")
    check = doctor._apps_check(_layout(tmp_path))
    assert check.status == "warn"
    assert "blog" in check.detail and "woltspace-site in site-a, site-b" in check.detail
    assert "--host $HOST" in check.remedy


def test_doctor_passes_loopback_apps(tmp_path):
    from woltspace import doctor
    _manifest(tmp_path, "blog", "blog", "astro dev --port $PORT --host $HOST")
    assert doctor._apps_check(_layout(tmp_path)).status == "pass"


def test_doctor_asks_for_access_settings_when_there_is_a_tunnel(tmp_path, monkeypatch):
    from woltspace import doctor
    monkeypatch.setenv("CLOUDFLARE_TUNNEL_URL", "https://owner.woltspace.test")
    assert doctor._remote_apps_check(_layout(tmp_path)).status == "warn"
    (tmp_path / "woltspace.json").write_text(json.dumps({"access": {
        "team_domain": "t.cloudflareaccess.com", "lodge_aud": "a", "apps_aud": "a",
        "owner_email": "o@example.com"}}))
    assert doctor._remote_apps_check(_layout(tmp_path)).status == "warn"  # one Access app for both
    (tmp_path / "woltspace.json").write_text(json.dumps({"access": {
        "team_domain": "t.cloudflareaccess.com", "lodge_aud": "a", "apps_aud": "b",
        "owner_email": "o@example.com"}}))
    assert doctor._remote_apps_check(_layout(tmp_path)).status == "pass"
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL")
    assert doctor._remote_apps_check(_layout(tmp_path)) is None


# --- Host names that are neither the lodge nor a content host ------------------------------

@pytest.mark.parametrize("auth", [False, True])
@pytest.mark.parametrize("host", [
    "site-foo.localhost:7777", "-x.localhost:7777", f"site-_foo.{DOMAIN}", f"a.b.{DOMAIN}",
    f"-x.{DOMAIN}",
])
def test_a_content_shaped_host_never_reaches_lodge_routes(lodge, monkeypatch, host, auth):
    from starlette.websockets import WebSocketDisconnect
    if auth:
        monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    response = TestClient(server_app.app, base_url=f"http://{host}",
                          client=("127.0.0.1", 50000)).get("/version")
    assert response.status_code in (401, 403)
    assert "version" not in response.text.lower() or "error" in response.text
    with pytest.raises(WebSocketDisconnect):
        with TestClient(server_app.app, base_url=f"http://{host}", client=("127.0.0.1", 50000)
                        ).websocket_connect("/tui?session=main", headers={"host": host}):
            pass


# --- The real Access token check (RS256, JWKS), with locally signed tokens ----------------

@pytest.fixture
def signer(monkeypatch):
    """A team signing key served as Cloudflare's certs endpoint would, and a token maker."""
    import asyncio
    import httpx
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "team-key"
    fetched = []

    def certs(request):
        fetched.append(str(request.url))
        return httpx.Response(200, json={"keys": [jwk]})

    real = httpx.AsyncClient
    monkeypatch.setattr(access.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(certs)))

    def token(*, aud="apps-aud", iss="https://team.cloudflareaccess.com", exp=600,
              email="owner@example.com", kid="team-key", signing=None, alg="RS256"):
        now = int(time.time())
        claims = {"aud": [aud], "iss": iss, "iat": now, "exp": now + exp, "email": email}
        headers = {"kid": kid} if kid else {}
        signer_key = signing or key
        if alg == "HS256":
            return jwt.encode(claims, "secret", algorithm="HS256", headers=headers)
        return jwt.encode(claims, signer_key, algorithm=alg, headers=headers)

    def verify(tok, audience="apps-aud"):
        return asyncio.run(access.AccessTokenVerifier().verify(tok, SETTINGS, audience))

    return SimpleNamespace(token=token, verify=verify, other=other, fetched=fetched)


def test_a_real_owner_token_for_the_apps_audience_verifies(signer):
    claims = signer.verify(signer.token())
    assert claims["email"] == "owner@example.com"
    assert signer.fetched == ["https://team.cloudflareaccess.com/cdn-cgi/access/certs"]


@pytest.mark.parametrize("bad", [
    dict(signing="other"),                               # wrong signature
    dict(aud="lodge-aud"),                               # the lodge's audience
    dict(iss="https://evil.cloudflareaccess.com"),       # another team
    dict(exp=-60),                                       # expired
    dict(kid="unknown-key"),                             # a key the team doesn't have
    dict(kid=None),                                      # no key id
    dict(alg="HS256"),                                   # not RS256
])
def test_a_wrong_token_fails_closed(signer, bad):
    if bad.get("signing") == "other":
        bad = dict(bad, signing=signer.other)
    with pytest.raises(Exception):
        signer.verify(signer.token(**bad))


@pytest.mark.parametrize("email,expected", [("owner@example.com", "owner"),
                                            ("OWNER@example.com", "owner"),
                                            ("someone@example.com", None)])
def test_the_lodge_lets_in_only_the_owners_verified_email(signer, monkeypatch, email, expected):
    import asyncio
    monkeypatch.setattr(access, "load_access_settings", lambda root: SETTINGS)
    monkeypatch.setattr(access, "access_token_verifier", access.AccessTokenVerifier())
    visitor, _exp = asyncio.run(server_app._remote_visitor(
        {"cf-access-jwt-assertion": signer.token(email=email)}))
    assert visitor == expected
    lodge_token = signer.token(aud="lodge-aud")
    assert asyncio.run(server_app._remote_visitor({"cf-access-jwt-assertion": lodge_token}))[0] is None


# --- Review: Origin through the tunnel, old quick tunnels, site- names -----------------------

@pytest.mark.parametrize("origin", [
    "http://board.localhost:7777",                 # the local form, before the rewrite
    f"https://evil.{DOMAIN}",                      # a sibling
    f"http://board.{DOMAIN}",                      # wrong scheme
    f"https://board.{DOMAIN}:8443",                # wrong port
    "null",
])
def test_a_write_through_the_tunnel_needs_the_apps_own_public_origin(lodge, origin):
    before = len(_Seen.requests)
    response = _through_tunnel(f"board.{DOMAIN}").post("/x", headers={
        "cf-access-jwt-assertion": "good", "origin": origin})
    assert response.status_code == 403
    assert len(_Seen.requests) == before  # nothing reached the app


def test_a_cross_site_write_through_the_tunnel_is_refused(lodge):
    response = _through_tunnel(f"board.{DOMAIN}").post("/x", headers={
        "cf-access-jwt-assertion": "good", "sec-fetch-site": "cross-site"})
    assert response.status_code == 403


def test_a_websocket_through_the_tunnel_needs_the_public_origin(lodge):
    from starlette.websockets import WebSocketDisconnect
    host = f"board.{DOMAIN}"
    for origin in (None, "http://board.localhost:7777"):
        headers = {"host": host, "cf-access-jwt-assertion": "good"}
        if origin:
            headers["origin"] = origin
        with pytest.raises(WebSocketDisconnect):
            with _through_tunnel(host).websocket_connect("/ws", headers=headers):
                pass


def test_remote_sites_are_off_until_the_owner_turns_them_on(lodge, monkeypatch):
    client = _through_tunnel(f"site-softie.{DOMAIN}")
    assert client.get("/", headers={"cf-access-jwt-assertion": "good"}).status_code == 503
    assert _through_tunnel(LODGE_HOST).get("/sites/open?wolt=softie", follow_redirects=False).status_code == 503
    _remote_sites_on(lodge)
    assert client.get("/", headers={"cf-access-jwt-assertion": "good"}).status_code == 200
    # An app that holds the name keeps it from the site.
    monkeypatch.setattr(server_app, "get_app",
                        lambda name: SimpleNamespace(can_start=lambda: True) if name == "site-softie" else None)
    assert client.get("/", headers={"cf-access-jwt-assertion": "good"}).status_code == 503


@pytest.fixture
def tunnel_records(monkeypatch):
    import apps
    rows = {}
    monkeypatch.setattr(apps, "_all_states", lambda: [dict(v) for v in rows.values()])
    monkeypatch.setattr(apps, "_write_state", lambda name, state: rows.__setitem__(name, dict(state)))
    alive, killed = set(), []
    monkeypatch.setattr(apps, "is_cloudflared", lambda pid: pid in alive)

    def stop(pid):
        killed.append(pid)
        if pid != 999:  # 999 ignores the signal
            alive.discard(pid)
        return True

    monkeypatch.setattr(apps, "stop_cloudflared", stop)
    return SimpleNamespace(apps=apps, rows=rows, alive=alive, killed=killed)


def test_old_quick_tunnels_are_retired_once_and_only_when_verified(tunnel_records, monkeypatch):
    t = tunnel_records
    monkeypatch.setattr(t.apps.time, "sleep", lambda s: None)
    t.rows.update({
        "live": {"name": "live", "tunnel_pid": 101, "tunnel_url": "https://a.trycloudflare.com"},
        "reused": {"name": "reused", "tunnel_pid": 202, "tunnel_url": "https://b.trycloudflare.com"},
        "stuck": {"name": "stuck", "tunnel_pid": 999, "tunnel_url": "https://c.trycloudflare.com"},
        "plain": {"name": "plain", "tunnel_pid": None, "tunnel_url": None},
    })
    t.alive.update({101, 999})  # 202's pid now belongs to something else
    results = {r["name"]: r["action"] for r in t.apps.retire_app_tunnels()}
    assert results == {"live": "tunnel-retired", "reused": "tunnel-retired", "stuck": "tunnel-still-running"}
    assert 202 not in t.killed  # a reused pid is never signalled
    assert t.rows["live"]["tunnel_pid"] is None and t.rows["live"]["tunnel_url"] is None
    assert t.rows["stuck"]["tunnel_pid"] == 999  # still reported next time
    t.alive.discard(999)
    again = {r["name"]: r["action"] for r in t.apps.retire_app_tunnels()}
    assert again == {"stuck": "tunnel-retired"}
    assert t.apps.retire_app_tunnels() == []


def test_boot_retires_old_tunnels_before_restoring_apps(monkeypatch):
    import apps
    order = []
    monkeypatch.setattr(apps, "retire_app_tunnels", lambda: order.append("retire") or [])
    monkeypatch.setattr(apps, "_all_states", lambda: order.append("restore") or [])
    apps.apps_restore()
    assert order == ["retire", "restore"]


def test_doctor_fails_while_an_old_tunnel_still_runs(monkeypatch, tmp_path):
    import apps
    from woltspace import doctor
    monkeypatch.setattr(apps, "intended_apps", lambda: [{"name": "blog", "tunnel_pid": 77}])
    monkeypatch.setattr(apps, "is_cloudflared", lambda pid: True)
    check = doctor._app_tunnels_check(_layout(tmp_path))
    assert check.status == "fail" and "blog" in check.detail
    monkeypatch.setattr(apps, "is_cloudflared", lambda pid: False)
    assert doctor._app_tunnels_check(_layout(tmp_path)) is None
