"""The Mill: apps on origins of their own, behind the lodge's one port. Its
socket and process, the static file server a static app uses, and what stays
on the lodge's origin: wolt sites, sparks and tool pages."""

import socket
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_auth
import server.app as server_app
import server.mill as mill_server

APP = "notes.localhost:7777"
OWNER = {mill_server.IDENTITY_HEADER: mill_server.OWNER}


@pytest.fixture
def wolts(tmp_path):
    """A wolt with a site, and a static app beside it."""
    site = tmp_path / "softie" / "wolt" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html><body>home</body></html>")
    (site / "data.txt").write_text("0123456789")
    app = tmp_path / "apps" / "notes"
    (app / "sub").mkdir(parents=True)
    (app / "index.html").write_text("<html><body>notes app</body></html>")
    (app / "sub" / "index.html").write_text("<p>sub</p>")
    (app / "data.txt").write_text("0123456789")
    (tmp_path / "softie" / "wolt" / "memory").mkdir()
    (tmp_path / "softie" / "wolt" / "memory" / "secret.md").write_text("private")
    (app / "leak.md").symlink_to(tmp_path / "softie" / "wolt" / "memory" / "secret.md")
    return tmp_path


class StaticApps:
    """The lodge's apps as the Mill sees them: one static app, nothing running."""

    def __init__(self, root):
        self.root = root

    def running_apps(self):
        return []

    def get_app(self, name):
        return SimpleNamespace(can_start=lambda: False) if name == "notes" else None

    def app_dir(self, name):
        return self.root / "apps" / name


def _mill(wolts) -> TestClient:
    return TestClient(mill_server.Mill(wolts, apps=StaticApps(wolts)), base_url=f"http://{APP}")


# --- A static app's files ------------------------------------------------------------------

def test_the_owner_gets_files_with_the_static_policy(wolts):
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


def test_pages_are_served_as_written(wolts):
    page = _mill(wolts).get("/", headers=OWNER).text
    assert page == "<html><body>notes app</body></html>"


@pytest.mark.parametrize("path", [
    "/leak.md", "/%2e%2e/%2e%2e/softie/wolt/memory/secret.md", "/..%2f..%2fsoftie%2fwolt%2fmemory%2fsecret.md",
    "/sub/../../../softie/wolt/memory/secret.md", "/missing.html", "/data.txt%00",
])
def test_nothing_outside_the_app_is_served(wolts, path):
    response = _mill(wolts).get(path, headers=OWNER)
    assert response.status_code == 404
    assert "private" not in response.text


def test_a_folder_redirects_to_its_slash_and_serves_its_index(wolts):
    client = _mill(wolts)
    redirect = client.get("/sub", headers=OWNER, follow_redirects=False)
    assert redirect.status_code == 308 and redirect.headers["location"] == "/sub/"
    assert client.get("/sub/", headers=OWNER).text.startswith("<p>sub</p>")


def test_ranges_and_head(wolts):
    client = _mill(wolts)
    part = client.get("/data.txt", headers={**OWNER, "range": "bytes=2-4"})
    assert part.status_code == 206 and part.text == "234"
    head = client.head("/data.txt", headers=OWNER)
    assert head.status_code == 200 and head.headers["content-length"] == "10" and head.content == b""


def test_a_static_app_is_read_only(wolts):
    assert _mill(wolts).post("/data.txt", headers=OWNER).status_code == 405


def test_unknown_hosts_are_not_found(wolts):
    for host in ("localhost:7777", "softie.sites.localhost:7777", "ghost.localhost:7777"):
        client = TestClient(mill_server.Mill(wolts, apps=StaticApps(wolts)), base_url=f"http://{host}")
        assert client.get("/", headers=OWNER).status_code == 404


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


