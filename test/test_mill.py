"""The Mill: wolt sites on origins of their own, behind the lodge's one port."""

import asyncio
import os
import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_auth
import server.app as server_app
import server.mill as mill_server
from server.content_hosts import (
    SuffixNaming,
    anchored_site_root,
    find_wolt_dir,
    parse_site_host,
)

SITE = "softie.sites.localhost:7777"
OWNER = {mill_server.IDENTITY_HEADER: mill_server.OWNER}


@pytest.fixture
def wolts(tmp_path):
    site = tmp_path / "softie" / "wolt" / "site"
    (site / "notes").mkdir(parents=True)
    (site / "index.html").write_text("<html><body>home</body></html>")
    (site / "notes" / "index.html").write_text("<p>notes</p>")
    (site / "data.txt").write_text("0123456789")
    (tmp_path / "softie" / "wolt" / "memory").mkdir()
    (tmp_path / "softie" / "wolt" / "memory" / "secret.md").write_text("private")
    (site / "leak.md").symlink_to(tmp_path / "softie" / "wolt" / "memory" / "secret.md")
    return tmp_path


def _mill(wolts) -> TestClient:
    return TestClient(mill_server.Mill(wolts), base_url=f"http://{SITE}")


# --- Naming -------------------------------------------------------------------------

@pytest.mark.parametrize("host,label", [
    ("softie.sites.localhost:7777", "softie"),
    ("SOFTIE.sites.localhost", "softie"),
    ("softie.sites.localhost.:7777", "softie"),
    ("a-b2.sites.localhost:9", "a-b2"),
])
def test_site_hosts_name_a_wolt(host, label):
    assert parse_site_host(host).label == label


@pytest.mark.parametrize("host", [
    "localhost:7777", "softie.localhost:7777", "sites.localhost", ".sites.localhost",
    "a.b.sites.localhost", "-x.sites.localhost", "x_y.sites.localhost",
    "user@softie.sites.localhost", "softie.sites.localhost/x", "softie.sites.localhost:bad",
    "softie.sites.example.com",
])
def test_anything_else_is_not_a_site(host):
    assert parse_site_host(host) is None


def test_naming_is_a_list_each_way_in_adds_to():
    tailnet = SuffixNaming("tailscale", ".box.tailnet.ts.net")
    assert parse_site_host("softie.box.tailnet.ts.net", namings=(tailnet,)).way_in == "tailscale"
    assert parse_site_host("softie.box.tailnet.ts.net") is None


def test_a_site_root_is_the_wolts_own_real_folder(wolts, tmp_path):
    assert find_wolt_dir(wolts, "softie") == wolts / "softie"
    assert find_wolt_dir(wolts, "apps") is None
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    linked = wolts / "linky" / "wolt"
    linked.mkdir(parents=True)
    (linked / "site").symlink_to(elsewhere)
    assert anchored_site_root(wolts / "linky") is None


def test_two_wolts_differing_by_case_get_no_site(wolts):
    if (wolts / "SOFTIE").exists():
        pytest.skip("case-insensitive filesystem")
    (wolts / "Softie" / "wolt" / "site").mkdir(parents=True)
    assert find_wolt_dir(wolts, "softie") is None


# --- The Mill ------------------------------------------------------------------------

def test_the_owner_gets_files_with_the_site_policy(wolts):
    response = _mill(wolts).get("/data.txt", headers=OWNER)
    assert response.status_code == 200 and response.text == "0123456789"
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts allow-same-origin")
    for allowed in ("allow-forms", "allow-popups", "allow-modals", "allow-downloads"):
        assert allowed in csp
    assert response.headers["cache-control"] == "private, no-store"


def test_without_the_lodges_word_nobody_is_the_owner(wolts):
    assert _mill(wolts).get("/data.txt").status_code == 403
    forged = {mill_server.IDENTITY_HEADER: "admin"}
    assert _mill(wolts).get("/data.txt", headers=forged).status_code == 403


def test_owner_pages_get_the_reload_client(wolts):
    page = _mill(wolts).get("/", headers=OWNER).text
    assert "home" in page and "/_woltspace/livereload" in page


