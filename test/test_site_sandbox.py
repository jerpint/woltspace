"""Wolt-authored content is sandboxed; the lodge never trusts `Origin: null`.

A wolt's site and its sparks render on the lodge origin. The server answers
every one of those documents with an enforced `Content-Security-Policy:
sandbox allow-scripts`, so a prompt-injected page runs with an opaque origin:
its requests arrive as `Origin: null`, which the write guard and every
websocket handler reject, and owner pages carry `frame-ancestors 'self'` so
that sandboxed content cannot frame them back.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import sites
import wolts as wolts_mod

import server.app as server_app
import server.sparks as sparks_mod
from server.access import AccessSettings, AccessTokenVerifier

SANDBOX = "sandbox allow-scripts"
OWNER_FRAMES = "frame-ancestors 'self'"
WOLT = "n00b"
EVIL = "<script>fetch('/settings/access',{method:'POST'})</script>"


@pytest.fixture(autouse=True)
def tmp_wolts(tmp_path, monkeypatch):
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(wolts_mod, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.delenv("WOLTSPACE_SITE_SHELL", raising=False)

    wolt = tmp_path / WOLT / "wolt"
    site = wolt / "site"
    (site / "pages").mkdir(parents=True)
    (site / "index.html").write_text(f"<html><head><title>Home</title></head><body>{EVIL}</body></html>")
    (site / "pages" / "deep.html").write_text(f"<title>Deep</title><body>{EVIL}</body>")
    (site / "logo.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    (site / "noext").write_text("<html><body>no extension</body></html>")
    (site / "header.html").write_text('<img src=x onerror="fetch(\'/settings/access\',{method:\'POST\'})">')
    (site / "evil.css").write_text("body{background:url(/settings/access)}")
    (site / "site.json").write_text(json.dumps({
        "header_html": "header.html", "custom_css": "evil.css", "title": "  Noob   Works  ",
        "fonts_href": "https://evil.example/exfil.css",
        "tokens": {"accent": "#123456", "bg": "url(/sessions)", "ink": "red", "line": "#abcd",
                   "display_font": "x; background:url(//evil.example/f)", "emoji": "🦝"},
    }))
    (site / "tools").mkdir()
    (site / "tools" / "index.html").write_text("<html><body>site tools page</body></html>")
    (wolt / "wolt.json").write_text(json.dumps({"name": WOLT, "type": "raccoon", "display_name": "Noob"}))
    (wolt / "memory").mkdir()
    (wolt / "memory" / "identity.md").write_text("# n00b\n\nA raccoon.\n")

    bare = tmp_path / "bare" / "wolt"
    (bare / "site").mkdir(parents=True)
    (bare / "wolt.json").write_text(json.dumps({"name": "bare", "type": "raccoon"}))

    sparks = tmp_path / "sparks"
    sparks.mkdir()
    (sparks / "spark-1.json").write_text(json.dumps({
        "id": "spark-1", "type": "html", "title": "a spark", "timestamp": "2026-01-01T00:00:00Z",
        "html": f"<html><body>{EVIL}</body></html>",
    }))
    monkeypatch.setattr(sparks_mod, "SPARKS_DIR", sparks)
    return tmp_path


@pytest.fixture
def client():
    return TestClient(server_app.app, base_url="http://localhost:7777")


def assert_sandboxed(response):
    assert response.headers.get("content-security-policy") == SANDBOX, response.url
    assert response.headers.get("x-content-type-options") == "nosniff", response.url
    assert "access-control-allow-origin" not in response.headers, response.url


def assert_owner_page(response):
    assert response.status_code == 200, response.url
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers.get("content-security-policy") == OWNER_FRAMES, response.url
    assert "sandbox" not in response.headers.get("content-security-policy", "")


# --- The sandbox header is on every wolt-authored response ---------------

@pytest.mark.parametrize("path, status, content_type", [
    (f"/wolt/{WOLT}/site/", 200, "text/html"),
    (f"/wolt/{WOLT}/site/index.html", 200, "text/html"),
    (f"/wolt/{WOLT}/site/pages/deep.html", 200, "text/html"),
    (f"/wolt/{WOLT}/site/logo.svg", 200, "image/svg+xml"),
    (f"/wolt/{WOLT}/site/noext", 200, "application/octet-stream"),
    (f"/wolt/{WOLT}/site/header.html", 200, "text/html"),
    (f"/wolt/{WOLT}/site/missing.html", 404, "text/plain"),
    (f"/wolt/{WOLT}/site/pages/missing", 404, "text/plain"),
    ("/wolt/ghost/site/", 404, "text/html"),
    ("/wolt/9bad/site/", 400, "application/json"),
    ("/wolt/bare/site/", 307, None),
    ("/history/spark-1", 200, "text/html"),
    ("/history/missing", 404, "text/plain"),
    (f"/wolt/{WOLT}/_/", 200, "text/html"),
    (f"/wolt/{WOLT}/_/about", 200, "text/html"),
    (f"/wolt/{WOLT}/_/memory", 200, "text/html"),
    (f"/wolt/{WOLT}/_/settings", 200, "text/html"),
    (f"/wolt/{WOLT}/_/nope", 404, "text/plain"),
    ("/wolt/ghost/_/", 404, "text/plain"),
    (f"/wolt/{WOLT}/_/memory.json", 200, "application/json"),
    (f"/wolt/{WOLT}/_/manifest.json", 200, "application/json"),
])
def test_untrusted_routes_are_sandboxed_with_a_correct_type(client, path, status, content_type):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == status, path
    assert_sandboxed(response)
    if content_type:
        assert response.headers["content-type"].startswith(content_type), path


def test_head_requests_carry_the_sandbox_too(client):
    assert_sandboxed(client.head(f"/wolt/{WOLT}/site/"))
    assert_sandboxed(client.head(f"/wolt/{WOLT}/site/logo.svg"))


def test_directory_redirect_under_a_site_is_sandboxed(client):
    response = client.get(f"/wolt/{WOLT}/site/pages", follow_redirects=False)

    assert response.status_code == 308
    assert response.headers["location"].endswith(f"/wolt/{WOLT}/site/pages/")
    assert_sandboxed(response)


def test_redirect_destinations_carry_their_own_header(client, monkeypatch):
    """A redirect's CSP cannot sandbox where it lands; every destination must."""
    landed = client.get(f"/wolt/{WOLT}/site/pages", follow_redirects=True)
    assert landed.status_code == 404 and str(landed.url).endswith("/site/pages/")  # no index.html there
    assert_sandboxed(landed)

    # A site without a home page lands on the built-in About page, which
    # carries the wolt's boot files and is sandboxed like the site.
    about = client.get("/wolt/bare/site/", follow_redirects=True)
    assert str(about.url).endswith("/wolt/bare/_/")
    assert_sandboxed(about)

    # The viewport redirect follows whatever URL a wolt pushed: the site it
    # points at answers sandboxed on its own.
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": f"/wolt/{WOLT}/site/")
    hop = client.get("/current?session=main", follow_redirects=False)
    assert hop.status_code == 302
    assert_sandboxed(client.get("/current?session=main", follow_redirects=True))
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": "/history/spark-1")
    assert_sandboxed(client.get("/current?session=main", follow_redirects=True))


