"""The app gateway is app-only across HTTP, websocket, and lifecycle seams."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request, WebSocket
from fastapi.responses import PlainTextResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.gateway as gateway
import server.app_proxy as app_proxy
import apps
from server.access import AccessTokenVerifier, load_access_settings
from server.gateway_settings import load_gateway_settings
from server import app as lodge
from woltspace.channel_supervisor import ChannelSupervisor, read_connector_report
from woltspace.channels import AppGatewayConnector, plan_connectors
from woltspace.layout import RuntimeLayout


def _key_and_config(root: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (root / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app",
        "access": {
            "team_domain": "team.cloudflareaccess.com",
            "lodge_aud": "lodge-aud",
            "apps_aud": "apps-aud",
            "owner_email": "owner@example.com",
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


def _token(key, aud, email="owner@example.com"):
    now = int(time.time())
    return jwt.encode({
        "iss": "https://team.cloudflareaccess.com", "aud": aud,
        "email": email, "iat": now, "exp": now + 300,
    }, key, algorithm="RS256", headers={"kid": "key"})


def _configure(tmp_path, monkeypatch):
    key, verifier = _key_and_config(tmp_path)
    monkeypatch.setattr(gateway, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(gateway, "access_token_verifier", verifier)
    return key


async def _get(path, *, host, token=None, client=("127.0.0.1", 123), extra_headers=None):
    headers = {"host": host}
    if token:
        headers["cf-access-jwt-assertion"] = token
    headers.update(extra_headers or {})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app, client=client),
        base_url="https://gateway.test",
    ) as client:
        return await client.get(path, headers=headers)


def test_gateway_valid_owner_and_shared_email_reach_http_app(tmp_path, monkeypatch):
    key = _configure(tmp_path, monkeypatch)
    seen = []

    async def proxy(request, name):
        seen.append((name, request.url.path))
        return PlainTextResponse("APP_OK")

    monkeypatch.setattr(gateway, "proxy_app_http", proxy)
    owner = asyncio.run(_get(
        "/hello", host="notes.owner.woltspace.app", token=_token(key, "apps-aud"),
    ))
    (tmp_path / ".space" / "platform").mkdir(parents=True)
    (tmp_path / ".space" / "platform" / "app-sharing.json").write_text(json.dumps({
        "notes": ["friend@example.com"],
    }))
    friend = asyncio.run(_get(
        "/hello", host="notes.owner.woltspace.app",
        token=_token(key, "apps-aud", "friend@example.com"),
    ))

    assert owner.text == friend.text == "APP_OK"
    assert seen == [("notes", "/hello"), ("notes", "/hello")]


def test_gateway_rejects_unlisted_missing_garbage_and_lodge_audience_http(tmp_path, monkeypatch):
    key = _configure(tmp_path, monkeypatch)
    cases = [
        None,
        "garbage",
        _token(key, "lodge-aud"),
        _token(key, "apps-aud", "stranger@example.com"),
    ]
    for token in cases:
        response = asyncio.run(_get(
            "/", host="notes.owner.woltspace.app", token=token,
        ))
        assert response.status_code == 403


@pytest.mark.parametrize("token_kind", ["missing", "garbage", "lodge", "unlisted"])
def test_gateway_rejects_bad_app_websocket_identity(tmp_path, monkeypatch, token_kind):
    key = _configure(tmp_path, monkeypatch)
    tokens = {
        "missing": None, "garbage": "garbage",
        "lodge": _token(key, "lodge-aud"),
        "unlisted": _token(key, "apps-aud", "stranger@example.com"),
    }
    headers = {
        "host": "notes.owner.woltspace.app",
        "origin": "https://notes.owner.woltspace.app",
    }
    if tokens[token_kind]:
        headers["cf-access-jwt-assertion"] = tokens[token_kind]
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(gateway.app).websocket_connect("/socket", headers=headers):
            pass
    assert exc.value.code == 1008


def test_gateway_valid_shared_websocket_reaches_app_proxy(tmp_path, monkeypatch):
    key = _configure(tmp_path, monkeypatch)
    platform = tmp_path / ".space" / "platform"
    platform.mkdir(parents=True)
    (platform / "app-sharing.json").write_text(json.dumps({"notes": ["friend@example.com"]}))
    seen = []

    async def proxy(ws, name, path):
        seen.append((name, path, ws.state.access_email))
        await ws.accept()
        await ws.close()

    monkeypatch.setattr(gateway, "proxy_app_websocket", proxy)
    with TestClient(gateway.app).websocket_connect(
        "/socket",
        headers={
            "host": "notes.owner.woltspace.app",
            "origin": "https://notes.owner.woltspace.app",
            "cf-access-jwt-assertion": _token(key, "apps-aud", "friend@example.com"),
        },
    ):
        pass
    assert seen == [("notes", "/socket", "friend@example.com")]


def test_gateway_rejects_foreign_websocket_origin(tmp_path, monkeypatch):
    key = _configure(tmp_path, monkeypatch)
    with pytest.raises(WebSocketDisconnect) as exc:
        with TestClient(gateway.app).websocket_connect(
            "/socket",
            headers={
                "host": "notes.owner.woltspace.app",
                "origin": "https://evil.example",
                "cf-access-jwt-assertion": _token(key, "apps-aud"),
            },
        ):
            pass
    assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/tui", "/sessions", "/settings"])
def test_gateway_has_no_lodge_routes(path, tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    response = asyncio.run(_get(path, host="owner.woltspace.com"))
    assert response.status_code == 404


def test_stopped_and_unknown_apps_have_bounded_pages(tmp_path, monkeypatch):
    key = _configure(tmp_path, monkeypatch)
    import server.app_proxy as proxy
    monkeypatch.setattr(proxy, "running_app", lambda _name: None)
    monkeypatch.setattr(proxy, "get_app", lambda name: object() if name == "notes" else None)
    stopped = asyncio.run(_get(
        "/", host="notes.owner.woltspace.app", token=_token(key, "apps-aud"),
    ))
    unknown = asyncio.run(_get(
        "/", host="missing.owner.woltspace.app", token=_token(key, "apps-aud"),
    ))
    assert stopped.status_code == 503
    assert "stopped" in stopped.text
    assert unknown.status_code == 404


def test_proxy_rewrites_domain_cookies_to_host_only_and_preserves_multiples(monkeypatch):
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})
    upstream = httpx.Response(
        200,
        headers=[
            ("set-cookie", "wide=1; Domain=.woltspace.app; Path=/; HttpOnly"),
            ("set-cookie", "plain=2; Path=/; Secure"),
            ("set-cookie", "parent=3; domain=woltspace.app; SameSite=Lax"),
        ],
        content=b"ok",
        request=httpx.Request("GET", "http://127.0.0.1:4321/"),
    )

    class Client:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, *_args, **_kwargs):
            return upstream

        async def aclose(self):
            return None

    monkeypatch.setattr(app_proxy.httpx, "AsyncClient", Client)
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "raw_path": b"/", "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 123), "server": ("notes.example", 443),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = asyncio.run(app_proxy.proxy_app_http(Request(scope, receive), "notes"))

    assert response.headers.getlist("set-cookie") == [
        "wide=1; Path=/; HttpOnly",
        "plain=2; Path=/; Secure",
        "parent=3; SameSite=Lax",
    ]


def test_gateway_is_present_without_apps_domain(tmp_path):
    layout = RuntimeLayout(tmp_path, ROOT)
    direct = AppGatewayConnector().plan(layout, {"WOLTSPACE_ENTRYPOINT": "1"})
    plans = plan_connectors(layout, {"WOLTSPACE_ENTRYPOINT": "1"})

    assert direct.enabled
    assert "7117" in direct.detail
    assert any(plan.name == "app-gateway" for plan in plans)


@pytest.mark.parametrize(("lodge_port", "gateway_port"), [(7777, 7117), (7778, 7118)])
def test_gateway_default_port_tracks_lodge_port(
    tmp_path, lodge_port, gateway_port,
):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app",
    }))
    layout = RuntimeLayout(tmp_path, ROOT, port=lodge_port)
    plan = AppGatewayConnector().plan(layout, {"WOLTSPACE_ENTRYPOINT": "1"})
    assert plan.enabled
    assert ("--host", "127.0.0.1") == (plan.command[4], plan.command[5])
    assert ("--port", str(gateway_port)) == (plan.command[6], plan.command[7])
    assert load_gateway_settings(tmp_path, lodge_port=lodge_port).port == gateway_port
    assert any(item.name == "app-gateway" for item in plan_connectors(
        layout, {"WOLTSPACE_ENTRYPOINT": "1"},
    ))


def test_container_gateway_binds_all_interfaces(tmp_path):
    plan = AppGatewayConnector().plan(
        RuntimeLayout(tmp_path, ROOT, isolation="external"),
        {"WOLTSPACE_ENTRYPOINT": "1"},
    )
    assert ("--host", "0.0.0.0") == (plan.command[4], plan.command[5])
    assert "0.0.0.0:7117" in plan.detail


def test_container_files_expose_and_publish_gateway_port():
    dockerfile = (ROOT / "container" / "Dockerfile").read_text()
    launcher = (ROOT / "woltspace").read_text()
    assert "EXPOSE 7777 7117" in dockerfile
    assert '-p "127.0.0.1:$app_gateway_port:$app_gateway_port"' in launcher
    assert '-e WOLTSPACE_APP_GATEWAY_PORT="$app_gateway_port"' in launcher
    assert "--entrypoint /usr/bin/python3" not in launcher
    assert "from woltspace.app_gateway_port import resolve_app_gateway_port" in launcher
    assert 'resolved_gateway_port=""' in launcher
    assert '|| resolved_gateway_port=""' in launcher
    assert "-p 127.0.0.1:7117:7117" in launcher
    assert '-p "127.0.0.1:${WOLTSPACE_PORT:-7777}:7777"' in launcher
    assert "-p 127.0.0.1:7777:7777" in launcher


def test_gateway_port_is_configurable(tmp_path):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app",
        "app_gateway": {"port": 4555},
    }))
    settings = load_gateway_settings(tmp_path)
    plan = AppGatewayConnector().plan(
        RuntimeLayout(tmp_path, ROOT), {"WOLTSPACE_ENTRYPOINT": "1"},
    )
    assert settings.port == 4555
    assert "4555" in plan.command


def test_gateway_port_collision_falls_back_without_breaking_plans(tmp_path, capsys):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app",
        "app_gateway": {"port": 7777},
    }))
    assert load_gateway_settings(tmp_path, lodge_port=7777).port == 7117
    plan = AppGatewayConnector().plan(
        RuntimeLayout(tmp_path, ROOT, port=7777), {"WOLTSPACE_ENTRYPOINT": "1"},
    )
    assert plan.command[7] == "7117"
    assert "7777" in capsys.readouterr().err


def test_gateway_port_changes_only_through_validated_lodge_route(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text("{}")
    monkeypatch.setattr(lodge, "WOLTS_DIR", tmp_path)

    async def post(value):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=lodge.app),
            base_url="http://localhost:7777",
        ) as client:
            return await client.post("/settings/app-gateway", json={"port": value})

    saved = asyncio.run(post(4555))
    invalid = asyncio.run(post(7777))
    blocked = asyncio.run(post(6667))
    assert saved.json() == {"ok": True, "port": 4555, "applies": "next lodge start"}
    assert invalid.status_code == 400
    assert blocked.status_code == 400
    assert json.loads((tmp_path / "woltspace.json").read_text()) == {
        "app_gateway": {"port": 4555},
    }


def test_app_start_always_reserves_gateway_port(
    tmp_path, monkeypatch,
):
    target = tmp_path / "apps" / "notes"
    target.mkdir(parents=True)
    (target / "woltspace.json").write_text(json.dumps({
        "name": "notes", "keeper": "n00b", "port": 7117,
        "start": "echo hello", "stack": "html",
    }))
    state = tmp_path / ".space" / "apps"
    for attr, value in (
        ("WOLTS_DIR", tmp_path), ("APPS_DIR", tmp_path / "apps"),
        ("LEGACY_PROJECTS_DIR", tmp_path / "projects"),
        ("_RUNNING_STATE_DIR", state),
    ):
        monkeypatch.setattr(apps, attr, value)
    monkeypatch.setenv("WOLTSPACE_PORT", "7777")
    monkeypatch.setattr(apps.subprocess, "Popen", lambda *_args, **_kwargs: type(
        "Process", (), {"pid": 12345},
    )())

    (tmp_path / "woltspace.json").write_text(json.dumps({
        "app_gateway": {"port": 7117},
    }))
    with pytest.raises(RuntimeError) as exc:
        apps.start_app("notes")
    assert str(exc.value) == (
        "port 7117 is used by the app gateway; "
        "change the app's port or the gateway port in Settings"
    )


def test_gateway_env_port_overrides_file_and_default(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "app_gateway": {"port": 4555},
    }))
    monkeypatch.setenv("WOLTSPACE_APP_GATEWAY_PORT", "4666")
    settings = load_gateway_settings(tmp_path, lodge_port=7777)
    plan = AppGatewayConnector().plan(
        RuntimeLayout(tmp_path, ROOT),
        {"WOLTSPACE_ENTRYPOINT": "1", "WOLTSPACE_APP_GATEWAY_PORT": "4777"},
    )
    assert settings.port == 4666
    assert plan.command[7] == "4777"


@pytest.mark.parametrize("value", ["nonsense", "6667", "10080"])
def test_bad_env_gateway_port_falls_back_and_lodge_plans_survive(tmp_path, value, capsys):
    plans = plan_connectors(RuntimeLayout(tmp_path, ROOT), {
        "WOLTSPACE_ENTRYPOINT": "1", "WOLTSPACE_APP_GATEWAY_PORT": value,
    })
    gateway_plan = next(plan for plan in plans if plan.name == "app-gateway")
    assert gateway_plan.enabled
    assert gateway_plan.command[7] == "7117"
    warning = capsys.readouterr().err
    assert value in warning
    assert "using 7117" in warning


def test_loopback_owner_opens_local_app_with_access_configured(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)

    async def proxy(_request, name):
        return PlainTextResponse(f"{name}:LOCAL_OK")

    monkeypatch.setattr(gateway, "proxy_app_http", proxy)
    response = asyncio.run(_get("/", host="notes.localhost:7117"))
    assert response.status_code == 200
    assert response.text == "notes:LOCAL_OK"


def test_localhost_app_requires_loopback_peer(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    monkeypatch.delenv("WOLTSPACE_ISOLATION", raising=False)
    response = asyncio.run(_get(
        "/", host="notes.localhost:7117", client=("192.0.2.10", 123),
    ))
    assert response.status_code == 403


def test_container_localhost_app_accepts_docker_bridge_peer(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")

    async def proxy(_request, name):
        return PlainTextResponse(f"{name}:CONTAINER_OK")

    monkeypatch.setattr(gateway, "proxy_app_http", proxy)
    response = asyncio.run(_get(
        "/", host="notes.localhost:7117", client=("192.168.65.1", 123),
    ))
    assert response.status_code == 200
    assert response.text == "notes:CONTAINER_OK"


def test_container_forwarded_localhost_is_still_refused(tmp_path, monkeypatch):
    _configure(tmp_path, monkeypatch)
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")
    response = asyncio.run(_get(
        "/", host="notes.localhost:7117", client=("192.168.65.1", 123),
        extra_headers={"x-forwarded-for": "203.0.113.4"},
    ))
    assert response.status_code == 403


@pytest.mark.parametrize("header", ["cf-connecting-ip", "x-forwarded-for"])
def test_forwarded_localhost_app_is_not_owner_local(tmp_path, monkeypatch, header):
    _configure(tmp_path, monkeypatch)
    response = asyncio.run(_get(
        "/", host="notes.localhost:7117", extra_headers={header: "203.0.113.4"},
    ))
    assert response.status_code == 403


def test_remote_app_host_without_access_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app",
    }))
    monkeypatch.setattr(gateway, "WOLTS_DIR", tmp_path)
    response = asyncio.run(_get("/", host="notes.owner.woltspace.app"))
    assert response.status_code == 403
    assert "identity verification required" in response.text


def test_remote_app_host_with_corrupt_access_config_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "woltspace.json").write_text(json.dumps({
        "apps_domain": "owner.woltspace.app", "access": {"broken": True},
    }))
    monkeypatch.setattr(gateway, "WOLTS_DIR", tmp_path)
    response = asyncio.run(_get("/", host="notes.owner.woltspace.app"))
    assert response.status_code == 403


def test_gateway_log_omits_tokens_cookies_and_query(tmp_path, monkeypatch, capsys):
    _configure(tmp_path, monkeypatch)
    secret = "SECRET_TOKEN_MUST_NOT_APPEAR"
    response = asyncio.run(_get(
        "/clean?secret=query-secret", host="notes.owner.woltspace.app", token=secret,
    ))
    assert response.status_code == 403
    log = capsys.readouterr().err
    assert "path=/clean" in log
    assert "decision=bad-token" in log
    assert secret not in log
    assert "query-secret" not in log


def test_gateway_log_escapes_non_printable_host_and_path(capsys):
    gateway._log_request(
        {"type": "http", "method": "GET", "path": "/bad\x1bpath"},
        {"host": "notes.localhost\x1b"}, 400, "test",
    )
    log = capsys.readouterr().err
    assert "\\x1b" in log
    assert "\x1b" not in log


def test_gateway_port_resolver_is_shared_by_every_consumer():
    sources = [
        ROOT / "src" / "woltspace" / "channels.py",
        ROOT / "server" / "gateway_settings.py",
        ROOT / "server" / "app.py",
        ROOT / "container" / "lib" / "apps.py",
    ]
    for source in sources:
        assert "resolve_app_gateway_port" in source.read_text(), source


def test_owner_local_rule_is_shared_by_lodge_and_gateway():
    for source in (ROOT / "server" / "app.py", ROOT / "server" / "gateway.py"):
        assert "from .local_request import owner_local_request" in source.read_text()


def test_proxy_never_hands_visitor_credentials_to_the_app(monkeypatch):
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen["timeout"] = kwargs.get("timeout")

        def build_request(self, *args, **kwargs):
            seen["headers"] = kwargs["headers"]
            return httpx.Request(*args, **kwargs)

        async def send(self, request, **_kwargs):
            return httpx.Response(200, content=b"ok", request=request)

        async def aclose(self):
            return None

    monkeypatch.setattr(app_proxy.httpx, "AsyncClient", Client)
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "raw_path": b"/", "query_string": b"",
        "headers": [
            (b"host", b"notes.you.example"),
            (b"cf-access-jwt-assertion", b"visitor.jwt.token"),
            (b"cf-access-client-id", b"svc"),
            (b"cookie", b"CF_Authorization=visitor.jwt.token; theme=dark; CF_AppSession=s"),
            (b"x-woltspace-user", b"spoofed@evil.example"),
            (b"connection", b"keep-alive"),
            (b"accept", b"text/html"),
        ],
        "client": ("127.0.0.1", 123), "server": ("notes.you.example", 443),
        "state": {"access_email": "friend@example.com"},
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = asyncio.run(app_proxy.proxy_app_http(Request(scope, receive), "notes"))

    headers = seen["headers"]
    assert response.status_code == 200
    assert "cf-access-jwt-assertion" not in headers
    assert "cf-access-client-id" not in headers
    assert "connection" not in headers
    assert headers["cookie"] == "theme=dark; CF_AppSession=s"
    assert "visitor.jwt.token" not in repr(headers)
    assert headers["x-woltspace-user"] == "friend@example.com"
    assert headers["accept"] == "text/html"
    assert headers["host"] == "localhost:4321"
    # Streams may stay quiet; only connecting is bounded.
    assert seen["timeout"].read is None and seen["timeout"].connect is not None


def test_proxy_drops_a_cookie_header_that_held_only_the_access_cookie():
    headers = app_proxy.upstream_headers({"cookie": "CF_Authorization=t"}, None)
    assert "cookie" not in headers and "x-woltspace-user" not in headers


def test_proxy_reports_an_upstream_timeout_as_bad_gateway(monkeypatch):
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})

    class Client:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, request, **_kwargs):
            raise httpx.ConnectTimeout("slow", request=request)

        async def aclose(self):
            return None

    monkeypatch.setattr(app_proxy.httpx, "AsyncClient", Client)
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "raw_path": b"/", "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 123), "server": ("notes.example", 443),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = asyncio.run(app_proxy.proxy_app_http(Request(scope, receive), "notes"))
    assert response.status_code == 502


def _upstream_ws_server(handler):
    """Run a real websocket server on a free loopback port in a thread."""
    import threading
    import websockets

    ready = threading.Event()
    box = {}

    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                box["port"] = server.sockets[0].getsockname()[1]
                box["stop"] = loop.create_future()
                ready.set()
                await box["stop"]

        loop.run_until_complete(main())

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    return box, thread


def _bridge_app(app_name):
    async def app(scope, receive, send):
        scope.setdefault("state", {})["access_email"] = "friend@example.com"
        # A bridge that never ends fails the test instead of hanging it.
        await asyncio.wait_for(
            app_proxy.proxy_app_websocket(WebSocket(scope, receive, send), app_name, scope["path"]),
            timeout=10,
        )
    return app


def test_websocket_bridge_carries_binary_frames_and_identity(monkeypatch):
    seen = {}

    async def echo(connection):
        seen["user"] = connection.request.headers.get("x-woltspace-user")
        async for message in connection:
            await connection.send(message)

    box, _thread = _upstream_ws_server(echo)
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": box["port"]})
    try:
        with TestClient(_bridge_app("notes")).websocket_connect("/live") as ws:
            ws.send_bytes(b"\x00\x01binary")
            assert ws.receive_bytes() == b"\x00\x01binary"
            ws.send_text("hello")
            assert ws.receive_text() == "hello"
    finally:
        box["stop"].get_loop().call_soon_threadsafe(box["stop"].set_result, None)
    assert seen["user"] == "friend@example.com"


def test_websocket_bridge_closes_the_visitor_when_the_app_hangs_up(monkeypatch):
    async def hang_up(connection):
        await connection.send("bye")
        await connection.close(4000, "app restarting")

    box, _thread = _upstream_ws_server(hang_up)
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": box["port"]})
    try:
        with TestClient(_bridge_app("notes")).websocket_connect("/live") as ws:
            assert ws.receive_text() == "bye"
            message = ws.receive()
            assert message["type"] == "websocket.close"
            # The app's own close code and reason reach the visitor.
            assert message["code"] == 4000
            assert message["reason"] == "app restarting"
    finally:
        box["stop"].get_loop().call_soon_threadsafe(box["stop"].set_result, None)


@pytest.mark.parametrize("code, expected", [
    (None, 1000), (1000, 1000), (1001, 1001), (1005, 1000), (1006, 1011),
    (1015, 1011), (4000, 4000), (999, 1011), (5000, 1011),
])
def test_only_sendable_close_codes_are_forwarded(code, expected):
    assert app_proxy._sendable_close_code(code) == expected


def test_cookie_filter_removes_only_the_exact_access_cookie():
    headers = app_proxy.upstream_headers(
        {"cookie": "cf_authorization2=keep-me; CF_Authorization =t; other=1"}, None,
    )
    assert headers["cookie"] == "cf_authorization2=keep-me; other=1"


def test_proxy_bounds_the_wait_for_response_headers(monkeypatch):
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})
    monkeypatch.setattr(app_proxy, "RESPONSE_START_TIMEOUT", 0.05)

    class Client:
        def __init__(self, **_kwargs):
            pass

        def build_request(self, *args, **kwargs):
            return httpx.Request(*args, **kwargs)

        async def send(self, *_args, **_kwargs):
            await asyncio.sleep(30)  # accepted, never answers

        async def aclose(self):
            return None

    monkeypatch.setattr(app_proxy.httpx, "AsyncClient", Client)
    scope = {
        "type": "http", "method": "GET", "scheme": "https", "path": "/",
        "raw_path": b"/", "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 123), "server": ("notes.example", 443),
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    response = asyncio.run(app_proxy.proxy_app_http(Request(scope, receive), "notes"))
    assert response.status_code == 504


class _BlockingUpstream:
    close_code = None
    close_reason = ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()

    async def send(self, _message):
        return None

    async def close(self, *_args):
        return None


class _BlockingVisitor:
    headers = {}
    query_params = ""
    scope = {"state": {}}

    async def accept(self, subprotocol=None):
        return None

    async def receive(self):
        await asyncio.Event().wait()

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


def test_cancelled_bridge_leaves_no_tasks_behind(monkeypatch):
    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})
    monkeypatch.setattr(app_proxy.websockets, "connect", lambda *_a, **_k: _BlockingUpstream())

    async def scenario():
        handler = asyncio.ensure_future(
            app_proxy.proxy_app_websocket(_BlockingVisitor(), "notes", "/live")
        )
        await asyncio.sleep(0.05)
        handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handler
        await asyncio.sleep(0)
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    assert asyncio.run(scenario()) == []


def test_a_failing_bridge_tells_the_visitor_it_failed(monkeypatch):
    class Failing(_BlockingUpstream):
        async def __anext__(self):
            raise RuntimeError("app bridge broke")

    monkeypatch.setattr(app_proxy, "running_app", lambda _name: {"port": 4321})
    monkeypatch.setattr(app_proxy.websockets, "connect", lambda *_a, **_k: Failing())
    visitor = _BlockingVisitor()

    async def scenario():
        await asyncio.wait_for(app_proxy.proxy_app_websocket(visitor, "notes", "/live"), 5)

    asyncio.run(scenario())
    assert visitor.closed[0] == 1011
