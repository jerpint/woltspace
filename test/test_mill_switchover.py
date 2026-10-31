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
SETTINGS = AccessSettings("team.cloudflareaccess.com", "apps-aud", "owner@example.com")


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


def test_old_quick_tunnels_to_an_apps_own_port_are_gone():
    import apps
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
    (tmp_path / "woltspace.json").write_text(json.dumps({"ways_in": {"cloudflare": {
        "team_domain": "t.cloudflareaccess.com", "apps_aud": "",
        "owner_email": "o@example.com"}}}))
    assert doctor._remote_apps_check(_layout(tmp_path)).status == "warn"
    (tmp_path / "woltspace.json").write_text(json.dumps({"ways_in": {"cloudflare": {
        "team_domain": "t.cloudflareaccess.com", "apps_aud": "b",
        "owner_email": "o@example.com"}}}))
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
    cloudflare = server_app.ways_in.named("cloudflare")
    visitor = asyncio.run(cloudflare.owner({"cf-access-jwt-assertion": signer.token(email=email)}, {}))
    assert (visitor.identity if visitor else None) == expected
    lodge_token = signer.token(aud="lodge-aud")
    assert asyncio.run(cloudflare.owner({"cf-access-jwt-assertion": lodge_token}, {})) is None


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
    commands = {}
    monkeypatch.setattr(apps, "process_command", lambda pid: commands.get(pid, ""))
    return SimpleNamespace(apps=apps, rows=rows, alive=alive, killed=killed, commands=commands)


def _quick(port):
    return f"cloudflared tunnel --url http://localhost:{port} --http-host-header localhost"


def test_old_quick_tunnels_are_retired_once_and_only_when_verified(tunnel_records, monkeypatch):
    t = tunnel_records
    monkeypatch.setattr(t.apps.time, "sleep", lambda s: None)
    t.rows.update({
        "live": {"name": "live", "port": 4001, "tunnel_pid": 101, "tunnel_url": "https://a.trycloudflare.com"},
        "reused": {"name": "reused", "port": 4002, "tunnel_pid": 202, "tunnel_url": "https://b.trycloudflare.com"},
        "stuck": {"name": "stuck", "port": 4003, "tunnel_pid": 999, "tunnel_url": "https://c.trycloudflare.com"},
        "plain": {"name": "plain", "port": 4004, "tunnel_pid": None, "tunnel_url": None},
    })
    t.alive.update({101, 999})  # 202's pid now belongs to something else
    t.commands.update({101: _quick(4001), 999: _quick(4003)})
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


@pytest.mark.parametrize("has_manifest", [False, True])
def test_a_tunnel_that_wont_stop_keeps_its_app_record_until_it_does(tunnel_records, monkeypatch,
                                                                     has_manifest, tmp_path):
    from woltspace import doctor
    t = tunnel_records
    monkeypatch.setattr(t.apps.time, "sleep", lambda s: None)
    cleared, started = [], []
    monkeypatch.setattr(t.apps, "_clear_state", lambda name: (cleared.append(name), t.rows.pop(name, None)))
    monkeypatch.setattr(t.apps, "get_app", lambda name: SimpleNamespace() if has_manifest else None)
    monkeypatch.setattr(t.apps, "_is_pid_alive", lambda pid: False)  # the app itself is down

    def start(name):
        started.append(name)
        t.rows[name] = {"name": name, "pid": 1, "port": 4000}
        return t.rows[name]

    monkeypatch.setattr(t.apps, "start_app", start)
    monkeypatch.setattr(t.apps, "intended_apps", lambda: [dict(v) for v in t.rows.values()])
    t.rows["blog"] = {"name": "blog", "pid": 5, "port": 4005, "tunnel_pid": 999,
                      "tunnel_url": "https://x.trycloudflare.com"}
    t.alive.add(999)  # 999 ignores the signal
    t.commands[999] = _quick(4005)

    actions = {a["name"] + ":" + a["action"] for a in t.apps.apps_restore()}
    assert actions == {"blog:tunnel-still-running", "blog:held-for-tunnel"}
    assert not cleared and not started and t.rows["blog"]["tunnel_pid"] == 999
    assert doctor._app_tunnels_check(_layout(tmp_path)).status == "fail"

    t.alive.discard(999)  # it finally stops
    actions = [a["action"] for a in t.apps.apps_restore()]
    assert actions[0] == "tunnel-retired"
    assert (cleared == ["blog"]) if not has_manifest else (started == ["blog"])
    assert doctor._app_tunnels_check(_layout(tmp_path)) is None



@pytest.mark.parametrize("command", [
    "cloudflared tunnel run --token abc",                          # the lodge's own named tunnel
    "cloudflared tunnel --url http://localhost:9999",              # another app's, or someone else's
    "cloudflared access tcp --hostname x",                         # not a quick tunnel at all
])
def test_a_reused_pid_running_another_cloudflared_is_never_signalled(tunnel_records, command):
    t = tunnel_records
    t.rows["blog"] = {"name": "blog", "port": 4005, "tunnel_pid": 303, "tunnel_url": "https://y.trycloudflare.com"}
    t.alive.add(303)
    t.commands[303] = command
    results = t.apps.retire_app_tunnels()
    assert results == [{"name": "blog", "action": "tunnel-pid-ambiguous", "pid": 303}]
    assert t.killed == [] and t.rows["blog"]["tunnel_pid"] == 303


def test_a_write_through_the_tunnel_without_an_origin_is_refused(lodge):
    before = len(_Seen.requests)
    assert _through_tunnel(f"board.{DOMAIN}").post(
        "/x", headers={"cf-access-jwt-assertion": "good"}).status_code == 403
    assert len(_Seen.requests) == before


def test_doctor_names_apps_on_the_reserved_site_prefix(tmp_path):
    from woltspace import doctor
    _manifest(tmp_path, "site-old", "site-old", "npm run dev")
    check = doctor._apps_check(_layout(tmp_path))
    assert check.status == "warn" and "site-old" in check.detail and "reserved" in check.detail


