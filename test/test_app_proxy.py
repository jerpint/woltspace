"""The lodge's proxy to apps: what reaches an app, and what comes back."""

import asyncio
import gzip
import socket
import sys
import threading
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps as apps_lib
import server.app as server_app
from server.app_proxy import (
    client_response_headers,
    upstream_request_headers,
    websocket_upstream_headers,
)

APP_HOST = "notes.localhost:7777"


def _client() -> TestClient:
    return TestClient(server_app.app, base_url=f"http://{APP_HOST}")


def _running(monkeypatch, port: int) -> None:
    monkeypatch.setattr(apps_lib, "running_apps", lambda: [{"name": "notes", "port": port}])


class _Stream(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        yield self.body


def _upstream(monkeypatch, handler) -> list[httpx.Request]:
    """Route the proxy's HTTP client to `handler`; return the requests it saw."""
    seen: list[httpx.Request] = []

    async def record(request: httpx.Request) -> httpx.Response:
        await request.aread()
        seen.append(request)
        reply = handler(request)
        # A live app streams its reply; a mock built from bytes arrives already
        # read, which the proxy's raw relay can't use.
        return httpx.Response(reply.status_code, headers=reply.headers.multi_items(),
                              stream=_Stream(b"".join(reply.stream)))

    monkeypatch.setattr(
        server_app, "_app_http_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(record)),
    )
    _running(monkeypatch, 4999)
    return seen


# --- Requests to the app ---

def test_access_credentials_never_reach_the_app(monkeypatch):
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200, text="ok"))

    response = _client().get("/api", headers={
        "cf-access-jwt-assertion": "jwt-for-the-owner",
        "cf-access-token": "jwt-again",
        "cf-access-client-id": "id",
        "cf-access-client-secret": "secret",
        "cookie": "CF_Authorization=jwt-cookie; app_session=abc; CF_AppSession=x",
    })

    assert response.status_code == 200
    sent = seen[0].headers
    for name in ("cf-access-jwt-assertion", "cf-access-token", "cf-access-client-id", "cf-access-client-secret"):
        assert name not in sent
    assert sent["cookie"] == "app_session=abc"
    assert "jwt" not in str(seen[0].headers.raw)


def test_the_app_keeps_its_own_authorization_and_cookies(monkeypatch):
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200))

    _client().get("/api/versions", headers={
        "authorization": "Bearer app-invite-token",
        "cookie": "theme=dark; app_session=abc",
        "origin": f"http://{APP_HOST}",
    })

    sent = seen[0].headers
    assert sent["authorization"] == "Bearer app-invite-token"
    assert sent["cookie"] == "theme=dark; app_session=abc"
    assert sent["origin"] == f"http://{APP_HOST}"
    assert sent["host"] == "localhost:4999"


def _jwt(claims: dict) -> str:
    import base64, json
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    return f"{enc({'alg': 'RS256', 'kid': 'k'})}.{enc(claims)}.c2ln"


@pytest.mark.parametrize("value", [
    '{"cf-access-client-id":"id.access","cf-access-client-secret":"secret"}',
    "Bearer " + _jwt({"iss": "https://team.cloudflareaccess.com", "aud": ["a"]}),
    _jwt({"iss": "https://team.cloudflareaccess.com/"}),
])
def test_access_credentials_in_authorization_never_reach_the_app(value):
    for helper in (upstream_request_headers, websocket_upstream_headers):
        out = helper([("Authorization", value)], 4999)
        assert all(name.lower() != "authorization" for name, _ in out)
        assert "secret" not in str(out) and "cloudflareaccess" not in str(out)


@pytest.mark.parametrize("value", [
    "Bearer app-invite-token",
    "Bearer " + _jwt({"iss": "https://auth.example.com"}),
    "Basic dXNlcjpwYXNz",
])
def test_an_apps_own_authorization_passes(value):
    out = upstream_request_headers([("Authorization", value)], 4999)
    assert ("Authorization", value) in out


def test_a_cookie_header_of_only_access_cookies_is_dropped():
    out = upstream_request_headers([("Cookie", "CF_Authorization=jwt")], 4999)
    assert [n.lower() for n, _ in out] == ["host"]


def test_hop_by_hop_and_connection_named_headers_are_not_forwarded():
    out = upstream_request_headers([
        ("Connection", "keep-alive, X-Secret-Hop"),
        ("X-Secret-Hop", "1"),
        ("Keep-Alive", "timeout=5"),
        ("Proxy-Authorization", "Basic abc"),
        ("TE", "trailers"),
        ("Upgrade", "h2c"),
        ("Host", "evil.example"),
        ("X-App", "kept"),
    ], 4999)
    assert out == [("host", "localhost:4999"), ("X-App", "kept")]


def test_repeated_request_headers_all_reach_the_app():
    out = upstream_request_headers([("X-Tag", "a"), ("X-Tag", "b")], 4999)
    assert out[1:] == [("X-Tag", "a"), ("X-Tag", "b")]


