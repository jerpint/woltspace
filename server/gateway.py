"""Minimal app-only ASGI gateway. It contains no lodge routes."""

from __future__ import annotations

import ipaddress
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse

from .access import access_token_verifier, load_access_settings
from .app_proxy import proxy_app_http, proxy_app_websocket
from .app_sharing import email_is_shared, read_app_shares
from .config import WOLTS_DIR
from .gateway_settings import load_gateway_settings


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _host_name(host_header: str) -> str | None:
    try:
        host = (urlsplit(f"//{host_header}").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    return host


def _app_name(host_header: str, apps_domain: str | None) -> tuple[str | None, bool]:
    host = _host_name(host_header)
    if host is None:
        return None, False
    local = host.endswith(".localhost")
    if local:
        name = host.removesuffix(".localhost")
    elif apps_domain and host.endswith(f".{apps_domain}"):
        name = host.removesuffix(f".{apps_domain}")
    else:
        return None, False
    if not name or "." in name:
        return None, local
    valid = name[0].isalpha() and all(c.isalnum() or c in "-_" for c in name)
    return (name if valid else None), local


def _loopback_peer(scope) -> bool:
    peer = scope.get("client")
    try:
        return bool(peer and ipaddress.ip_address(peer[0]).is_loopback)
    except ValueError:
        return False


async def request_identity(scope, headers, *, local_host: bool, access):
    """Return the provider-neutral request identity and an audit reason."""
    if local_host:
        if not _loopback_peer(scope):
            return None, "non-loopback-peer"
        forwarding_headers = {
            "cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host",
            "forwarded", "cf-access-jwt-assertion",
        }
        if forwarding_headers.intersection(headers):
            return None, "forwarded-localhost"
        owner = access.owner_email if access is not None else "owner@localhost"
        return owner, "owner-local"
    if access is None:
        return None, "no-identity-provider"
    token = headers.get("cf-access-jwt-assertion", "").strip()
    if not token:
        return None, "no-token"
    try:
        claims = await access_token_verifier.verify(token, access, access.apps_aud)
    except Exception:
        return None, "bad-token"
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "no-identity"
    return email.strip().lower(), "access"


def _log_request(scope, headers, status: int, reason: str) -> None:
    method = "ws" if scope["type"] == "websocket" else scope.get("method", "HTTP")
    host = ascii(headers.get("host", "-"))[1:-1]
    path = ascii(scope.get("path", "/"))[1:-1]
    timestamp = datetime.now(timezone.utc).isoformat()
    print(
        f"{timestamp} host={host} path={path} method={method} status={status} decision={reason}",
        file=sys.stderr, flush=True,
    )


def _websocket_origin_allowed(headers: dict[str, str]) -> bool:
    origin = headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").lower().rstrip(".")
        origin_port = parsed.port
        request = urlsplit(f"//{headers.get('host', '')}")
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and origin_host == (request.hostname or "").lower().rstrip(".")
        and origin_port == request.port
    )


async def _reject(scope, receive, send, status: int, message: str):
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
    else:
        await HTMLResponse(f"<h1>{message}</h1>", status_code=status)(scope, receive, send)


class AppGateway:
    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"}:
            return await _reject(scope, receive, send, 404, "Not found")
        headers = _headers(scope)
        settings = load_gateway_settings(Path(WOLTS_DIR))
        app_name, local_host = _app_name(headers.get("host", ""), settings.apps_domain)
        if app_name is None:
            _log_request(scope, headers, 404, "unknown-host")
            return await _reject(scope, receive, send, 404, "Not found")
        if scope["type"] == "websocket" and not _websocket_origin_allowed(headers):
            _log_request(scope, headers, 403, "bad-origin")
            return await _reject(scope, receive, send, 403, "Invalid websocket origin")
        try:
            access = load_access_settings(Path(WOLTS_DIR))
        except RuntimeError:
            access = None
        email, identity_reason = await request_identity(
            scope, headers, local_host=local_host, access=access,
        )
        if email is None:
            _log_request(scope, headers, 403, identity_reason)
            return await _reject(scope, receive, send, 403, "App identity verification required")
        try:
            shares = read_app_shares(Path(WOLTS_DIR), app_name)
        except RuntimeError:
            _log_request(scope, headers, 403, "sharing-unavailable")
            return await _reject(scope, receive, send, 403, "App access unavailable")
        owner_email = access.owner_email if access is not None else email
        if email != owner_email and not email_is_shared(email, shares):
            _log_request(scope, headers, 403, "not-shared")
            return await _reject(scope, receive, send, 403, "This app is not shared with you")
        scope.setdefault("state", {})["access_email"] = email
        if scope["type"] == "http":
            response = await proxy_app_http(Request(scope, receive), app_name)
            reason = identity_reason if response.status_code < 400 else (
                "unknown-app" if response.status_code == 404 else
                "stopped" if response.status_code == 503 else "upstream-error"
            )
            _log_request(scope, headers, response.status_code, reason)
            return await response(scope, receive, send)
        _log_request(scope, headers, 101, identity_reason)
        websocket = WebSocket(scope, receive, send)
        await proxy_app_websocket(websocket, app_name, scope.get("path", "/"))


app = AppGateway()