def test_a_lodge_under_its_own_name_has_its_content_there(monkeypatch):
    from server import ways_in
    way = ways_in.CloudflareAccess()
    tunnel = way._tunnel()
    monkeypatch.setattr(tunnel, "_tunnel_hostname", "lodge.owner.woltspace.test")
    monkeypatch.setattr(tunnel, "_tunnel_domain", "owner.woltspace.test")
    assert way.lodge_hosts() >= {"lodge.owner.woltspace.test"}
    assert way.content_host("lodge.owner.woltspace.test") is None
    assert way.content_host("deck.owner.woltspace.test") == ways_in.PublicHost("app", "deck", "deck.owner.woltspace.test")
    assert way.content_host("site-softie.owner.woltspace.test") == ways_in.PublicHost(
        "site", "softie", "site-softie.owner.woltspace.test")
    assert way.content_host("deck.woltspace.test") is None
    assert way.content_host("a.deck.owner.woltspace.test") is None
    assert way.public_base("lodge.owner.woltspace.test", "app", "deck") == "https://deck.owner.woltspace.test"


@pytest.mark.parametrize("raw", [
    {"team_domain": "t.cloudflareaccess.com", "apps_aud": "b"},          # no owner_email
    {"team_domain": "t", "apps_aud": "b", "owner_email": "o@example.com"},
    {"team_domain": "t.cloudflareaccess.com", "apps_aud": "b", "owner_email": "nope"},
    {"team_domain": "t.cloudflareaccess.com", "apps_aud": "", "owner_email": "o@example.com"},
    {"team_domain": "t.cloudflareaccess.com", "apps_aud": "b", "owner_email": "o@example.com"},
    {"team_domain": "t.cloudflareaccess.com", "lodge_aud": "a", "apps_aud": "b", "owner_email": "o@example.com"},
    {"team_domain": "t.cloudflareaccess.com", "apps_aud": "b", "owner_email": "o@example.com", "other": "x"},
])
def test_doctor_agrees_with_the_lodge_on_access_settings(raw):
    from woltspace import doctor
    try:
        access.validate_access_settings(raw)
        lodge_accepts = True
    except ValueError:
        lodge_accepts = False
    assert (doctor._access_settings_problem(raw) == "") is lodge_accepts


def test_doctor_finds_a_native_lodges_tunnel_in_its_env_file(tmp_path, monkeypatch):
    from woltspace import doctor
    monkeypatch.delenv("CLOUDFLARE_TUNNEL_URL", raising=False)
    assert doctor._remote_apps_check(_layout(tmp_path)) is None
    (tmp_path / ".env").write_text("CLOUDFLARE_TUNNEL_URL=https://owner.woltspace.test\n")
    check = doctor._remote_apps_check(_layout(tmp_path))
    assert check is not None and check.status == "warn"


# --- Sharing an app: an explicit opt-in --------------------------------------------------------

import app_sharing  # noqa: E402

TUNNEL = "https://quiet-otter.trycloudflare.com"


class _Tunnels:
    """Fake cloudflared: `start` hands out pids, `stop` records them."""

    def __init__(self, url=TUNNEL, alive=False):
        self.url, self.alive, self.started, self.stopped, self.next_pid = url, alive, [], [], 100

    def start(self, **kw):
        self.next_pid += 1
        self.started.append((self.next_pid, kw["host_header"]))
        return {"url": self.url, "pid": self.next_pid}

    def stop(self, pid):
        self.stopped.append(pid)

    def is_ours(self, pid, name, port, token):
        return bool(pid) and self.alive and pid not in self.stopped


def _share(root, name="board", tunnels=None, **kw):
    tunnels = tunnels or _Tunnels()
    return app_sharing.share(root, name, 7777, by="owner", start=tunnels.start, stop=tunnels.stop,
                             is_ours=tunnels.is_ours, **kw)


def _via_tunnel(record, name="board", **headers) -> TestClient:
    # What cloudflared sends: the share host it was given, from loopback. No
    # forwarding headers here on purpose: the share host alone makes it public.
    host = app_sharing.share_host(name, record["token"], 7777)
    return TestClient(server_app.app, base_url=f"http://{host}", client=("127.0.0.1", 50000), headers=headers)


@pytest.mark.parametrize("auth", [False, True])
def test_a_shared_app_opens_for_anyone_through_its_share_host(lodge, monkeypatch, auth):
    import lodge_auth
    if auth:
        monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    record = _share(lodge)
    key = lodge_auth.ensure_keys(lodge)[lodge_auth.OWNER]
    response = _via_tunnel(record).get("/", headers={"cookie": "app=1", "x-woltspace-key": key})
    assert response.status_code == 200 and response.text == "board says hi"
    _method, _path, sent = _Seen.requests[-1]
    assert sent["cookie"] == "app=1" and "x-woltspace-key" not in sent
    assert mill_server.IDENTITY_HEADER not in sent and "share.localhost" not in sent["host"]


@pytest.mark.parametrize("origin,ok", [
    (TUNNEL, True),
    (None, False),                                  # a write must say where it's from
    ("http://board.localhost:7777", False),          # the internal local form is no credential
    (TUNNEL + "/", False),                          # exact, not normalised
    ("https://evil.example", False),
    ("null", False),
])
def test_a_shared_apps_writes_must_come_from_its_public_page(lodge, origin, ok):
    record = _share(lodge)
    headers = {"origin": origin} if origin else {}
    response = _via_tunnel(record).post("/x", headers=headers)
    assert (response.status_code == 200) is ok
    cross = _via_tunnel(record).post("/x", headers={"origin": TUNNEL, "sec-fetch-site": "cross-site"})
    assert cross.status_code == 403


def test_unshare_and_re_share_end_the_old_link(lodge):
    tunnels = _Tunnels()
    old = _share(lodge, tunnels=tunnels)
    assert app_sharing.unshare(lodge, "board", 7777, by="owner", stop=tunnels.stop, is_ours=tunnels.is_ours)
    assert _via_tunnel(old).get("/").status_code == 404
    new = _share(lodge, tunnels=tunnels)
    assert new["token"] != old["token"]
    assert _via_tunnel(old).get("/").status_code == 404      # a surviving old tunnel reaches nothing
    assert _via_tunnel(new).get("/").status_code == 200
    assert app_sharing.current_share(lodge, "board", old["token"]) is None   # its sockets close too


