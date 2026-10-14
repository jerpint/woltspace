"""The lodge side of wolt sites on the Mill: one resolver, navigations redirected,
subresources and remote requests kept opaque, and frame flags chosen from the
resolved host (never from the look of a string).
"""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps as apps_mod
import sites
import wolts as wolts_mod

import server.app as server_app

GATEWAY_PORT = 7117
SANDBOX = "sandbox allow-scripts"
LOCAL = ("127.0.0.1", 4242)
REMOTE = ("10.1.2.3", 4242)


def _wolt(root: Path, name: str, *, site: bool = True) -> None:
    home = root / name / "wolt"
    (home / "memory").mkdir(parents=True)
    (home / "wolt.json").write_text(json.dumps({"name": name, "type": "raccoon"}))
    (home / "memory" / "identity.md").write_text(f"# {name}\n")
    if site:
        (home / "site" / "nested").mkdir(parents=True)
        (home / "site" / "index.html").write_text(f"<html><head><title>{name}</title></head><body>{name}</body></html>")
        (home / "site" / "data.json").write_text('{"hello": "%s"}' % name)
        (home / "site" / "nested" / "page.html").write_text("<html><body>nested</body></html>")


@pytest.fixture(autouse=True)
def colony(tmp_path, monkeypatch):
    for module in (sites, wolts_mod, server_app):
        monkeypatch.setattr(module, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(apps_mod, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(apps_mod, "APPS_DIR", tmp_path / "apps")
    monkeypatch.setattr(apps_mod, "LEGACY_PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setenv("WOLTSPACE_APP_GATEWAY_PORT", str(GATEWAY_PORT))
    monkeypatch.delenv("WOLTSPACE_ISOLATION", raising=False)
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")
    (tmp_path / "woltspace.json").write_text(json.dumps({"apps_domain": "owner.woltspace.app"}))
    _wolt(tmp_path, "astre")
    _wolt(tmp_path, "Pine")                 # a directory name DNS lowercases
    _wolt(tmp_path, "under_score")          # not a DNS label: no site host
    _wolt(tmp_path, "nosite", site=False)
    app_dir = tmp_path / "apps" / "notes"
    app_dir.mkdir(parents=True)
    (app_dir / "woltspace.json").write_text(json.dumps({"name": "notes", "keeper": "astre", "port": 4321}))
    return tmp_path


def _client(peer=LOCAL) -> TestClient:
    return TestClient(server_app.app, base_url="http://localhost:7777", client=peer)


def canonical(label: str, rest: str = "") -> str:
    return f"http://{label}.sites.localhost:{GATEWAY_PORT}/{rest}"


# --- The resolver ---------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("/wolt/astre/site/", canonical("astre")),
    ("/wolt/astre/site", canonical("astre")),
    ("/wolt/astre/site/nested/page.html", canonical("astre", "nested/page.html")),
    ("/wolt/astre/site/a%20b.html", canonical("astre", "a%20b.html")),
    ("/wolt/Pine/site/", canonical("pine")),
    ("/wolt/pine/site/data.json", canonical("pine", "data.json")),
    ("/wolt/under_score/site/", None),       # directory name cannot be a host label
    ("/wolt/ghost/site/", None),             # no such wolt
    ("/wolt/nosite/site/", None),            # wolt without a site directory
    ("/wolt/astre/_/", None),                # built-in pages stay on the lodge
    ("/wolt/astre/site/../other", None),
    ("/wolt/astre/site/./x", None),
    ("/wolt/astre/site/a//b", None),
    ("/wolt/astre/site/a\\b", None),
    ("/history/spark-1", None),
    ("/settings", None),
    ("wolt/astre/site/", None),
])
def test_canonical_site_url_for_local_owner(path, expected):
    assert server_app.canonical_site_url(path, local=True) == expected


def test_canonical_site_url_is_never_remote():
    """Remote site hosts are gated in this slice: a remote request resolves nothing."""
    assert server_app.canonical_site_url("/wolt/astre/site/", local=False) is None
    assert server_app.resolve_site_url("/wolt/astre/site/", local=False) is None
    assert server_app.resolve_site_url(canonical("astre"), local=False) is None


def test_canonical_site_url_follows_the_configured_gateway_port(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_APP_GATEWAY_PORT", "7200")
    assert server_app.canonical_site_url("/wolt/astre/site/", local=True) == "http://astre.sites.localhost:7200/"


def test_canonical_site_url_refuses_a_symlinked_site_dir(colony):
    real = colony / "elsewhere"
    real.mkdir()
    (real / "index.html").write_text("x")
    (colony / "sym" / "wolt").mkdir(parents=True)
    (colony / "sym" / "wolt" / "wolt.json").write_text("{}")
    (colony / "sym" / "wolt" / "site").symlink_to(real, target_is_directory=True)
    assert server_app.canonical_site_url("/wolt/sym/site/", local=True) is None


@pytest.mark.parametrize("url, expected", [
    ("/wolt/astre/site/p.html?x=1&y=2", canonical("astre", "p.html?x=1&y=2")),
    ("/wolt/astre/site/", canonical("astre")),
    (canonical("astre", "nested/page.html"), canonical("astre", "nested/page.html")),
    (canonical("pine"), canonical("pine")),
    ("http://astre.sites.localhost:7118/", None),        # not the gateway port
    ("http://astre.sites.localhost/", None),
    ("https://astre.sites.localhost:7117/", None),       # wrong scheme for a local host
    ("http://ghost.sites.localhost:7117/", None),        # no such site
    ("http://under_score.sites.localhost:7117/", None),
    ("http://notes.localhost:7117/", None),              # an app, not a site
    ("http://site-astre.owner.woltspace.app/", None),    # remote hosts are gated
    ("http://evil.example/wolt/astre/site/", None),
    ("/history/spark-1", None),
    ("", None),
    # Fragments and queries survive, with or without a trailing slash.
    ("/wolt/astre/site/index.html#section", canonical("astre", "index.html#section")),
    ("/wolt/astre/site#top", canonical("astre", "#top")),
    ("/wolt/astre/site?v=1#top", canonical("astre", "?v=1#top")),
    # Spellings parsers disagree about never resolve.
    ("http://localhost:7777\\@astre.sites.localhost:7117/", None),
    ("http://user@astre.sites.localhost:7117/", None),
    ("http://astre.sites.localhost:7117/a b", None),
    ("http://astre.sites.localhost:7117/\tx", None),
    ("/wolt/astre/site/a\\b", None),
    # An accepted URL is rebuilt, normalized, not echoed.
    ("http://ASTRE.sites.localhost.:7117/p?q=1#f", canonical("astre", "p?q=1#f")),
])
def test_resolve_site_url(url, expected):
    assert server_app.resolve_site_url(url, local=True) == expected


# --- Legacy lodge paths: navigations redirect, everything else stays opaque ----

@pytest.mark.parametrize("dest", ["document", "iframe", "Document"])
@pytest.mark.parametrize("path, location", [
    ("/wolt/astre/site/", canonical("astre")),
    ("/wolt/astre/site", canonical("astre")),
    ("/wolt/astre/site/nested/page.html", canonical("astre", "nested/page.html")),
    ("/wolt/astre/site/nested/page.html?v=2&q=a%20b", canonical("astre", "nested/page.html?v=2&q=a%20b")),
    ("/wolt/astre/site/a%20b.html", canonical("astre", "a%20b.html")),
    ("/wolt/Pine/site/", canonical("pine")),
])
def test_local_navigation_redirects_to_the_canonical_site_url(path, location, dest):
    for method in ("GET", "HEAD"):
        response = _client().request(method, path, headers={"sec-fetch-dest": dest}, follow_redirects=False)
        assert response.status_code == 302, (method, path)
        assert response.headers["location"] == location, (method, path)


@pytest.mark.parametrize("headers", [
    {},
    {"sec-fetch-dest": "script"},
    {"sec-fetch-dest": "image"},
    {"sec-fetch-dest": "style"},
    {"sec-fetch-dest": "empty", "sec-fetch-mode": "cors"},   # fetch()
    {"sec-fetch-dest": "font"},
    {"sec-fetch-dest": "serviceworker"},
])
def test_local_subresource_requests_keep_the_opaque_lodge_copy(headers):
    for path, body in (("/wolt/astre/site/data.json", '"hello"'), ("/wolt/astre/site/index.html", "astre")):
        response = _client().get(path, headers=headers, follow_redirects=False)
        assert response.status_code == 200, (path, headers)
        assert "location" not in response.headers
        assert response.headers["content-security-policy"] == SANDBOX
        assert response.headers["x-content-type-options"] == "nosniff"
        assert body in response.text


@pytest.mark.parametrize("peer, headers", [
    (REMOTE, {"sec-fetch-dest": "document"}),
    (REMOTE, {"sec-fetch-dest": "iframe"}),
    (LOCAL, {"sec-fetch-dest": "document", "x-forwarded-for": "10.9.9.9"}),
    (LOCAL, {"sec-fetch-dest": "document", "cf-connecting-ip": "10.9.9.9"}),
    (LOCAL, {"sec-fetch-dest": "document", "cf-access-jwt-assertion": "token"}),
])
def test_non_local_navigations_keep_the_opaque_lodge_copy(peer, headers):
    """Remote sites are gated: no remote canonical URL exists in this slice."""
    response = _client(peer).get("/wolt/astre/site/", headers=headers, follow_redirects=False)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert response.headers["content-security-policy"] == SANDBOX
    assert "astre" in response.text


def test_external_isolation_trusts_the_published_loopback_port(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    response = _client(("172.17.0.1", 5)).get("/wolt/astre/site/", headers={"sec-fetch-dest": "document"},
                                              follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == canonical("astre")


def test_wolts_without_a_site_host_keep_the_legacy_copy_on_navigation():
    hostless = _client().get("/wolt/under_score/site/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert hostless.status_code == 200 and "under_score" in hostless.text
    assert hostless.headers["content-security-policy"] == SANDBOX
    missing = _client().get("/wolt/ghost/site/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert missing.status_code == 404 and missing.headers["content-security-policy"] == SANDBOX
    no_site = _client().get("/wolt/nosite/site/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert no_site.status_code == 404 and no_site.headers["content-security-policy"] == SANDBOX
    bad = _client().get("/wolt/9bad/site/", headers={"sec-fetch-dest": "document"}, follow_redirects=False)
    assert bad.status_code == 400


def test_old_livereload_socket_still_refuses_null_origin():
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client().websocket_connect("/wolt/astre/site/livereload",
                                         headers={"host": "localhost:7777", "origin": "null"}):
            pass
    assert exc.value.code == 1008


# --- /sites exposes the canonical URL; /sites/resolve answers the split view ---

def test_sites_list_and_detail_carry_site_url_for_the_local_owner():
    listing = {entry["wolt"]: entry for entry in _client().get("/sites").json()}
    assert listing["astre"] == {"wolt": "astre", "url": "/wolt/astre/site/", "site_url": canonical("astre")}
    assert listing["Pine"]["site_url"] == canonical("pine")
    assert "site_url" not in listing["under_score"]
    assert "nosite" not in listing

    detail = _client().get("/sites/astre").json()
    assert detail == {"wolt": "astre", "url": "/wolt/astre/site/", "site_url": canonical("astre"), "dir_exists": True}
    assert "site_url" not in _client().get("/sites/under_score").json()
    assert _client().get("/sites/ghost").json()["dir_exists"] is False


def test_sites_carry_no_site_url_for_remote_requests():
    for entry in _client(REMOTE).get("/sites").json():
        assert "site_url" not in entry
    assert "site_url" not in _client(REMOTE).get("/sites/astre").json()
    assert _client(REMOTE).get("/sites/resolve", params={"path": "/wolt/astre/site/"}).json()["site_url"] is None


@pytest.mark.parametrize("path, site_url", [
    ("/wolt/astre/site/", canonical("astre")),
    ("/wolt/astre/site/nested/page.html?x=1", canonical("astre", "nested/page.html?x=1")),
    (canonical("astre"), canonical("astre")),
    ("http://astre.sites.localhost:7118/", None),
    ("http://notes.localhost:7117/", None),
    ("/wolt/under_score/site/", None),
    ("/wolt/ghost/site/", None),
    ("/history/spark-1", None),
    ("https://evil.example/", None),
    ("", None),
])
def test_sites_resolve(path, site_url):
    response = _client().get("/sites/resolve", params={"path": path})
    assert response.status_code == 200
    assert response.json() == {"path": path, "site_url": site_url}
    assert response.headers["cache-control"] == "no-store"
    assert "sandbox" not in response.headers.get("content-security-policy", "")


# --- GET /current goes only where the lodge can vouch for ------------------

@pytest.mark.parametrize("url, location", [
    ("/wolt/astre/site/", canonical("astre")),
    ("/wolt/astre/site/p.html?x=1", canonical("astre", "p.html?x=1")),
    ("/wolt/Pine/site/", canonical("pine")),
    ("/wolt/under_score/site/", "/wolt/under_score/site/"),   # no host: the lodge copy
    ("/history/spark-1", "/history/spark-1"),
    ("/settings", "/settings"),
    ("/wolt/astre/_/", "/wolt/astre/_/"),
    (canonical("astre", "x.html"), canonical("astre", "x.html")),
    ("http://notes.localhost:7117/", "http://notes.localhost:7117/"),      # a registered app
    ("http://localhost:7777/w/astre", "http://localhost:7777/w/astre"),    # the lodge itself
    ("http://127.0.0.1:7777/", "http://127.0.0.1:7777/"),
    ("https://owner.woltspace.test/settings", "https://owner.woltspace.test/settings"),
    ("https://notes.owner.woltspace.app/", "https://notes.owner.woltspace.app/"),  # a registered app, on the apps domain
])
def test_current_redirects_to_lodge_sites_apps_and_itself(monkeypatch, url, location):
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": url)
    response = _client().get("/current?session=main", follow_redirects=False)
    assert response.status_code == 302, url
    assert response.headers["location"] == location


@pytest.mark.parametrize("url", [
    "http://ghost.localhost:7117/",             # looks like an app; none registered
    "http://notes.localhost:9999/",             # a registered app, wrong port
    "http://notes.localhost/",                  # no port
    "https://notes.localhost:7117/",
    "http://astre.sites.localhost:7118/",       # a site, wrong port
    "http://ghost.sites.localhost:7117/",       # no such site
    "http://site-astre.localhost:7117/",        # reserved label
    "https://site-astre.owner.woltspace.app/",  # remote site hosts are gated
    "https://ghost.owner.woltspace.app/",       # looks like an app on the apps domain; none registered
    "http://notes.owner.woltspace.app/",        # the apps domain is https only
    "http://localhost:7778/",                   # not the lodge port
    "http://localhost/",
    "https://evil.example/",
    "//evil.example/",
    "/\\evil.example",
    "javascript:alert(1)",
    "data:text/html,hi",
    "http://[::1",
    "http://localhost:bad/",                    # invalid port: 400, not 500
    "http://localhost:99999/",                  # out-of-range port
    "http://localhost:7777\\@notes.localhost:7117/",
    "http://user@localhost:7777/",
])
def test_current_refuses_hosts_the_lodge_cannot_vouch_for(monkeypatch, url):
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": url)
    response = _client().get("/current?session=main", follow_redirects=False)
    assert response.status_code == 400, url
    assert "location" not in response.headers


def test_current_from_a_remote_request_never_names_a_local_host(monkeypatch):
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": "/wolt/astre/site/")
    lodge_copy = _client(REMOTE).get("/current?session=main", follow_redirects=False)
    assert lodge_copy.status_code == 302 and lodge_copy.headers["location"] == "/wolt/astre/site/"
    for url in ("http://notes.localhost:7117/", canonical("astre"), "http://localhost:7777/"):
        monkeypatch.setattr(server_app, "get_current_url", lambda session="main", u=url: u)
        assert _client(REMOTE).get("/current?session=main", follow_redirects=False).status_code == 400, url
    monkeypatch.setattr(server_app, "get_current_url", lambda session="main": "https://notes.owner.woltspace.app/")
    assert _client(REMOTE).get("/current?session=main", follow_redirects=False).status_code == 302


# --- The UI chooses frame flags from the resolved host, before loading ---------

def test_wolt_page_frames_the_resolved_site_host_or_the_opaque_lodge_copy():
    source = (ROOT / "public" / "static" / "wolt-page.js").read_text()
    assert "fetchJSON(`/sites/${encodeURIComponent(name)}`,{})" in source
    assert "site.site_url ? { src: site.site_url, sandbox: 'allow-scripts allow-same-origin' }" in source
    assert "{ src: `/wolt/${encodeURIComponent(name)}/site/`, sandbox: 'allow-scripts' }" in source
    assert source.count("'allow-scripts allow-same-origin'") == 1
    render = source[source.index("function renderSite()"):]
    render = render[:render.index("\n")]
    assert "const t=siteTarget()" in render and "frame.setAttribute('sandbox',t.sandbox);frame.src=t.src" in render
    assert "link('⤢ Expand',t.src," in render


def test_split_view_resolves_before_it_frames():
    source = (ROOT / "templates" / "tui.html").read_text()
    assert "fetch('/sites/resolve?path=' + encodeURIComponent(url))" in source
    assert source.count("'allow-scripts allow-same-origin'") == 1
    same_origin = source.index("preview.setAttribute('sandbox', 'allow-scripts allow-same-origin')")
    guard = source.index("if (siteUrl) {")
    assert guard < same_origin < source.index("preview.src = siteUrl")
    assert "OPAQUE_VIEWS = /^\\/(wolt\\/[^/]+\\/site([/?#]|$)|history\\/)/" in source
    assert "preview.setAttribute('sandbox', 'allow-scripts');" in source
    assert "preview.removeAttribute('sandbox')" in source
    load_view = source[source.index("async function loadView("):source.index("function setCurrent(")]
    assert load_view.index("await resolveSite(url)") < load_view.index("frameView(")
    assert "preview.src =" not in load_view  # every src goes through frameView, with its flags



def test_split_view_treats_query_and_fragment_spellings_as_site_candidates():
    html = (ROOT / "templates" / "tui.html").read_text()
    assert r"site([/?#]|$)" in html
    assert r"localhost(:\d+)?([/?#]|$)" in html
