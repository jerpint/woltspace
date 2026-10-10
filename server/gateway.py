"""Minimal app-only ASGI gateway. It contains no lodge routes."""

from __future__ import annotations

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
from .local_request import owner_local_request


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


async def request_identity(scope, headers, *, local_host: bool, access):
    """Return the provider-neutral request identity and an audit reason."""
    if local_host:
        hostname = _host_name(headers.get("host", "")) or ""
        if not owner_local_request(scope, hostname, allow_localhost_subdomain=True):
            return None, "non-loopback-peer"
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


_READ_ONLY_METHODS = {"GET", "HEAD", "OPTIONS"}


def _origin_matches_host(headers: dict[str, str]) -> bool:
    """No Origin (a non-browser client), or one naming exactly this host."""
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
        if scope["type"] == "websocket" and not _origin_matches_host(headers):
            _log_request(scope, headers, 403, "bad-origin")
            return await _reject(scope, receive, send, 403, "Invalid websocket origin")
        # The lodge's rule, local and remote alike: a browser write must come
        # from the app's own page. Without it any page the owner visits can
        # drive an app as the owner. Origin-less (non-browser) writes pass.
        if (scope["type"] == "http"
                and scope.get("method", "GET").upper() not in _READ_ONLY_METHODS
                and (headers.get("sec-fetch-site", "").lower() == "cross-site"
                     or not _origin_matches_host(headers))):
            _log_request(scope, headers, 403, "cross-site")
            return await _reject(scope, receive, send, 403, "Cross-site request rejected")
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