@pytest.mark.parametrize("host", [
    "board--" + "0" * 24 + ".share.localhost:7777",   # a token that was never shared
    "board--short.share.localhost:7777",
    "share.localhost:7777",
    "x.y.share.localhost.:7777",
])
def test_a_share_shaped_host_that_isnt_a_current_share_reaches_nothing(lodge, host):
    _share(lodge)
    client = TestClient(server_app.app, base_url=f"http://{host}", client=("127.0.0.1", 50000))
    for path in ("/", "/sessions", "/_woltspace/grant?code=x"):
        assert client.get(path).status_code == 404


def test_the_apps_local_address_is_never_public(lodge):
    _share(lodge)
    # Through anything but the share host, `<app>.localhost` is the owner's only.
    forwarded = _through_quick_tunnel_shaped()
    assert forwarded.get("/").status_code == 403


def _through_quick_tunnel_shaped():
    return TestClient(server_app.app, base_url="http://board.localhost:7777", client=("127.0.0.1", 50000),
                      headers={"cf-connecting-ip": "203.0.113.9"})


def test_a_share_host_is_never_the_owner_and_never_a_grant(lodge, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    record = _share(lodge)
    response = _via_tunnel(record).get("/_woltspace/grant?code=anything")
    assert response.text == "board says hi"       # the app's own page, not a lodge grant
    assert "set-cookie" not in {k.lower() for k in response.headers}


def test_a_wolts_site_and_the_lodge_are_never_shared(lodge, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    record = _share(lodge, name="softie")   # a share by a wolt's name reaches an app of that name, not its site
    response = _via_tunnel(record, name="softie").get("/")
    assert "softie's site" not in response.text and response.status_code == 404
    lodge_api = TestClient(server_app.app, base_url="http://localhost:7777",
                           client=("127.0.0.1", 50000), headers={"cf-connecting-ip": "203.0.113.9"})
    assert lodge_api.get("/sessions").status_code == 401


def test_the_mill_refuses_a_public_visitor_on_an_app_that_isnt_shared(lodge):
    import httpx
    transport = httpx.HTTPTransport(uds=str(server_app.mill_server.socket_path(lodge)))
    with httpx.Client(transport=transport) as mill:
        public = {"host": "board.localhost:7777", mill_server.IDENTITY_HEADER: mill_server.PUBLIC}
        assert mill.get("http://mill/", headers=public).status_code == 403
        _share(lodge)
        assert mill.get("http://mill/", headers=public).status_code == 200


def test_a_shared_app_on_your_domain_opens_without_an_access_token(lodge):
    assert _through_tunnel(f"board.{DOMAIN}").get("/").status_code == 403
    _share(lodge, domain_url=f"https://board.{DOMAIN}")
    response = _through_tunnel(f"board.{DOMAIN}").get("/")
    assert response.status_code == 200 and response.text == "board says hi"
    assert _through_tunnel(f"board.{DOMAIN}").post("/x", headers={"origin": "https://evil.example"}).status_code == 403
    assert _through_tunnel(f"board.{DOMAIN}").post("/x").status_code == 403


def test_a_wolt_can_share_when_asked_and_every_share_is_logged(lodge, monkeypatch):
    import lodge_auth
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    keys = lodge_auth.ensure_keys(lodge)
    tunnels = _Tunnels()
    monkeypatch.setattr(app_sharing, "start_cloudflared", tunnels.start)
    monkeypatch.setattr(app_sharing, "stop_cloudflared", tunnels.stop)
    wolt = _local()
    wolt.headers["x-woltspace-key"] = keys[lodge_auth.WOLT]
    shared = wolt.post("/apps/board/share", json={"said_by": "softie"}).json()
    assert shared["tunnel_url"] == TUNNEL and "Anyone with this link" in shared["warning"]
    assert shared["by"] == lodge_auth.WOLT and shared["said_by"] == "softie" and "token" not in shared
    assert tunnels.started[0][1].startswith("board--") and tunnels.started[0][1].endswith(".share.localhost:7777")
    assert wolt.post("/apps/unshare-all").json()["unshared"] == ["board"]
    log = app_sharing.log_path(lodge)
    assert [json.loads(line)["event"] for line in log.read_text().splitlines()] == [
        "share-requested", "shared", "unshare"]
    assert log.stat().st_mode & 0o777 == 0o600


def test_no_share_goes_live_unlogged(lodge, monkeypatch):
    tunnels = _Tunnels(alive=True)
    app_sharing.log_path(lodge).parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(app_sharing, "log_path", lambda root: Path("/dev/null/nope/sharing.log"))
    with pytest.raises(RuntimeError, match="nothing shared"):
        _share(lodge, tunnels=tunnels)
    assert app_sharing.shared(lodge, "board") is None and tunnels.stopped == [101]
    assert app_sharing._read(lodge)["launching"] == []


def test_a_failed_save_after_the_tunnel_started_stops_it(lodge, monkeypatch):
    tunnels = _Tunnels(alive=True)
    real_write, writes = app_sharing._write, []

    def flaky(root, data):
        writes.append(1)
        if len(writes) == 2:          # the publishing write
            raise OSError("disk full")
        real_write(root, data)
    monkeypatch.setattr(app_sharing, "_write", flaky)
    with pytest.raises(RuntimeError, match="nothing shared"):
        _share(lodge, tunnels=tunnels)
    assert app_sharing.shared(lodge, "board") is None and tunnels.stopped == [101]


def test_a_tunnel_that_wont_stop_stays_tracked_and_reaches_nothing(lodge):
    class Stuck(_Tunnels):
        def is_ours(self, pid, name, port, token):
            return bool(pid)      # never stops
    tunnels = Stuck()
    old = _share(lodge, tunnels=tunnels)
    app_sharing.unshare(lodge, "board", 7777, by="owner", stop=tunnels.stop, is_ours=tunnels.is_ours)
    assert app_sharing.stuck_tunnels(lodge, 7777, is_ours=tunnels.is_ours) == [
        {**app_sharing._read(lodge)["retired"][0]}]
    new = _share(lodge, tunnels=tunnels)
    assert _via_tunnel(old).get("/").status_code == 404 and _via_tunnel(new).get("/").status_code == 200


def test_a_launch_the_sweep_expired_is_never_published(lodge):
    tunnels = _Tunnels(alive=True)

    def slow_start(**kw):
        result = tunnels.start(**kw)
        with app_sharing._locked(lodge):     # as the sweep does once the grace has passed
            data = app_sharing._read(lodge)
            data["launching"] = []
            app_sharing._write(lodge, data)
        return result
    with pytest.raises(RuntimeError, match="too long"):
        app_sharing.share(lodge, "board", 7777, by="owner", start=slow_start, stop=tunnels.stop,
                          is_ours=tunnels.is_ours)
    assert app_sharing.shared(lodge, "board") is None and tunnels.stopped == [101]


def test_restore_never_overwrites_a_newer_share(lodge):
    tunnels = _Tunnels(alive=True)
    first = _share(lodge, tunnels=tunnels)
    with app_sharing._locked(lodge):     # its tunnel is gone after a reboot
        data = app_sharing._read(lodge)
        data["shares"]["board"]["tunnel_pid"] = None
        app_sharing._write(lodge, data)

    def start_while_reshared(**kw):
        result = tunnels.start(**kw)
        app_sharing.unshare(lodge, "board", 7777, by="owner", stop=tunnels.stop, is_ours=tunnels.is_ours)
        _share(lodge, tunnels=tunnels)
        return result
    actions = app_sharing.restore(lodge, 7777, start=start_while_reshared, stop=tunnels.stop,
                                  is_ours=tunnels.is_ours, processes=lambda: [])
    current = app_sharing.shared(lodge, "board")
    assert [a["action"] for a in actions] == ["superseded"]
    assert current["token"] != first["token"] and current["tunnel_pid"] == 103
    assert 102 in tunnels.stopped        # restore's stale tunnel


def test_restore_keeps_a_live_tunnel_and_restarts_a_dead_one(tmp_path):
    tunnels = _Tunnels(alive=True)
    _share(tmp_path, "board", tunnels=tunnels)
    _share(tmp_path, "notes", tunnels=tunnels)
    tunnels.stopped.append(102)          # notes' tunnel died
    actions = app_sharing.restore(tmp_path, 7777, start=tunnels.start, stop=tunnels.stop,
                                  is_ours=tunnels.is_ours, processes=lambda: [])
    assert {a["name"]: a["action"] for a in actions} == {"board": "kept", "notes": "restarted"}
    assert app_sharing.shared(tmp_path, "notes")["tunnel_pid"] == 103
    events = [json.loads(line)["event"] for line in app_sharing.log_path(tmp_path).read_text().splitlines()]
    assert events.count("live-at-boot") == 2
    assert (app_sharing.sharing_dir(tmp_path).stat().st_mode & 0o777) == 0o700


def test_the_sweep_stops_only_this_lodges_untracked_share_tunnels(tmp_path):
    tunnels = _Tunnels(alive=True)
    live = _share(tmp_path, tunnels=tunnels)
    with app_sharing._locked(tmp_path):
        data = app_sharing._read(tmp_path)
        data["launching"].append(app_sharing._new_launch("notes", "a" * 24))
        app_sharing._write(tmp_path, data)
    stale = "b" * 24

    def cmd(host, port=7777):
        return f"/usr/bin/cloudflared tunnel --url http://localhost:{port} --http-host-header {host}"
    processes = [
        (live["tunnel_pid"], cmd(app_sharing.share_host("board", live["token"], 7777))),  # recorded: kept
        (6, cmd(app_sharing.share_host("board", live["token"], 7777))),   # current token, unrecorded pid
        (2, cmd(app_sharing.share_host("notes", "a" * 24, 7777))),         # launching: kept
        (3, cmd(app_sharing.share_host("board", stale, 7777))),            # orphan: stopped
        (4, cmd(app_sharing.share_host("board", stale, 7788), 7788)),     # another lodge: kept
        (5, "/usr/bin/cloudflared tunnel run --token x"),                  # the lodge's own tunnel
    ]
    stopped = app_sharing.sweep_orphans(tmp_path, 7777, processes=lambda: processes, stop=tunnels.stop,
                                        is_ours=lambda pid, *a: pid not in tunnels.stopped)
    assert sorted(stopped) == [3, 6]


def test_an_unreadable_sharing_state_is_kept_aside_and_shares_fail_closed(tmp_path):
    app_sharing.sharing_dir(tmp_path).mkdir(parents=True)
    (app_sharing.sharing_dir(tmp_path) / "shared.json").write_text("{not json")
    assert app_sharing.shared(tmp_path, "board") is None
    _share(tmp_path)
    assert len(app_sharing.corrupt_files(tmp_path)) == 1
    assert app_sharing.corrupt_files(tmp_path)[0].read_text() == "{not json"


def test_only_the_shares_own_tunnel_is_ever_stopped():
    token = "c" * 24
    ours = f"cloudflared tunnel --url http://localhost:7777 --http-host-header board--{token}.share.localhost:7777"
    check = lambda command, alive=True: app_sharing.is_sharing_tunnel(  # noqa: E731
        7, "board", 7777, token, command=lambda pid: command, cloudflared=lambda pid: alive)
    assert check(ours)
    for other in ("cloudflared tunnel run --token x",
                  "cloudflared tunnel --url http://localhost:4001",
                  ours.replace("board--", "notes--"),
                  ours.replace(token, "d" * 24),
                  ours.replace("localhost:7777 --", "localhost:7788 --")):
        assert not check(other)
    assert not check(ours, alive=False)


def test_doctor_lists_every_shared_app_and_stuck_tunnel(tmp_path, monkeypatch):
    from woltspace import doctor
    assert doctor._shared_apps_check(_layout(tmp_path)) is None
    _share(tmp_path)
    check = doctor._shared_apps_check(_layout(tmp_path))
    assert check.status == "warn" and "PUBLIC" in check.detail and "board" in check.detail
    assert TUNNEL in check.detail and "woltspace app unshare" in check.remedy
    monkeypatch.setattr(app_sharing, "stuck_tunnels", lambda root, port: [{"name": "old", "pid": 9}])
    assert doctor._shared_apps_check(_layout(tmp_path)).status == "fail"


# --- Ways in: anyone can add their own ---------------------------------------------------------

class _TestNet:
    """A made-up way in, as a plugin would write one: a VPN whose proxy runs on
    this machine and names the visitor in one header."""
    name = "testnet"
    credential_headers = frozenset({"x-testnet-user"})

    def lodge_hosts(self):
        return {"lodge.testnet.example"}

    def content_host(self, host):
        from server.ways_in import PublicHost
        if host == "board.testnet.example":
            return PublicHost("app", "board", host)
        return None

    async def owner(self, headers, scope):
        from server.ways_in import Visitor
        from_proxy = (scope.get("client") or ("", 0))[0] == "127.0.0.1"
        if from_proxy and headers.get("x-testnet-user") == "owner@example.com":
            return Visitor("owner", time.time() + 600)
        return None

    def public_base(self, lodge_host, kind, name):
        return f"https://{name}.testnet.example" if lodge_host == "lodge.testnet.example" else None


@pytest.fixture
def testnet():
    from server import ways_in
    way = _TestNet()
    ways_in.register(way)
    yield way
    ways_in._registered.remove(way)


def _via_testnet(host="board.testnet.example", **headers):
    return TestClient(server_app.app, base_url=f"https://{host}", client=("127.0.0.1", 50000), headers=headers)


def test_a_new_way_in_opens_an_app_for_the_owner_it_proves(lodge, testnet):
    response = _via_testnet(**{"x-testnet-user": "owner@example.com"}).get("/")
    assert response.status_code == 200 and response.text == "board says hi"
    _method, _path, sent = _Seen.requests[-1]
    assert "x-testnet-user" not in sent and mill_server.IDENTITY_HEADER not in sent
    assert _via_testnet(**{"x-testnet-user": "someone@example.com"}).get("/").status_code == 403
    assert _via_testnet().get("/").status_code == 403


def test_a_new_way_ins_writes_must_come_from_its_public_page(lodge, testnet):
    owner = {"x-testnet-user": "owner@example.com"}
    assert _via_testnet(**owner).post("/x", headers={"origin": "https://board.testnet.example"}).status_code == 200
    assert _via_testnet(**owner).post("/x", headers={"origin": "https://evil.example"}).status_code == 403


def test_a_way_ins_identity_header_is_never_forwarded_to_an_app(lodge, testnet):
    # Forged on a local request, it's dropped all the same.
    _local("board.localhost:7777").get("/", headers={"x-testnet-user": "owner@example.com"})
    _method, _path, sent = _Seen.requests[-1]
    assert "x-testnet-user" not in sent


def test_a_new_way_in_names_the_lodge_and_where_its_browsers_open_apps(lodge, testnet):
    client = _via_testnet("lodge.testnet.example")
    assert client.get("/version").status_code == 200
    opened = client.get("/apps/open?app=board&path=/x", follow_redirects=False)
    assert opened.headers["location"] == "https://board.testnet.example/x"


@pytest.mark.parametrize("target", ["class", "factory", "instance"])
def test_an_installed_way_in_may_be_a_class_a_factory_or_an_instance(monkeypatch, target):
    from server import ways_in

    class Entry:
        name = "testnet"

        def load(self):
            return {"class": _TestNet, "factory": lambda: _TestNet(), "instance": _TestNet()}[target]
    monkeypatch.setattr("importlib.metadata.entry_points", lambda group: [Entry()])
    monkeypatch.setattr(ways_in, "_installed", None)
    loaded = [way for way in ways_in.all_ways() if way.name == "testnet"]
    assert len(loaded) == 1 and not isinstance(loaded[0], type)
    assert ways_in.lodge_hosts() >= {"lodge.testnet.example"}
    monkeypatch.setattr(ways_in, "_installed", None)


def test_a_way_in_that_isnt_one_is_refused(monkeypatch):
    from server import ways_in
    with pytest.raises(TypeError):
        ways_in.register(_TestNet)          # the class, not a way in
    with pytest.raises(TypeError):
        ways_in.register(object())


def test_ways_in_load_from_installed_packages_and_a_broken_one_is_skipped(monkeypatch):
    from server import ways_in

    class Entry:
        def __init__(self, name, target):
            self.name, self.target = name, target

        def load(self):
            if isinstance(self.target, Exception):
                raise self.target
            return self.target
    monkeypatch.setattr("importlib.metadata.entry_points",
                        lambda group: [Entry("testnet", _TestNet), Entry("broken", ImportError("nope"))])
    monkeypatch.setattr(ways_in, "_installed", None)
    names = [way.name for way in ways_in.all_ways()]
    assert names == ["cloudflare", "testnet"]
    monkeypatch.setattr(ways_in, "_installed", None)


@pytest.mark.parametrize("proof", [
    ("public", lambda: time.time() + 600),
    ("owenr", lambda: time.time() + 600),
    ("owner", lambda: time.time() - 1),           # already expired
    ("owner", lambda: float("nan")),
    ("owner", lambda: "soon"),
])
def test_a_way_in_can_only_ever_prove_the_owner(lodge, testnet, monkeypatch, proof):
    from server.ways_in import PublicHost, Visitor
    identity, expires = proof

    async def owner(headers, scope):
        return Visitor(identity, expires())
    monkeypatch.setattr(testnet, "owner", owner)
    monkeypatch.setattr(testnet, "content_host", lambda host: {
        "board.testnet.example": PublicHost("app", "board", host),
        "softie.testnet.example": PublicHost("site", "softie", host)}.get(host))
    _remote_sites_on(lodge)
    assert _via_testnet().get("/").status_code == 403
    assert _via_testnet("softie.testnet.example").get("/").status_code == 403


def test_a_way_in_that_raises_lets_nobody_in(lodge, testnet, monkeypatch):
    async def owner(headers, scope):
        raise RuntimeError("plugin bug")
    monkeypatch.setattr(testnet, "owner", owner)
    assert _via_testnet(**{"x-testnet-user": "owner@example.com"}).get("/").status_code == 403


def test_an_orphan_that_wont_stop_becomes_tracked_debt(tmp_path, monkeypatch):
    stale = "e" * 24
    command = (f"/usr/bin/cloudflared tunnel --url http://localhost:7777 "
               f"--http-host-header {app_sharing.share_host('board', stale, 7777)}")
    monkeypatch.setattr(app_sharing, "STOP_WAIT_SECONDS", 0.2)
    always = lambda pid, *a: True  # noqa: E731
    stopped = app_sharing.sweep_orphans(tmp_path, 7777, processes=lambda: [(9, command)],
                                        stop=lambda pid: None, is_ours=always)
    assert stopped == []
    assert [r["pid"] for r in app_sharing.stuck_tunnels(tmp_path, 7777, is_ours=always)] == [9]


def test_a_short_write_still_logs_the_whole_line(tmp_path, monkeypatch):
    real_write = app_sharing.os.write
    calls = []

    def short(fd, data):
        calls.append(1)
        return real_write(fd, bytes(data[:5]) if len(calls) == 1 else data)
    monkeypatch.setattr(app_sharing.os, "write", short)
    app_sharing._log(tmp_path, "share-requested", "board")
    assert json.loads(app_sharing.log_path(tmp_path).read_text())["event"] == "share-requested"
    monkeypatch.setattr(app_sharing.os, "write", lambda fd, data: 0)
    with pytest.raises(RuntimeError, match="nothing shared"):
        _share(tmp_path)
    assert app_sharing.shared(tmp_path, "board") is None


@pytest.mark.parametrize("revoke", ["one", "all"])
def test_revoking_cancels_a_share_whose_tunnel_is_still_starting(lodge, revoke):
    tunnels = _Tunnels(alive=True)
    seen = {}

    def start_then_revoke(**kw):
        result = tunnels.start(**kw)
        if revoke == "one":
            seen["revoked"] = app_sharing.unshare(lodge, "board", 7777, by="owner",
                                                  stop=tunnels.stop, is_ours=tunnels.is_ours)
        else:
            seen["revoked"] = app_sharing.unshare_all(lodge, 7777, by="owner",
                                                      stop=tunnels.stop, is_ours=tunnels.is_ours)
        return result
    with pytest.raises(RuntimeError, match="cancelled"):
        app_sharing.share(lodge, "board", 7777, by="owner", start=start_then_revoke,
                          stop=tunnels.stop, is_ours=tunnels.is_ours)
    assert seen["revoked"] in (True, ["board"])
    assert app_sharing.shared(lodge, "board") is None and 101 in tunnels.stopped


def test_restore_that_cant_record_its_new_tunnel_stops_it(lodge, monkeypatch):
    tunnels = _Tunnels(alive=True)
    _share(lodge, tunnels=tunnels)
    tunnels.stopped.append(101)            # the old tunnel died
    real_write, writes = app_sharing._write, []

    def flaky(root, data):
        if (data["shares"].get("board") or {}).get("tunnel_pid") == 102:   # restore publishing it
            raise OSError("disk full")
        real_write(root, data)
    monkeypatch.setattr(app_sharing, "_write", flaky)
    actions = app_sharing.restore(lodge, 7777, start=tunnels.start, stop=tunnels.stop,
                                  is_ours=tunnels.is_ours, processes=lambda: [])
    assert [a["action"] for a in actions] == ["failed"] and 102 in tunnels.stopped


def test_a_crash_between_restarts_spawn_and_record_is_swept_next_boot(lodge):
    tunnels = _Tunnels(alive=True)
    live = _share(lodge, tunnels=tunnels)
    # A tunnel for the current token whose pid was never recorded.
    ghost = (77, "/usr/bin/cloudflared tunnel --url http://localhost:7777 --http-host-header "
                 + app_sharing.share_host("board", live["token"], 7777))
    stopped = app_sharing.sweep_orphans(lodge, 7777, processes=lambda: [ghost], stop=tunnels.stop,
                                        is_ours=lambda pid, *a: pid not in tunnels.stopped)
    assert stopped == [77] and app_sharing.shared(lodge, "board")["tunnel_pid"] == 101


@pytest.mark.parametrize("state", [
    {"shares": {}, "retired": "broken", "launching": []},
    {"shares": {"board": {"token": "nope"}}, "retired": [], "launching": []},
    {"shares": {}, "retired": [{"name": "board", "token": "a" * 24}], "launching": []},   # no pid
    {"shares": {}, "retired": [], "launching": [], "extra": 1},
])
def test_an_invalid_state_is_quarantined_not_normalised(tmp_path, state):
    app_sharing.sharing_dir(tmp_path).mkdir(parents=True)
    (app_sharing.sharing_dir(tmp_path) / "shared.json").write_text(json.dumps(state))
    assert app_sharing.state_problem(tmp_path)          # doctor sees it before anything moves it
    _share(tmp_path)
    _share(tmp_path, name="notes")                     # a second quarantine never overwrites the first
    assert len(app_sharing.corrupt_files(tmp_path)) == 1
    assert json.loads(app_sharing.corrupt_files(tmp_path)[0].read_text()) == state
    assert app_sharing.state_problem(tmp_path) is None


def test_two_quarantines_in_one_second_keep_both(tmp_path):
    for _ in range(2):
        app_sharing.sharing_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        (app_sharing.sharing_dir(tmp_path) / "shared.json").write_text("{bad")
        _share(tmp_path)
        app_sharing.unshare(tmp_path, "board", 7777, by="owner", stop=lambda pid: None)
    assert len(app_sharing.corrupt_files(tmp_path)) == 2


def test_a_way_ins_identity_header_never_reaches_a_tool(lodge, testnet, monkeypatch):
    seen = {}

    class Response:
        status_code, headers, content = 200, {}, b"ok"

    async def fake_send(self, request, **kw):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        import httpx
        return httpx.Response(200, content=b"ok")
    import httpx
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    monkeypatch.setattr(server_app.tool_registry, "get", lambda name: {"name": name, "port": 4999}, raising=False)
    _local().get("/tools/t/x", headers={"x-testnet-user": "owner@example.com", "x-other": "1"})
    assert seen.get("x-other") == "1" and "x-testnet-user" not in seen


def test_a_publish_that_fails_after_its_replace_is_taken_back(lodge, monkeypatch):
    tunnels = _Tunnels(alive=True)
    real_fsync_dir, calls = app_sharing._fsync_dir, []

    def flaky(directory):
        calls.append(1)
        state = app_sharing._read(lodge)
        if "board" in state["shares"] and len([c for c in calls]) and not getattr(flaky, "failed", False):
            flaky.failed = True
            raise OSError("fsync failed")          # the share is on disk, its sync isn't
        real_fsync_dir(directory)
    monkeypatch.setattr(app_sharing, "_fsync_dir", flaky)
    with pytest.raises(RuntimeError, match="nothing shared"):
        _share(lodge, tunnels=tunnels, domain_url=f"https://board.{DOMAIN}")
    assert app_sharing.shared(lodge, "board") is None and 101 in tunnels.stopped
    assert _through_tunnel(f"board.{DOMAIN}").get("/").status_code == 403


def test_a_publish_that_cant_be_taken_back_says_it_is_live(lodge, monkeypatch):
    tunnels = _Tunnels(alive=True)
    real_write = app_sharing._write

    def stuck(root, data):
        if "board" in data["shares"]:
            stuck.published = True
            real_write(root, data)
            raise OSError("disk trouble")          # it lands, then reports failure
        if getattr(stuck, "published", False):
            raise OSError("disk trouble")          # and the undo can't be saved at all
        real_write(root, data)
    monkeypatch.setattr(app_sharing, "_write", stuck)
    with pytest.raises(RuntimeError, match="is live"):
        _share(lodge, tunnels=tunnels)
    assert app_sharing.shared(lodge, "board") is not None   # what it says is what's on disk


def test_after_a_crash_the_old_lodges_launches_and_tunnels_are_swept(lodge):
    tunnels = _Tunnels(alive=True)
    live = _share(lodge, tunnels=tunnels)
    with app_sharing._locked(lodge):        # what a lodge that crashed mid-restart leaves
        data = app_sharing._read(lodge)
        data["launching"].append({**app_sharing._new_launch("board", live["token"]), "instance": "f" * 16})
        app_sharing._write(lodge, data)
    ghost = (77, "/usr/bin/cloudflared tunnel --url http://localhost:7777 --http-host-header "
                 + app_sharing.share_host("board", live["token"], 7777))
    stopped = app_sharing.sweep_orphans(lodge, 7777, processes=lambda: [ghost], stop=tunnels.stop,
                                        is_ours=lambda pid, *a: pid not in tunnels.stopped)
    assert stopped == [77] and app_sharing._read(lodge)["launching"] == []


@pytest.mark.parametrize("launch", [
    {"name": "board", "token": "a" * 24, "at": "broken", "instance": "f" * 16, "id": "0" * 16},
    {"name": "board", "token": "a" * 24, "at": 1.0, "instance": "f" * 16},                 # no id
    {"name": "Board!", "token": "a" * 24, "at": 1.0, "instance": "f" * 16, "id": "0" * 16},
])
def test_a_bad_launch_entry_is_an_invalid_state(tmp_path, launch):
    app_sharing.sharing_dir(tmp_path).mkdir(parents=True)
    (app_sharing.sharing_dir(tmp_path) / "shared.json").write_text(
        json.dumps({"shares": {}, "retired": [], "launching": [launch]}))
    assert app_sharing.state_problem(tmp_path)


def test_a_retired_entry_with_a_bad_token_is_an_invalid_state(tmp_path):
    app_sharing.sharing_dir(tmp_path).mkdir(parents=True)
    (app_sharing.sharing_dir(tmp_path) / "shared.json").write_text(json.dumps(
        {"shares": {}, "retired": [{"name": "board", "pid": 5, "token": "zz"}], "launching": []}))
    assert app_sharing.state_problem(tmp_path)


def test_if_way_in_headers_cant_be_known_nothing_reaches_a_tool(lodge, monkeypatch):
    import httpx
    sent = []

    async def fake_send(self, request, **kw):
        sent.append(request)
        return httpx.Response(200, content=b"ok")
    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    monkeypatch.setattr(server_app.tool_registry, "get", lambda name: {"name": name, "port": 4999}, raising=False)

    def broken():
        raise RuntimeError("registry broken")
    monkeypatch.setattr(server_app.ways_in, "credential_headers", broken)
    response = TestClient(server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000),
                          raise_server_exceptions=False).get("/tools/t/x")
    assert response.status_code >= 500 and sent == []


def test_a_partial_log_line_never_joins_the_next(tmp_path):
    path = app_sharing.log_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"event": "share-req')          # a write that stopped part way
    app_sharing._log(tmp_path, "unshare", "board")
    assert json.loads(path.read_text().splitlines()[-1])["event"] == "unshare"


def test_cloudflare_settings_live_under_their_own_name(tmp_path):
    cloudflare = {"team_domain": "t.cloudflareaccess.com", "apps_aud": "b",
                  "owner_email": "o@example.com"}
    for config, found in (({"ways_in": {"cloudflare": cloudflare}}, True),
                          ({"ways_in": {"another": {"anything": 1}}}, False),
                          ({}, False)):
        (tmp_path / "woltspace.json").write_text(json.dumps(config))
        access._CONFIG_CACHE.clear()
        assert (access.load_access_settings(tmp_path) is not None) is found


# --- The lodge itself, with lodge auth on ------------------------------------------------------

@pytest.fixture
def lodge_auth_on(lodge, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    return lodge


def test_with_lodge_auth_on_the_owners_login_opens_the_lodge(lodge_auth_on):
    client = _through_tunnel(LODGE_HOST)
    assert client.get("/tunnel", headers={"cf-access-jwt-assertion": "good"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {}, {"cf-access-jwt-assertion": "forged"}, {"cf-access-jwt-assertion": "stranger"},
])
def test_with_lodge_auth_on_nobody_else_opens_the_lodge(lodge_auth_on, headers):
    assert _through_tunnel(LODGE_HOST).get("/tunnel", headers=headers).status_code == 401


def test_the_owners_login_opens_sites_and_apps_from_the_lodge(lodge_auth_on):
    _remote_sites_on(lodge_auth_on)
    client = _through_tunnel(LODGE_HOST)
    login = {"cf-access-jwt-assertion": "good"}
    app = client.get("/apps/open?app=board&path=/x", headers=login, follow_redirects=False)
    assert app.status_code == 303 and app.headers["location"] == f"https://board.{DOMAIN}/x"
    site = client.get("/sites/open?wolt=softie", headers=login, follow_redirects=False)
    assert site.status_code == 303 and site.headers["location"] == f"https://site-softie.{DOMAIN}/"


def test_a_wolts_key_still_cant_open_a_site_or_app(lodge_auth_on):
    import lodge_auth
    key = lodge_auth.ensure_keys(lodge_auth_on)[lodge_auth.WOLT]
    client = TestClient(server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000))
    for path in ("/apps/open?app=board", "/sites/open?wolt=softie"):
        assert client.get(path, headers={lodge_auth.KEY_HEADER: key}, follow_redirects=False).status_code == 403


def test_a_name_sharing_cant_hold_says_so_and_changes_nothing(lodge):
    started = []
    with pytest.raises(RuntimeError, match="Rename it"):
        app_sharing.share(lodge, "my_app", 7777, by="owner", start=lambda *a: started.append(a))
    assert not started and not app_sharing.shared(lodge, "my_app")
    assert app_sharing.state_problem(lodge) is None


def test_an_app_link_in_a_frame_offers_a_link_and_spends_no_grant(lodge_auth_on):
    import lodge_auth
    key = lodge_auth.ensure_keys(lodge_auth_on)[lodge_auth.OWNER]
    response = _local().get("/app/board/", headers={"sec-fetch-dest": "iframe", lodge_auth.KEY_HEADER: key},
                            follow_redirects=False)
    assert response.status_code == 200 and "/apps/open?app=board" in response.text


def test_the_split_view_opens_apps_through_the_mill():
    root = Path(__file__).resolve().parent.parent
    assert "directPortUrl" not in (root / "templates" / "tui.html").read_text()


def test_only_the_lodges_own_pages_can_frame_it(lodge):
    response = _local().get("/health")
    assert response.headers["content-security-policy"] == "frame-ancestors 'self'"


def test_a_forgotten_browsers_socket_closes(lodge_auth_on, monkeypatch):
    import lodge_auth
    from starlette.websockets import WebSocketDisconnect
    monkeypatch.setattr(server_app, "SITE_SOCKET_RECHECK_SECONDS", 0.05)
    token = lodge_auth.redeem_pairing_code(lodge_auth_on, lodge_auth.new_pairing_code(lodge_auth_on))
    client = _local()
    headers = {"cookie": f"{lodge_auth.cookie_name('localhost:7777', False)}={token}",
               "origin": "http://localhost:7777"}
    with client.websocket_connect("ws://localhost:7777/livereload", headers=headers) as socket:
        lodge_auth.forget_browsers(lodge_auth_on)
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_doctor_names_a_wolt_whose_site_has_no_address(tmp_path):
    from woltspace.doctor import _wolt_sites_check
    from woltspace.layout import RuntimeLayout
    layout = RuntimeLayout.from_env({"WOLTSPACE_WOLTS_DIR": str(tmp_path)})
    (tmp_path / "fine" / "wolt" / "site").mkdir(parents=True)
    assert _wolt_sites_check(layout) is None
    (tmp_path / "my_wolt" / "wolt" / "site").mkdir(parents=True)
    assert "my_wolt" in _wolt_sites_check(layout).detail


def test_the_owners_login_opens_only_the_host_of_its_way_in(lodge_auth_on):
    client = TestClient(server_app.app, base_url="http://localhost:7777", client=("127.0.0.1", 50000))
    assert client.get("/tunnel", headers={"cf-access-jwt-assertion": "good"}).status_code == 401


@pytest.mark.parametrize("headers", [
    {},                                                 # a write always says where it came from
    {"origin": "https://evil.example"},
    {"origin": f"http://{LODGE_HOST}"},
])
def test_a_login_alone_cant_write_from_elsewhere(lodge_auth_on, headers):
    response = _through_tunnel(LODGE_HOST).post(
        "/current", json={"url": "/"}, headers={"cf-access-jwt-assertion": "good", **headers})
    assert response.status_code in (401, 403)


def test_the_owners_login_can_write_from_the_lodges_own_page(lodge_auth_on):
    response = _through_tunnel(LODGE_HOST).post(
        "/current", json={"url": "/"},
        headers={"cf-access-jwt-assertion": "good", "origin": f"https://{LODGE_HOST}"})
    assert response.status_code not in (401, 403)


def test_a_socket_opens_only_from_the_lodges_own_page(lodge_auth_on):
    from starlette.websockets import WebSocketDisconnect
    client = _through_tunnel(LODGE_HOST)
    with client.websocket_connect(f"wss://{LODGE_HOST}/livereload", headers={
            "cf-access-jwt-assertion": "good", "origin": f"https://{LODGE_HOST}"}):
        pass
    for origin in ("https://evil.example", None):
        headers = {"cf-access-jwt-assertion": "good"}
        if origin:
            headers["origin"] = origin
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"wss://{LODGE_HOST}/livereload", headers=headers):
                pass


def test_a_socket_closes_when_the_login_ends(lodge_auth_on, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    async def verify(token, settings, audience):
        return {"email": "owner@example.com", "exp": time.time() + 0.3}

    monkeypatch.setattr(access.access_token_verifier, "verify", verify)
    client = _through_tunnel(LODGE_HOST)
    with client.websocket_connect(f"wss://{LODGE_HOST}/livereload", headers={
            "cf-access-jwt-assertion": "good", "origin": f"https://{LODGE_HOST}"}) as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
    assert closed.value.code == 1008


def test_a_broken_way_in_opens_nothing(lodge_auth_on, monkeypatch):
    from server import ways_in

    async def broken(*_args):
        raise RuntimeError("no")
    monkeypatch.setattr(ways_in.named("cloudflare"), "owner", broken, raising=False)
    client = _through_tunnel(LODGE_HOST)
    assert client.get("/tunnel", headers={"cf-access-jwt-assertion": "good"}).status_code == 401