def test_a_request_body_reaches_the_app_whole(monkeypatch):
    seen = _upstream(monkeypatch, lambda r: httpx.Response(201))
    body = b"x" * (3 * 1024 * 1024)

    response = _client().post("/upload", content=body, headers={"origin": f"http://{APP_HOST}"})

    assert response.status_code == 201
    assert seen[0].content == body
    assert seen[0].headers["content-length"] == str(len(body))
    assert "transfer-encoding" not in seen[0].headers


def test_a_request_without_a_body_sends_none(monkeypatch):
    seen = _upstream(monkeypatch, lambda r: httpx.Response(200))

    _client().get("/")

    assert "transfer-encoding" not in seen[0].headers
    assert seen[0].content == b""


# --- Responses from the app ---

def test_every_set_cookie_comes_back_host_only(monkeypatch):
    _upstream(monkeypatch, lambda r: httpx.Response(200, headers=[
        ("set-cookie", "a=1; Path=/; Domain=.woltspace.test; HttpOnly"),
        ("set-cookie", "b=2; Path=/"),
        ("set-cookie", "CF_Authorization=forged; Domain=.woltspace.test"),
    ]))

    response = _client().get("/")

    cookies = response.headers.get_list("set-cookie")
    assert cookies == ["a=1; Path=/; HttpOnly", "b=2; Path=/"]


def test_compressed_responses_are_relayed_as_sent(monkeypatch):
    payload = b"hello " * 2000
    compressed = gzip.compress(payload)
    _upstream(monkeypatch, lambda r: httpx.Response(200, content=compressed, headers={
        "content-encoding": "gzip", "content-length": str(len(compressed)),
    }))

    response = _client().get("/big.js")

    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["content-length"] == str(len(compressed))
    assert response.content == payload  # the test client decodes, as a browser does


def test_response_hop_by_hop_headers_are_dropped():
    out = client_response_headers([
        ("Connection", "close, X-Hop"), ("X-Hop", "1"), ("Keep-Alive", "timeout=5"),
        ("Content-Type", "text/plain"),
    ])
    assert out == [("Content-Type", "text/plain")]


def test_a_range_reply_keeps_its_status_and_length(monkeypatch):
    _upstream(monkeypatch, lambda r: httpx.Response(206, content=b"0123", headers={
        "content-range": "bytes 0-3/100", "content-length": "4",
    }))

    response = _client().get("/video.mp4", headers={"range": "bytes=0-3"})

    assert response.status_code == 206
    assert response.headers["content-length"] == "4"
    assert response.content == b"0123"


# --- WebSockets ---

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def echo_app():
    """A real WebSocket app on 127.0.0.1 that echoes frames and records handshakes."""
    from websockets.asyncio.server import serve

    port = _free_port()
    handshakes: list = []
    ready = threading.Event()
    stop: dict = {}

    async def echo(conn):
        handshakes.append(conn.request.headers)
        async for msg in conn:
            await conn.send(msg)

    async def main():
        loop = asyncio.get_running_loop()
        stop["future"] = loop.create_future()
        stop["loop"] = loop
        async with serve(echo, "127.0.0.1", port):
            ready.set()
            await stop["future"]

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert ready.wait(5)
    yield port, handshakes
    stop["loop"].call_soon_threadsafe(stop["future"].set_result, None)
    thread.join(5)


def test_websocket_frames_keep_their_type_both_ways(echo_app, monkeypatch):
    port, _ = echo_app
    _running(monkeypatch, port)

    with _client().websocket_connect("/hmr", headers={
        "host": APP_HOST, "origin": f"http://{APP_HOST}",
    }) as ws:
        ws.send_bytes(b"\x00\xffbinary")
        assert ws.receive_bytes() == b"\x00\xffbinary"
        ws.send_text("text")
        assert ws.receive_text() == "text"


def test_websocket_handshake_carries_app_cookies_but_no_access_credentials(echo_app, monkeypatch):
    port, handshakes = echo_app
    _running(monkeypatch, port)

    with _client().websocket_connect("/hmr", headers={
        "host": APP_HOST,
        "origin": f"http://{APP_HOST}",
        "cookie": "CF_Authorization=jwt-cookie; app_session=abc",
        "cf-access-jwt-assertion": "jwt-for-the-owner",
    }) as ws:
        ws.send_text("ping")
        assert ws.receive_text() == "ping"

    sent = handshakes[0]
    assert sent.get_all("Host") == [f"localhost:{port}"]
    assert sent["Cookie"] == "app_session=abc"
    assert "cf-access-jwt-assertion" not in sent
    assert "jwt" not in str(sent)


def test_websocket_handshake_headers_leave_out_what_the_client_library_writes():
    out = websocket_upstream_headers([
        ("Host", APP_HOST), ("Sec-WebSocket-Key", "k"), ("Sec-WebSocket-Version", "13"),
        ("Sec-WebSocket-Protocol", "vite-hmr"), ("Origin", f"http://{APP_HOST}"),
    ], 4999)
    assert out == [("Origin", f"http://{APP_HOST}")]
