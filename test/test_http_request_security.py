"""The localhost lodge is not a public cross-origin control API."""

import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app


def _client() -> TestClient:
    return TestClient(server_app.app, base_url="http://localhost:7777")


def test_dns_rebinding_read_is_rejected_even_without_origin():
    response = _client().get(
        "/sessions",
        headers={"host": "evil.example:7777"},
    )

    assert response.status_code == 403
    assert response.json() == {"error": "untrusted request host"}


def test_foreign_simple_request_cannot_deliver_a_session_message(monkeypatch):
    delivered = Mock()
    monkeypatch.setattr(server_app, "deliver_message", delivered)

    response = _client().post(
        "/sessions/friend/message",
        content='{"text":"run this"}',
        headers={
            "content-type": "text/plain",
            "origin": "https://evil.example",
        },
    )

    assert response.status_code == 403
    delivered.assert_not_called()


def test_sec_fetch_cross_site_write_is_rejected_without_origin(monkeypatch):
    delivered = Mock()
    monkeypatch.setattr(server_app, "deliver_message", delivered)

    response = _client().post(
        "/sessions/friend/message",
        json={"text": "run this"},
        headers={"sec-fetch-site": "cross-site"},
    )

    assert response.status_code == 403
    delivered.assert_not_called()


def test_native_cli_message_without_browser_headers_still_delivers(monkeypatch):
    delivered = Mock(return_value={"status": "delivered"})
    monkeypatch.setattr(server_app, "deliver_message", delivered)

    response = _client().post(
        "/sessions/friend/message",
        json={"text": "hello from the connector"},
    )

    assert response.status_code == 200
    delivered.assert_called_once_with(
        "friend",
        "hello from the connector",
        from_wolt="",
        from_session="",
    )


def test_same_origin_browser_write_still_delivers(monkeypatch):
    delivered = Mock(return_value={"status": "delivered"})
    monkeypatch.setattr(server_app, "deliver_message", delivered)

    response = _client().post(
        "/sessions/friend/message",
        json={"text": "hello from the lodge"},
        headers={"origin": "http://localhost:7777"},
    )

    assert response.status_code == 200
    delivered.assert_called_once()


def test_lodge_responses_do_not_publish_wildcard_cors():
    response = _client().get("/sessions")

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_configured_tunnel_lodge_and_app_hosts_remain_valid(monkeypatch):
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    lodge = _client().get(
        "/sessions",
        headers={"host": "owner.woltspace.test"},
    )
    app = _client().get(
        "/",
        headers={"host": "notes.woltspace.test"},
    )

    assert lodge.status_code == 200
    # It passed the request guard and reached the app proxy. The app is simply
    # not running in this isolated test.
    assert app.status_code == 503
    assert "not running" in app.text


def test_quick_tunnel_exact_host_remains_valid(monkeypatch):
    monkeypatch.setattr(
        server_app.tunnel_mgr,
        "_tunnel_url",
        "https://random-name.trycloudflare.com",
    )

    response = _client().get(
        "/sessions",
        headers={"host": "random-name.trycloudflare.com"},
    )

    assert response.status_code == 200


def test_invalid_tunnel_sibling_is_not_treated_as_an_app(monkeypatch):
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_hostname", "owner.woltspace.test")
    monkeypatch.setattr(server_app.tunnel_mgr, "_tunnel_domain", "woltspace.test")

    response = _client().get(
        "/sessions",
        headers={"host": "two.labels.woltspace.test"},
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("localhost:7777", "https://evil.example"),
        ("evil.example:7777", "http://evil.example:7777"),
    ],
)
def test_terminal_websocket_rejects_foreign_and_rebinding_origins(host, origin):
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client().websocket_connect(
            "/tui?session=main",
            headers={"host": host, "origin": origin},
        ):
            pass

    assert exc.value.code == 1008


def test_originless_native_websocket_still_requires_an_allowed_host():
    class Socket:
        headers = {"host": "127.0.0.1:7777"}

    assert server_app._websocket_request_allowed(Socket()) is True


def test_app_websocket_rejects_a_foreign_origin():
    with pytest.raises(WebSocketDisconnect) as exc:
        with _client().websocket_connect(
            "/vite-hmr",
            headers={
                "host": "notes.localhost:7777",
                "origin": "https://evil.example",
            },
        ):
            pass

    assert exc.value.code == 1008
