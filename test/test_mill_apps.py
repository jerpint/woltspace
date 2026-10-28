"""Apps through the Mill: `<app>.localhost` on the lodge's port, proxied by the Mill."""

import asyncio
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps as apps_lib
import lodge_auth
import server.app as server_app
import server.mill as mill_server
from server.content_hosts import parse_app_host

APP = "board.localhost:7777"
OWNER = {mill_server.IDENTITY_HEADER: mill_server.OWNER}


# --- A real app ------------------------------------------------------------------------

class _Seen:
    requests: list = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _reply(self, body: bytes):
        _Seen.requests.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "app=1; Path=/; Domain=.localhost")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._reply(b"hello from board")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self._reply(b"got " + str(len(self.rfile.read(length))).encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def board():
    """A running app on a loopback port, and the Mill's view of the lodge's apps."""
    _Seen.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    yield port
    server.shutdown()


class FakeApps:
    def __init__(self, tmp_path, running=None, static=None):
        self.tmp_path, self.running, self.static = tmp_path, running or {}, static or {}

    def running_apps(self):
        return [{"name": n, "port": p} for n, p in self.running.items()]

    def get_app(self, name):
        if name in self.running:
            return SimpleNamespace(can_start=lambda: True)
        if name in self.static:
            return SimpleNamespace(can_start=lambda: False)
        if name == "stopped":
            return SimpleNamespace(can_start=lambda: True)
        return None

    def app_dir(self, name):
        return self.static[name]


def _mill(tmp_path, **kw) -> TestClient:
    return TestClient(mill_server.Mill(tmp_path, apps=FakeApps(tmp_path, **kw)),
                      base_url=f"http://{APP}")


# --- Naming ----------------------------------------------------------------------------

@pytest.mark.parametrize("host,name", [("board.localhost:7777", "board"), ("my_app.localhost", "my_app"),
                                       ("BOARD.localhost", "board")])
def test_app_hosts_name_an_app(host, name):
    assert parse_app_host(host).name == name


@pytest.mark.parametrize("host", ["localhost:7777", "softie.sites.localhost", "a.b.localhost",
                                  "1app.localhost", "board.woltspace.test", "-x.localhost"])
def test_anything_else_is_not_an_app(host):
    assert parse_app_host(host) is None


# --- The Mill alone ----------------------------------------------------------------------

def test_the_mill_proxies_the_owner_to_a_running_app(tmp_path, board):
    client = _mill(tmp_path, running={"board": board})
    response = client.get("/hi?x=1", headers={**OWNER, "cookie": "app=1"})
    assert response.status_code == 200 and response.text == "hello from board"
    method, path, sent = _Seen.requests[-1]
    assert path == "/hi?x=1" and sent["host"] == f"localhost:{board}" and sent["cookie"] == "app=1"
    assert mill_server.IDENTITY_HEADER not in sent
    assert response.headers["set-cookie"] == "app=1; Path=/"


def test_an_app_needs_the_lodges_word_too(tmp_path, board):
    assert _mill(tmp_path, running={"board": board}).get("/").status_code == 403


def test_uploads_reach_the_app(tmp_path, board):
    response = _mill(tmp_path, running={"board": board}).post(
        "/upload", content=b"x" * 100000, headers={**OWNER, "origin": f"http://{APP}"})
    assert response.text == "got 100000"


def test_a_cross_site_write_to_an_app_is_refused(tmp_path, board):
    client = _mill(tmp_path, running={"board": board})
    assert client.post("/x", headers={**OWNER, "origin": "http://evil.example"}).status_code == 403
    assert client.post("/x", headers={**OWNER, "sec-fetch-site": "cross-site"}).status_code == 403


def test_stopped_and_unknown_apps(tmp_path):
    assert _mill(tmp_path).get("/", headers=OWNER).status_code == 404
    stopped = TestClient(mill_server.Mill(tmp_path, apps=FakeApps(tmp_path)),
                         base_url="http://stopped.localhost:7777")
    assert stopped.get("/", headers=OWNER).status_code == 503


def test_a_static_app_is_served_by_the_mill_without_its_dotfiles(tmp_path):
    app_dir = tmp_path / "apps" / "board"
    (app_dir / ".git").mkdir(parents=True)
    (app_dir / ".git" / "config").write_text("secret")
    (app_dir / ".env").write_text("KEY=1")
    (app_dir / "index.html").write_text("<h1>board</h1>")
    client = _mill(tmp_path, static={"board": app_dir})
    page = client.get("/", headers=OWNER)
    assert page.status_code == 200 and "<h1>board</h1>" in page.text
    assert "/_woltspace/livereload" not in page.text
    for path in ("/.env", "/.git/config", "/%2egit/config"):
        assert client.get(path, headers=OWNER).status_code == 404
    assert client.post("/", headers=OWNER).status_code == 405


def test_a_static_apps_dist_is_its_root(tmp_path):
    app_dir = tmp_path / "apps" / "board"
    (app_dir / "dist").mkdir(parents=True)
    (app_dir / "dist" / "index.html").write_text("built")
    (app_dir / "src.txt").write_text("source")
    client = _mill(tmp_path, static={"board": app_dir})
    assert client.get("/", headers=OWNER).text.startswith("built")
    assert client.get("/src.txt", headers=OWNER).status_code == 404


def test_a_symlinked_static_root_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "index.html").write_text("outside")
    link = tmp_path / "apps" / "board"
    link.parent.mkdir()
    link.symlink_to(elsewhere)
    assert mill_server.static_app_root(link) is None
    assert _mill(tmp_path, static={"board": link}).get("/", headers=OWNER).status_code == 404