def test_a_container_keeps_the_socket_off_its_data_root(monkeypatch, tmp_path):
    assert mill_server.socket_path(tmp_path) == tmp_path / ".space" / "run" / "mill" / "mill.sock"
    monkeypatch.setenv("WOLTSPACE_MILL_SOCKET", "/tmp/woltspace-mill/mill.sock")
    assert mill_server.socket_path(tmp_path) == Path("/tmp/woltspace-mill/mill.sock")


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
    server = uvicorn.Server(uvicorn.Config(mill_server.Mill(wolts, apps=StaticApps(wolts)), uds=str(path),
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
    yield wolts
    server.should_exit = True
    thread.join(5)


def _lodge(host: str = "localhost:7777", client=("127.0.0.1", 50000)) -> TestClient:
    return TestClient(server_app.app, base_url=f"http://{host}", client=client)


def test_the_lodge_port_serves_an_app_through_the_mill(running_mill):
    response = _lodge(APP).get("/data.txt")
    assert response.status_code == 200 and response.text == "0123456789"
    assert response.headers["content-security-policy"].startswith("sandbox allow-scripts allow-same-origin")
    assert _lodge().get("/version").status_code == 200  # the lodge is still itself


def test_a_site_host_is_no_longer_forwarded(running_mill):
    response = _lodge("softie.sites.localhost:7777").get("/data.txt")
    assert response.status_code != 200 and "0123456789" not in response.text


def test_a_wolts_site_is_the_lodges_own_page(running_mill, monkeypatch):
    import sites
    monkeypatch.setattr(sites, "WOLTS_DIR", running_mill)
    page = _lodge().get("/wolt/softie/site/?tab=2")
    assert page.status_code == 200 and "home" in page.text
    assert "/wolt/softie/site/livereload" in page.text
    assert _lodge().get("/sites").json() == [{"wolt": "softie", "url": "/wolt/softie/site/"}]
    assert "location" not in _lodge().get("/sites/open?wolt=softie", follow_redirects=False).headers


def test_a_stopped_mill_leaves_the_lodge_up(wolts, monkeypatch):
    monkeypatch.setattr(server_app, "WOLTS_DIR", wolts)
    monkeypatch.setattr(server_app.mill_server, "socket_path", lambda _root: _short_dir() / "none.sock")
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    assert _lodge(APP).get("/data.txt").status_code == 502
    assert _lodge().get("/version").status_code == 200


# --- With lodge auth on --------------------------------------------------------------------

@pytest.fixture
def locked(running_mill, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    return running_mill, lodge_auth.ensure_keys(running_mill)


def _paired(root) -> TestClient:
    client = _lodge()
    client.get(f"/pair?code={lodge_auth.new_pairing_code(root)}")
    return client


def test_with_auth_on_a_site_needs_a_paired_browser(locked, monkeypatch):
    import sites
    root, keys = locked
    monkeypatch.setattr(sites, "WOLTS_DIR", root)
    assert _lodge().get("/wolt/softie/site/").status_code == 401
    assert _lodge().get("/wolt/softie/site/", headers={"x-woltspace-key": keys["owner"]}).status_code == 200
    page = _paired(root).get("/wolt/softie/site/")
    assert page.status_code == 200 and "home" in page.text


def test_static_app_files_are_not_served_on_the_lodges_origin(locked, monkeypatch):
    root, _ = locked
    app = root / "apps" / "board"
    app.mkdir(parents=True)
    (app / "woltspace.json").write_text('{"name": "board", "port": 4100, "keeper": "softie"}')
    (app / "index.html").write_text("<script>fetch('/sessions')</script>")
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    # The lodge's own answer, not wherever a redirect leads: on this
    # machine `board` may be a real app the lodge would send the browser to.
    response = _paired(root).get("/app/board/", follow_redirects=False)
    assert response.status_code in (303, 404) and "<script>" not in response.text


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
    grant = lodge_auth.new_app_grant(root, "app:notes")
    code = lodge_auth.new_pairing_code(root)
    lodge_auth.forget_browsers(root)
    assert lodge_auth.redeem_app_grant(root, "app:notes", grant) is None
    assert lodge_auth.redeem_pairing_code(root, code) is None


def test_forgetting_browsers_closes_app_sessions(locked):
    root, _ = locked
    token = lodge_auth.redeem_app_grant(root, "app:notes", lodge_auth.new_app_grant(root, "app:notes"))
    assert lodge_auth.app_session_ok(root, "app:notes", token)
    lodge_auth.forget_browsers(root)
    assert not lodge_auth.app_session_ok(root, "app:notes", token)