def test_sandbox_is_enforced_not_report_only_or_meta(client):
    response = client.get(f"/wolt/{WOLT}/site/")

    assert "content-security-policy-report-only" not in response.headers
    assert "allow-same-origin" not in response.headers["content-security-policy"]
    assert EVIL in response.text  # the page is served as written; the header does the isolating


def test_null_origin_is_never_granted_cors(client):
    for path in (f"/wolt/{WOLT}/site/", "/history/spark-1", "/sessions", f"/wolt/{WOLT}/_/memory.json"):
        response = client.get(path, headers={"origin": "null"})
        assert "access-control-allow-origin" not in response.headers, path
        assert "access-control-allow-credentials" not in response.headers, path
    preflight = client.options("/wolts/n00b/settings", headers={
        "origin": "null", "access-control-request-method": "PATCH",
    })
    assert "access-control-allow-origin" not in preflight.headers


def test_root_served_site_files_are_sandboxed(client, tmp_path, monkeypatch):
    """The catch-all serves the lodge process's own wolt site at `/`."""
    root_site = tmp_path / "root-site"
    root_site.mkdir()
    (root_site / "evil.html").write_text(f"<html><body>{EVIL}</body></html>")
    (root_site / "pic.svg").write_text("<svg/>")
    monkeypatch.setattr(server_app, "SITE_DIR", root_site)

    page = client.get("/evil.html")
    assert page.status_code == 200 and EVIL in page.text
    assert_sandboxed(page)
    framed = client.get("/evil.html", headers={"sec-fetch-dest": "iframe"})
    assert_sandboxed(framed)
    assert_sandboxed(client.get("/pic.svg"))