@pytest.mark.parametrize("path", [
    "/leak.md", "/%2e%2e/memory/secret.md", "/..%2fmemory%2fsecret.md", "/notes/../../memory/secret.md",
    "/_woltspace/anything", "/missing.html", "/data.txt%00",
])
def test_nothing_outside_the_site_is_served(wolts, path):
    response = _mill(wolts).get(path, headers=OWNER)
    assert response.status_code == 404
    assert "private" not in response.text


def test_a_folder_redirects_to_its_slash_and_serves_its_index(wolts):
    client = _mill(wolts)
    redirect = client.get("/notes", headers=OWNER, follow_redirects=False)
    assert redirect.status_code == 308 and redirect.headers["location"] == "/notes/"
    assert client.get("/notes/", headers=OWNER).text.startswith("<p>notes</p>")


def test_ranges_and_head(wolts):
    client = _mill(wolts)
    part = client.get("/data.txt", headers={**OWNER, "range": "bytes=2-4"})
    assert part.status_code == 206 and part.text == "234"
    head = client.head("/data.txt", headers=OWNER)
    assert head.status_code == 200 and head.headers["content-length"] == "10" and head.content == b""


def test_sites_are_read_only_and_refuse_service_workers(wolts):
    client = _mill(wolts)
    assert client.post("/data.txt", headers=OWNER).status_code == 405
    assert client.get("/sw.js", headers={**OWNER, "service-worker": "script"}).status_code == 403


def test_unknown_hosts_and_wolts_are_not_found(wolts):
    assert TestClient(mill_server.Mill(wolts), base_url="http://localhost:7777").get(
        "/", headers=OWNER).status_code == 404
    assert TestClient(mill_server.Mill(wolts), base_url="http://ghost.sites.localhost:7777").get(
        "/", headers=OWNER).status_code == 404