@pytest.fixture
def echo_app():
    from websockets.asyncio.server import serve
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    ready, stop = threading.Event(), {}

    async def echo(conn):
        async for msg in conn:
            await conn.send(msg)

    async def main():
        loop = asyncio.get_running_loop()
        stop["future"], stop["loop"] = loop.create_future(), loop
        async with serve(echo, "127.0.0.1", port, subprotocols=["vite-hmr"]):
            ready.set()
            await stop["future"]

    threading.Thread(target=lambda: asyncio.run(main()), daemon=True).start()
    assert ready.wait(5)
    yield port
    stop["loop"].call_soon_threadsafe(stop["future"].set_result, None)


def test_app_websockets_go_through_the_mill(tmp_path, echo_app):
    client = _mill(tmp_path, running={"board": echo_app})
    with client.websocket_connect("/hmr", subprotocols=["vite-hmr"],
                                  headers={**OWNER, "host": APP, "origin": f"http://{APP}"}) as ws:
        assert ws.accepted_subprotocol == "vite-hmr"
        ws.send_bytes(b"\x00\x01")
        assert ws.receive_bytes() == b"\x00\x01"
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/hmr", headers={**OWNER, "host": APP,
                                                       "origin": "http://evil.example"}):
            pass


# --- Through the lodge's port -------------------------------------------------------------

def _short_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="mill", dir="/tmp"))


@pytest.fixture
def lodge_with_board(tmp_path, monkeypatch, board, echo_app):
    import uvicorn
    path = _short_dir() / "mill.sock"
    fake = FakeApps(tmp_path, running={"board": board, "hmr": echo_app})
    server = uvicorn.Server(uvicorn.Config(mill_server.Mill(tmp_path, apps=fake), uds=str(path),
                                           log_level="error", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if path.exists():
            break
        time.sleep(0.05)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app.mill_server, "socket_path", lambda _root: path)
    monkeypatch.delenv("WOLTSPACE_LODGE_AUTH", raising=False)
    monkeypatch.setattr(server_app, "_pairing_failures", lodge_auth.FailureLimit())
    monkeypatch.setattr(server_app, "get_app", lambda name: fake.get_app(name))
    yield tmp_path
    server.should_exit = True
    thread.join(5)


def _lodge(host=APP, client=("127.0.0.1", 50000)) -> TestClient:
    return TestClient(server_app.app, base_url=f"http://{host}", client=client)


def test_an_app_on_the_lodges_port_goes_through_the_mill(lodge_with_board):
    response = _lodge().post("/upload", content=b"y" * 5000, headers={
        "origin": f"http://{APP}", "cookie": "app=1; woltspace-lodge-7777=t; woltspace-site=s",
        "x-woltspace-key": "k", mill_server.IDENTITY_HEADER: "forged"})
    assert response.status_code == 200 and response.text == "got 5000"
    _method, _path, sent = _Seen.requests[-1]
    assert sent["cookie"] == "app=1"
    assert "x-woltspace-key" not in sent and mill_server.IDENTITY_HEADER not in sent


def test_a_visitor_through_a_tunnel_is_not_the_owner_of_a_local_app(lodge_with_board):
    assert _lodge().get("/", headers={"x-forwarded-for": "203.0.113.9"}).status_code == 403


def test_app_websockets_go_through_the_lodge_and_the_mill(lodge_with_board):
    host = "hmr.localhost:7777"
    with _lodge(host).websocket_connect("/ws", subprotocols=["vite-hmr"],
                                        headers={"host": host, "origin": f"http://{host}"}) as ws:
        assert ws.accepted_subprotocol == "vite-hmr"
        ws.send_text("ping")
        assert ws.receive_text() == "ping"


def test_with_auth_on_an_app_opens_from_the_lodge(lodge_with_board, monkeypatch):
    root = lodge_with_board
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    keys = lodge_auth.ensure_keys(root)
    refused = _lodge().get("/")
    assert refused.status_code == 401 and "/apps/open?app=board" in refused.text
    assert _lodge("localhost:7777").get(
        "/apps/open?app=board", headers={"x-woltspace-key": keys["wolt"]}).status_code == 403
    opened = _lodge("localhost:7777").get("/apps/open?app=board&path=/x", follow_redirects=False,
                                          headers={"x-woltspace-key": keys["owner"]})
    grant = opened.headers["location"]
    assert grant.startswith("http://board.localhost:7777/_woltspace/grant?code=")
    app = _lodge()
    granted = app.get(grant.split("7777", 1)[1], follow_redirects=False)
    assert granted.headers["location"] == "/x"
    assert app.get("/").text == "hello from board"
    # A site's grant doesn't open an app of the same name, and the reverse.
    site_code = lodge_auth.new_site_grant(root, "board")
    assert _lodge().get(f"/_woltspace/grant?code={site_code}").status_code == 403


def test_auth_off_opening_an_app_just_redirects(lodge_with_board):
    opened = _lodge("localhost:7777").get("/apps/open?app=board&path=/x", follow_redirects=False)
    assert opened.headers["location"] == "http://board.localhost:7777/x"


# --- Ports -----------------------------------------------------------------------------------

def test_an_app_keeps_its_manifest_port_when_free():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
    assert apps_lib.choose_port(free, set()) == free


def test_an_app_gets_another_port_when_its_own_is_held():
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    try:
        port = held.getsockname()[1]
        chosen = apps_lib.choose_port(port, set())
        assert chosen != port
        assert apps_lib.choose_port(chosen, {chosen}) != chosen  # another app's record counts too
    finally:
        held.close()