def test_catch_all_sandboxes_every_site_file_type_but_not_owner_html(client, tmp_path, monkeypatch):
    """The catch-all serves root owner HTML, then wolt-site files, then platform assets."""
    root_site = tmp_path / "root-site"
    root_site.mkdir()
    for name, body in (("app.js", "fetch('/settings/access',{method:'POST'})"), ("data.json", "{}"),
                       ("style.css", "body{}"), ("notes.txt", "hi"), ("blob", "\x00\x01")):
        (root_site / name).write_text(body)
    monkeypatch.setattr(server_app, "SITE_DIR", root_site)

    for name in ("app.js", "data.json", "style.css", "notes.txt", "blob"):
        response = client.get(f"/{name}")
        assert response.status_code == 200, name
        assert_sandboxed(response)
    assert client.get("/blob").headers["content-type"].startswith("application/octet-stream")

    # Owner HTML through the same route: the home page and the platform files.
    for path in ("/", "/onboard.html", "/public-view.html", "/placeholder.html"):
        assert_owner_page(client.get(path))
    missing = client.get("/no-such-file")
    assert missing.status_code == 404 and "content-security-policy" not in missing.headers


# --- The tool registry and proxy are gone, for every spelling and method ---

@pytest.mark.parametrize("path", ["/tools", "/tools/", "/tools/spawn", "/tools/x/y", "/tools/index.html",
                                  "/Tools", "/TOOLS/x/y", "/tools/./x", "/%74ools", "/tools%2Fx", "/tools/x%2F..%2F..%2Fsettings"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"])
def test_tools_paths_are_gone_for_every_method_and_spelling(client, tmp_path, monkeypatch, method, path):
    # A root-site dir with content under tools/ (case-insensitive filesystems
    # also answer Tools/ and TOOLS/ from it): none of it may be served.
    root_site = tmp_path / "root-site"
    (root_site / "tools" / "x").mkdir(parents=True)
    (root_site / "tools" / "index.html").write_text("<html><body>root site tools</body></html>")
    (root_site / "tools" / "x" / "y").write_text("<html><body>root site tools x/y</body></html>")
    monkeypatch.setattr(server_app, "SITE_DIR", root_site)

    response = client.request(method, path, content='{"name":"t","command":"id","port":4100}',
                              headers={"content-type": "application/json", "origin": "http://localhost:7777"},
                              follow_redirects=False)

    # The exact path is answered 404 by the removed-route handler; a case
    # variant reaches the GET-only catch-all, which refuses it too (404), and
    # has no other method at all (405). Never a document either way.
    assert response.status_code in (404, 405), (method, path, response.headers.get("content-type"))
    assert response.headers["content-type"].startswith("application/json"), (method, path)
    if method != "HEAD":
        assert response.json() in ({"error": "not found"}, {"detail": "Method Not Allowed"})
    assert "content-security-policy" not in response.headers


def test_encoded_slash_tools_alias_never_becomes_an_owner_document(client):
    response = client.get("/%2Ftools", follow_redirects=False)
    assert response.status_code in (404, 400, 307, 308)
    if response.status_code == 404:
        assert not response.headers["content-type"].startswith("text/html") or \
            response.headers.get("content-security-policy") == SANDBOX


def test_tool_registry_module_and_routes_are_removed():
    assert not (ROOT / "server" / "tools.py").exists()
    source = (ROOT / "server" / "app.py").read_text()
    for gone in ("tool_registry", "proxy_tool", "spawn_tool", "_start_tool_gc", "import httpx"):
        assert gone not in source, gone
    assert "TOOL_REGISTRY_FILE" not in (ROOT / "server" / "config.py").read_text()


# --- GET /current only sends the browser to lodge destinations ------------

@pytest.mark.parametrize("url, status", [
    (f"/wolt/{WOLT}/site/page.html", 302),
    ("/history/spark-1", 302),
    ("/settings", 302),
    ("http://my-app.localhost:7117/", 302),
    ("http://localhost:7777/w/n00b", 302),
    ("https://evil.example/", 400),
    ("//evil.example/", 400),
    ("/\\evil.example", 400),
    ("javascript:alert(1)", 400),
    ("data:text/html,hi", 400),
    ("http://evil.example:7777/", 400),
    ("http://[::1", 400),
])
def test_viewport_redirect_follows_only_lodge_destinations(client, monkeypatch, url, status):
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": url)

    response = client.get("/current?session=main", follow_redirects=False)

    assert response.status_code == status, url
    if status == 302:
        assert response.headers["location"] == url
    else:
        assert "location" not in response.headers


def test_a_wolt_site_cannot_shadow_the_service_worker(client, tmp_path, monkeypatch):
    root_site = tmp_path / "root-site"
    root_site.mkdir()
    (root_site / "sw.js").write_text("self.addEventListener('fetch', e => e.respondWith(new Response('owned')))")
    monkeypatch.setattr(server_app, "SITE_DIR", root_site)

    response = client.get("/sw.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert "owned" not in response.text
    assert response.text == (ROOT / "public" / "sw.js").read_text()
    assert "content-security-policy" not in response.headers


# --- Owner pages are framable by the lodge only, and not sandboxed --------

@pytest.mark.parametrize("path", ["/", "/settings", "/tui", "/terminal", "/connectors", "/wolves",
                                  f"/w/{WOLT}", "/placeholder.html", "/onboard.html", "/public-view.html"])
def test_owner_pages_allow_only_the_lodge_to_frame_them(client, path):
    assert_owner_page(client.get(path))


def test_owner_assets_and_apis_are_not_sandboxed(client):
    for path in ("/static/wolt-page.js", "/static/wolt-shell/shell.js", "/sw.js", "/sessions",
                 "/history", "/history/spark-1/meta", f"/wolts/{WOLT}/settings"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "sandbox" not in response.headers.get("content-security-policy", ""), path


def test_framing_directive_is_added_even_when_owner_html_has_its_own_policy():
    """Whatever policy an owner response already names, frame-ancestors is required."""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse

    def request(path):
        return Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                        "headers": [(b"host", b"localhost:7777")]})

    async def run(path, response):
        async def call_next(_request):
            return response
        return await server_app.content_isolation_headers(request(path), call_next)

    own = asyncio.run(run("/settings", HTMLResponse("x", headers={"Content-Security-Policy": "default-src 'self'"})))
    assert own.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'self'"
    already = asyncio.run(run("/settings", HTMLResponse("x", headers={"Content-Security-Policy": "frame-ancestors 'none'"})))
    assert already.headers["content-security-policy"] == "frame-ancestors 'none'"
    plain = asyncio.run(run("/settings", HTMLResponse("x")))
    assert plain.headers["content-security-policy"] == OWNER_FRAMES
    api = asyncio.run(run("/sessions", JSONResponse([])))
    assert "content-security-policy" not in api.headers
    # An upstream policy on an untrusted path never replaces the sandbox.
    site = asyncio.run(run(f"/wolt/{WOLT}/site/x.html", HTMLResponse("x", headers={"Content-Security-Policy": "sandbox allow-same-origin"})))
    assert site.headers["content-security-policy"] == SANDBOX


def test_wolt_page_frames_the_site_with_a_sandbox_attribute():
    source = (ROOT / "public" / "static" / "wolt-page.js").read_text()
    assert "frame.setAttribute('sandbox','allow-scripts')" in source
    assert "allow-same-origin" not in source


def test_split_view_sandboxes_sites_and_sparks_but_not_apps():
    source = (ROOT / "templates" / "tui.html").read_text()
    assert "preview.setAttribute('sandbox', 'allow-scripts')" in source
    assert "preview.removeAttribute('sandbox')" in source
    assert "allow-same-origin" not in source


# --- The site shell works from inside the sandbox, without trusting it ----

def _manifest(html):
    start = html.index("window.__WOLT_SHELL__=") + len("window.__WOLT_SHELL__=")
    end = html.index(";</script>", start)
    return json.loads(html[start:end])


def test_site_page_inlines_its_slots_instead_of_fetching(client):
    manifest = _manifest(client.get(f"/wolt/{WOLT}/site/").text)

    assert "onerror" in manifest["site"]["slots"]["header"]
    assert "footer" not in manifest["site"]["slots"]
    shell = (ROOT / "public" / "static" / "wolt-shell" / "shell.js").read_text()
    assert "fetch(m.base" not in shell


def test_builtin_pages_inline_only_the_memory_each_tab_shows(client):
    memory = _manifest(client.get(f"/wolt/{WOLT}/_/memory").text)["memory"]
    about = _manifest(client.get(f"/wolt/{WOLT}/_/").text)["memory"]
    settings = _manifest(client.get(f"/wolt/{WOLT}/_/settings").text)["memory"]

    assert memory["identity"].startswith("# n00b") and set(memory) >= {"context", "learnings", "archive"}
    assert about == {"identity": memory["identity"]}
    assert settings == {}


def test_builtin_pages_carry_no_wolt_stylesheet_font_slot_or_free_form_token(client):
    html = client.get(f"/wolt/{WOLT}/_/").text
    manifest = _manifest(html)

    assert manifest["site"] == {"title": "Noob Works", "tokens": {"accent": "#123456", "line": "#abcd", "emoji": "🦝"}}
    for leaked in ("evil.css", "evil.example", "url(", "header.html", "onerror", "display_font", "custom_css", "fonts_href"):
        assert leaked not in html, leaked
    page = (ROOT / "public" / "static" / "wolt-shell" / "wolt.html").read_text()
    assert "fonts_href" not in page and "display_font" not in page
    assert "HEX.test(t[k])" in page


def test_owner_look_accepts_only_checked_values():
    import site_shell
    assert site_shell.owner_look({}) == {}
    assert site_shell.owner_look({"tokens": "nope", "title": 7}) == {}
    look = site_shell.owner_look({
        "title": "x" * 200, "custom_css": "a.css", "fonts_href": "https://x", "header_html": "h.html",
        "tokens": {"accent": " #ABCDEF ", "bg": "#12345", "ink": "#fff;background:url(x)", "muted": "rgb(1,2,3)",
                   "line": "#00000080", "display_font": "serif", "emoji": "<b>🦝</b>" + "x" * 20},
    })
    assert look == {"title": "x" * 60, "tokens": {"accent": "#ABCDEF", "line": "#00000080"}}


def test_slot_files_stay_inside_the_site_dir(tmp_wolts):
    import site_shell
    sdir = tmp_wolts / WOLT / "wolt" / "site"
    assert site_shell.slot_html(sdir, "../wolt.json") is None
    assert site_shell.slot_html(sdir, "missing.html") is None
    assert site_shell.slot_html(sdir, 7) is None
    assert "onerror" in site_shell.slot_html(sdir, "header.html")


# --- `Origin: null` writes change nothing ---------------------------------

def _wolt_json(tmp_wolts):
    return json.loads((tmp_wolts / WOLT / "wolt" / "wolt.json").read_text())


def test_null_origin_credentialed_fetch_cannot_change_settings(client, tmp_wolts):
    before = _wolt_json(tmp_wolts)

    response = client.patch(f"/wolts/{WOLT}/settings", json={"display_name": "pwned"}, headers={
        "origin": "null", "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors",
        "cookie": "CF_Authorization=owner-session",
    })

    assert response.status_code == 403
    assert response.json() == {"error": "cross-site request rejected"}
    assert _wolt_json(tmp_wolts) == before


def test_null_origin_is_rejected_even_without_fetch_metadata(client, tmp_wolts):
    """Older browsers send Origin but no Sec-Fetch-*: `null` alone must fail."""
    before = _wolt_json(tmp_wolts)

    response = client.patch(f"/wolts/{WOLT}/settings", json={"display_name": "pwned"},
                            headers={"origin": "null"})

    assert response.status_code == 403
    assert response.json() == {"error": "cross-origin request rejected"}
    assert _wolt_json(tmp_wolts) == before


def test_null_origin_simple_no_cors_post_delivers_nothing(client, monkeypatch):
    delivered = Mock()
    monkeypatch.setattr(server_app, "deliver_message", delivered)

    response = client.post(f"/sessions/{WOLT}-main/message", content='{"text":"run this"}', headers={
        "content-type": "text/plain", "origin": "null", "sec-fetch-mode": "no-cors",
    })

    assert response.status_code == 403
    delivered.assert_not_called()


def test_null_origin_beacon_post_delivers_nothing(client, monkeypatch, tmp_wolts):
    delivered = Mock()
    monkeypatch.setattr(server_app, "deliver_message", delivered)
    before = _wolt_json(tmp_wolts)

    beacon = client.post(f"/wolts/{WOLT}/message", content="hello", headers={
        "content-type": "text/plain;charset=UTF-8", "origin": "null",
    })
    harness = client.post(f"/wolts/{WOLT}/harness", json={"harness": "codex"}, headers={"origin": "null"})

    assert beacon.status_code == 403
    assert harness.status_code == 403
    delivered.assert_not_called()
    assert _wolt_json(tmp_wolts) == before


def test_null_origin_form_post_is_rejected(client, tmp_wolts):
    before = _wolt_json(tmp_wolts)
    response = client.post("/settings/session-expiry", data={"idle_timeout_seconds": "3600"},
                           headers={"origin": "null"})
    assert response.status_code == 403
    assert _wolt_json(tmp_wolts) == before


def test_same_origin_write_still_works_as_the_positive_control(client, tmp_wolts):
    response = client.patch(f"/wolts/{WOLT}/settings", json={"display_name": "Renamed"},
                            headers={"origin": "http://localhost:7777"})

    assert response.status_code == 200
    assert _wolt_json(tmp_wolts)["display_name"] == "Renamed"


@pytest.mark.parametrize("origin", ["null", "NULL", " null "])
def test_same_authority_check_treats_null_as_a_value(origin):
    assert server_app._same_http_authority(origin, "localhost:7777") is False
    assert server_app._same_http_authority(origin, "owner.woltspace.test") is False


# --- Through Cloudflare Access the answer is the same ---------------------

SETTINGS = AccessSettings(
    team_domain="team.cloudflareaccess.com", lodge_aud="lodge-audience",
    apps_aud="apps-audience", owner_email="owner@example.com",
)


def _access_configured(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = AccessTokenVerifier()

    async def refresh(team_domain):
        verifier._team_domain = team_domain
        verifier._keys = {"key-1": private_key.public_key()}
        verifier._expires_at = time.monotonic() + 3600
        return verifier._keys

    monkeypatch.setattr(verifier, "_refresh", refresh)
    monkeypatch.setattr(server_app, "access_token_verifier", verifier)
    monkeypatch.setattr(server_app, "load_access_settings", lambda _root: SETTINGS)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")
    now = int(time.time())
    return jwt.encode({
        "iss": "https://team.cloudflareaccess.com", "aud": "lodge-audience",
        "email": "owner@example.com", "iat": now, "exp": now + 300,
    }, private_key, algorithm="RS256", headers={"kid": "key-1"})


def test_authenticated_owner_session_with_null_origin_still_cannot_write(monkeypatch, tmp_wolts):
    token = _access_configured(monkeypatch)
    before = _wolt_json(tmp_wolts)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server_app.app, client=("10.0.0.9", 1234)),
            base_url="https://owner.woltspace.test",
        ) as http:
            headers = {"host": "owner.woltspace.test", "cf-access-jwt-assertion": token}
            allowed = await http.get(f"/wolt/{WOLT}/site/", headers=headers)
            rejected = await http.patch(f"/wolts/{WOLT}/settings", json={"display_name": "pwned"},
                                        headers={**headers, "origin": "null"})
            after_rejection = _wolt_json(tmp_wolts)
            control = await http.patch(f"/wolts/{WOLT}/settings", json={"display_name": "Owner"},
                                       headers={**headers, "origin": "https://owner.woltspace.test"})
            return allowed, rejected, after_rejection, control

    allowed, rejected, after_rejection, control = asyncio.run(run())

    assert allowed.status_code == 200 and allowed.headers["content-security-policy"] == SANDBOX
    assert rejected.status_code == 403
    assert after_rejection == before
    assert control.status_code == 200  # the same owner, naming the lodge: the positive control
    assert _wolt_json(tmp_wolts)["display_name"] == "Owner"


def test_authenticated_owner_websocket_with_null_origin_does_not_attach(monkeypatch):
    token = _access_configured(monkeypatch)
    attach = Mock()
    monkeypatch.setattr(server_app, "attach_tmux", attach)

    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(server_app.app, base_url="https://owner.woltspace.test").websocket_connect(
            "/tui?session=main",
            headers={"host": "owner.woltspace.test", "origin": "null", "cf-access-jwt-assertion": token},
        ):
            pass

    assert exc.value.code == 1008
    attach.assert_not_called()


# --- No websocket accepts `Origin: null` ----------------------------------

@pytest.mark.parametrize("path", ["/tui?session=main", "/livereload", f"/wolt/{WOLT}/site/livereload"])
def test_lodge_websockets_close_on_null_origin_before_accepting(client, path, monkeypatch):
    attach = Mock()
    monkeypatch.setattr(server_app, "attach_tmux", attach)

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(path, headers={"host": "localhost:7777", "origin": "null"}):
            pass

    assert exc.value.code == 1008
    attach.assert_not_called()


def test_site_livereload_from_inside_the_sandbox_is_refused_not_relaxed(client):
    """The reload script a site page carries now connects with Origin null."""
    html = client.get(f"/wolt/{WOLT}/site/").text
    assert f"/wolt/{WOLT}/site/livereload" in html  # still injected, documented as broken

    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/wolt/{WOLT}/site/livereload",
                                      headers={"host": "localhost:7777", "origin": "null"}):
            pass
    assert exc.value.code == 1008


def test_public_site_manifest_inlines_the_wolts_own_slots(tmp_path):
    """On the site's own origin the shell draws only slots it was handed."""
    import site_shell

    wolt = tmp_path / "astre"
    site = wolt / "wolt" / "site"
    site.mkdir(parents=True)
    (site / "header.html").write_text("<b>hello</b>")
    cfg = {"header_html": "header.html", "footer_html": "missing.html"}
    manifest = site_shell.public_shell_manifest("astre", wolt, site, cfg)
    assert manifest["site"]["slots"] == {"header": "<b>hello</b>"}
    assert "memory" not in manifest