def test_the_reload_socket_is_the_owners_from_the_sites_own_page(wolts):
    client = _mill(wolts)
    with client.websocket_connect("/_woltspace/livereload",
                                  headers={**OWNER, "host": SITE, "origin": f"http://{SITE}"}) as ws:
        time.sleep(0.6)
        (wolts / "softie" / "wolt" / "site" / "data.txt").write_text("changed")
        assert '"reload"' in ws.receive_text()
    for headers in ({"host": SITE, "origin": f"http://{SITE}"},
                    {**OWNER, "host": SITE, "origin": "http://evil.sites.localhost:7777"},
                    {**OWNER, "host": SITE, "origin": "http://localhost:7777"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/_woltspace/livereload", headers=headers):
                pass


# --- Its socket -------------------------------------------------------------------------

def _short_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mill", dir="/tmp"))


def test_the_socket_folder_is_private_and_a_stale_socket_is_cleared():
    path = _short_dir() / "run" / "mill.sock"
    path.parent.mkdir(parents=True)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()  # bound, nobody listening: stale

    mill_server.prepare_socket(path)

    assert not path.exists()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_live_socket_is_never_taken_over():
    path = _short_dir() / "mill.sock"
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(path))
    live.listen(1)
    try:
        with pytest.raises(SystemExit):
            mill_server.prepare_socket(path)
        assert path.exists()
    finally:
        live.close()


def test_a_socket_path_too_long_is_refused_clearly():
    with pytest.raises(SystemExit) as stop:
        mill_server.prepare_socket(Path("/tmp") / ("x" * 120) / "mill.sock")
    assert "too long" in str(stop.value)


def test_the_mill_is_supervised_on_its_socket_by_the_entrypoint_only(tmp_path):
    from woltspace.channels import MillConnector
    from woltspace.layout import RuntimeLayout
    layout = RuntimeLayout.from_env({"WOLTSPACE_WOLTS_DIR": str(tmp_path)})
    assert not MillConnector().plan(layout, env={}).enabled
    plan = MillConnector().plan(layout, env={"WOLTSPACE_ENTRYPOINT": "1"})
    assert plan.enabled
    assert plan.command[-4:] == ("-m", "server.mill", "--socket",
                                 str(tmp_path / ".space" / "run" / "mill" / "mill.sock"))
    assert not any("--port" in part or "--host" in part for part in plan.command)


# --- Through the lodge's port -----------------------------------------------------------

@pytest.fixture
def running_mill(wolts, monkeypatch):
    """A real Mill on a unix socket, and the lodge pointed at it."""
    import uvicorn
    path = _short_dir() / "mill.sock"
    server = uvicorn.Server(uvicorn.Config(mill_server.Mill(wolts), uds=str(path),
                                           log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.05)
    monkeypatch.setattr(server_app, "WOLTS_DIR", wolts)
    monkeypatch.setattr(server_app.mill_server, "socket_path", lambda _root: path)
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    monkeypatch.setattr(server_app, "_pairing_failures", lodge_auth.FailureLimit())
    yield wolts
    server.should_exit = True
    thread.join(5)


def _lodge(host: str = SITE, client=("127.0.0.1", 50000)) -> TestClient:
    return TestClient(server_app.app, base_url=f"http://{host}", client=client)


def test_the_lodge_port_serves_a_site_through_the_mill(running_mill):
    response = _lodge().get("/data.txt")
    assert response.status_code == 200 and response.text == "0123456789"
    assert response.headers["content-security-policy"].startswith("sandbox allow-scripts allow-same-origin")
    assert _lodge("localhost:7777").get("/version").status_code == 200  # the lodge is still itself


def test_a_visitor_cannot_claim_to_be_the_owner(running_mill):
    tunnel = _lodge().get("/data.txt", headers={"x-forwarded-for": "203.0.113.9",
                                                mill_server.IDENTITY_HEADER: "owner"})
    assert tunnel.status_code == 403
    remote = _lodge(client=("203.0.113.9", 50000)).get("/data.txt",
                                                       headers={mill_server.IDENTITY_HEADER: "owner"})
    assert remote.status_code == 403


def test_only_safe_headers_go_to_the_mill():
    sent = server_app.MillForwardMiddleware._upstream_headers({
        "host": SITE, "cookie": "woltspace-lodge=t", "authorization": "Bearer k",
        "x-woltspace-key": "k", mill_server.IDENTITY_HEADER: "forged", "range": "bytes=0-1",
        "accept": "*/*", "cf-access-jwt-assertion": "jwt",
    })
    names = [name for name, _ in sent]
    assert names.count(mill_server.IDENTITY_HEADER) == 1
    assert dict(sent)[mill_server.IDENTITY_HEADER] == mill_server.OWNER
    assert "cookie" not in names and "authorization" not in names and "x-woltspace-key" not in names
    assert "cf-access-jwt-assertion" not in names
    assert ("range", "bytes=0-1") in sent


def test_two_hosts_naming_a_site_are_refused(running_mill):
    import asyncio
    sent = []

    async def run():
        scope = {"type": "http", "method": "GET", "path": "/version", "raw_path": b"/version",
                 "query_string": b"", "headers": [(b"host", SITE.encode()), (b"host", b"localhost:7777")],
                 "scheme": "http", "server": ("127.0.0.1", 7777), "client": ("127.0.0.1", 5),
                 "http_version": "1.1", "root_path": ""}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        await server_app.MillForwardMiddleware(server_app.app.router)(scope, receive, send)

    asyncio.run(run())
    assert sent[0]["status"] == 400


def test_the_reload_socket_works_through_the_lodge(running_mill):
    with _lodge().websocket_connect("/_woltspace/livereload",
                                    headers={"host": SITE, "origin": f"http://{SITE}"}) as ws:
        time.sleep(0.6)
        (running_mill / "softie" / "wolt" / "site" / "data.txt").write_text("changed")
        assert '"reload"' in ws.receive_text()
    with pytest.raises(WebSocketDisconnect):
        with _lodge().websocket_connect("/_woltspace/livereload",
                                        headers={"host": SITE, "origin": "http://localhost:7777"}):
            pass


def test_a_stopped_mill_leaves_the_lodge_up(wolts, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", wolts)
    monkeypatch.setattr(server_app.mill_server, "socket_path", lambda _root: _short_dir() / "none.sock")
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    assert _lodge().get("/data.txt").status_code == 502
    assert _lodge("localhost:7777").get("/version").status_code == 200


# --- With lodge auth on --------------------------------------------------------------------

@pytest.fixture
def locked(running_mill, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    return running_mill, lodge_auth.ensure_keys(running_mill)


def _paired(root) -> TestClient:
    client = _lodge("localhost:7777")
    client.get(f"/pair?code={lodge_auth.new_pairing_code(root)}")
    return client


def test_a_site_needs_its_own_grant_when_auth_is_on(locked):
    root, _ = locked
    refused = _lodge().get("/data.txt")
    assert refused.status_code == 401 and "/sites/open?wolt=softie" in refused.text

    lodge = _paired(root)
    opened = lodge.get("/sites/open?wolt=softie&path=/data.txt", follow_redirects=False)
    assert opened.status_code == 303
    grant_url = opened.headers["location"]
    assert grant_url.startswith("http://softie.sites.localhost:7777/_woltspace/grant?code=")

    site = _lodge()
    granted = site.get(grant_url.split("7777", 1)[1], follow_redirects=False)
    assert granted.status_code == 303 and granted.headers["location"] == "/data.txt"
    cookie = granted.headers["set-cookie"]
    assert cookie.startswith(f"{lodge_auth.SITE_COOKIE}=") and "HttpOnly" in cookie
    assert "Domain" not in cookie
    assert site.get("/data.txt").text == "0123456789"

    assert _lodge().get(grant_url.split("7777", 1)[1]).status_code == 403  # spent


def test_a_grant_opens_only_its_own_site(locked):
    root, _ = locked
    (root / "other" / "wolt" / "site").mkdir(parents=True)
    code = lodge_auth.new_site_grant(root, "softie")
    other = _lodge("other.sites.localhost:7777")
    assert other.get(f"/_woltspace/grant?code={code}").status_code == 403


def test_opening_a_site_needs_the_owner(locked):
    _, keys = locked
    assert _lodge("localhost:7777").get("/sites/open?wolt=softie").status_code == 401
    wolt = _lodge("localhost:7777").get("/sites/open?wolt=softie",
                                        headers={"x-woltspace-key": keys["wolt"]})
    assert wolt.status_code == 403
    owner = _lodge("localhost:7777").get("/sites/open?wolt=softie", follow_redirects=False,
                                         headers={"x-woltspace-key": keys["owner"]})
    assert owner.status_code == 303


@pytest.mark.parametrize("next_path", ["//evil.example/x", "https://evil.example", "\\\\evil", "x"])
def test_a_grant_never_redirects_off_the_site(locked, next_path):
    root, _ = locked
    code = lodge_auth.new_site_grant(root, "softie")
    granted = _lodge().get("/_woltspace/grant", params={"code": code, "next": next_path},
                           follow_redirects=False)
    assert granted.headers["location"] == "/"


def test_auth_off_opening_a_site_just_redirects(running_mill):
    opened = _lodge("localhost:7777").get("/sites/open?wolt=softie&path=/notes/",
                                          follow_redirects=False)
    assert opened.headers["location"] == "http://softie.sites.localhost:7777/notes/"
    assert _lodge("localhost:7777").get("/sites/open?wolt=ghost").status_code == 404


def test_forgetting_browsers_closes_site_sessions(locked):
    root, _ = locked
    token = lodge_auth.redeem_site_grant(root, "softie", lodge_auth.new_site_grant(root, "softie"))
    assert lodge_auth.site_session_ok(root, "softie", token)
    lodge_auth.forget_browsers(root)
    assert not lodge_auth.site_session_ok(root, "softie", token)


# --- The lodge's origin never serves a wolt's pages when auth is on ------------------

def test_old_site_links_go_to_the_mill_and_serve_nothing(locked, monkeypatch):
    root, _ = locked
    site = root / "softie" / "wolt" / "site"
    (site / "evil.html").write_text("<script>fetch('/sessions')</script>")
    monkeypatch.setattr(server_app, "SITE_DIR", site)
    lodge = _paired(root)

    old = lodge.get("/wolt/softie/site/evil.html", follow_redirects=False)
    assert old.status_code == 303
    assert old.headers["location"] == "/sites/open?wolt=softie&path=/evil.html"
    assert "<script>" not in old.text

    assert "<script>" not in lodge.get("/evil.html").text   # the catch-all
    assert lodge.get("/sites").json()[0]["url"].startswith("/sites/open?wolt=")
    assert lodge.get("/sites/softie").json()["url"] == "/sites/open?wolt=softie&path=/"


def test_static_app_files_are_not_served_on_the_lodges_origin(locked, monkeypatch):
    root, _ = locked
    app = root / "apps" / "board"
    app.mkdir(parents=True)
    (app / "woltspace.json").write_text('{"name": "board", "port": 4100, "keeper": "softie"}')
    (app / "index.html").write_text("<script>fetch('/sessions')</script>")
    monkeypatch.setattr(server_app, "app_dir", lambda name: root / "apps" / name)
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    response = _paired(root).get("/app/board/")
    assert response.status_code == 404 and "<script>" not in response.text


def test_with_auth_off_old_site_links_go_to_the_mill_too(running_mill):
    assert _lodge("localhost:7777").get("/sites/softie").json()["url"] == "/sites/open?wolt=softie&path=/"
    old = _lodge("localhost:7777").get("/wolt/softie/site/data.txt", follow_redirects=False)
    assert old.headers["location"] == "/sites/open?wolt=softie&path=/data.txt"


# --- What else the lodge shows of a wolt's, and revocation ---------------------------

def test_a_spark_runs_opaque_on_the_lodges_origin_when_auth_is_on(locked, monkeypatch):
    root, _ = locked

    async def spark(spark_id):
        return {"id": spark_id, "html": "<script>fetch('/sessions')</script>", "version": 1,
                "totalVersions": 1}

    monkeypatch.setattr(server_app, "get_spark_with_chain", spark)
    response = _paired(root).get("/history/abc")
    csp = response.headers["content-security-policy"]
    assert csp.startswith("sandbox") and "allow-same-origin" not in csp


def test_a_tools_page_runs_opaque_on_the_lodges_origin_when_auth_is_on(locked, monkeypatch):
    import httpx
    root, _ = locked

    async def page(request):
        return httpx.Response(200, html="<script>fetch('/sessions')</script>",
                              headers={"content-security-policy": "default-src *"})

    real = httpx.AsyncClient
    monkeypatch.setattr(server_app.tool_registry, "get", lambda name: {"port": 4998})
    monkeypatch.setattr(server_app.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(page)))
    response = _paired(root).get("/tools/board/")
    assert response.headers.get_list("content-security-policy") == [server_app.OPAQUE_PAGE_CSP]
    assert "allow-same-origin" not in server_app.OPAQUE_PAGE_CSP


def test_forgetting_browsers_voids_unspent_grants_and_codes(locked):
    root, _ = locked
    grant = lodge_auth.new_site_grant(root, "softie")
    code = lodge_auth.new_pairing_code(root)
    lodge_auth.forget_browsers(root)
    assert lodge_auth.redeem_site_grant(root, "softie", grant) is None
    assert lodge_auth.redeem_pairing_code(root, code) is None


def _site_socket(root):
    token = lodge_auth.redeem_site_grant(root, "softie", lodge_auth.new_site_grant(root, "softie"))
    return _lodge().websocket_connect("/_woltspace/livereload", headers={
        "host": SITE, "origin": f"http://{SITE}", "cookie": f"{lodge_auth.SITE_COOKIE}={token}"})


@pytest.mark.parametrize("active", [False, True])
def test_a_forgotten_site_session_closes_its_open_socket(locked, monkeypatch, active):
    root, _ = locked
    monkeypatch.setattr(server_app, "SITE_SOCKET_RECHECK_SECONDS", 0.3)
    page = root / "softie" / "wolt" / "site" / "data.txt"
    with _site_socket(root) as ws:
        lodge_auth.forget_browsers(root)
        deadline = time.monotonic() + 5
        with pytest.raises(WebSocketDisconnect):
            while time.monotonic() < deadline:
                if active:
                    page.write_text(str(time.monotonic()))  # reload traffic keeps flowing
                ws.receive_text()
        assert time.monotonic() < deadline


def test_the_lodges_own_frame_offers_a_link_instead_of_spending_a_grant(locked):
    root, _ = locked
    lodge = _paired(root)
    framed = lodge.get("/wolt/softie/site/", headers={"sec-fetch-dest": "iframe"},
                       follow_redirects=False)
    assert framed.status_code == 200
    assert 'href="/sites/open?wolt=softie&amp;path=/"' in framed.text and 'target="_blank"' in framed.text
    assert not lodge_auth._read_json(lodge_auth.auth_dir(root) / "site-grants.json")


def test_opening_a_wolt_without_a_site_is_not_found(locked):
    root, keys = locked
    (root / "nosite" / "wolt").mkdir(parents=True)
    response = _lodge("localhost:7777").get("/sites/open?wolt=nosite",
                                            headers={"x-woltspace-key": keys["owner"]})
    assert response.status_code == 404
