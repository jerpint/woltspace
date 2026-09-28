"""Minimal app-only ASGI gateway. It contains no lodge routes."""

from __future__ import annotations

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


def _app_name(host_header: str, apps_domain: str | None) -> str | None:
    try:
        host = (urlsplit(f"//{host_header}").hostname or "").lower().rstrip(".")
    except ValueError:
        return None
    if not apps_domain or not host.endswith(f".{apps_domain}"):
        return None
    name = host.removesuffix(f".{apps_domain}")
    if not name or "." in name:
        return None
    return name if name[0].isalpha() and all(c.isalnum() or c in "-_" for c in name) else None


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
        app_name = _app_name(headers.get("host", ""), settings.apps_domain)
        if app_name is None:
            return await _reject(scope, receive, send, 404, "Not found")
        if scope["type"] == "websocket" and not _websocket_origin_allowed(headers):
            return await _reject(scope, receive, send, 403, "Invalid websocket origin")
        try:
            access = load_access_settings(Path(WOLTS_DIR))
        except RuntimeError:
            access = None
        if access is None:
            return await _reject(scope, receive, send, 403, "Access verification required")
        token = headers.get("cf-access-jwt-assertion", "").strip()
        if not token:
            return await _reject(scope, receive, send, 403, "Access token required")
        try:
            claims = await access_token_verifier.verify(token, access, access.apps_aud)
        except Exception:
            return await _reject(scope, receive, send, 403, "Invalid Access token")
        email = claims.get("email")
        if not isinstance(email, str):
            return await _reject(scope, receive, send, 403, "Access identity required")
        email = email.strip().lower()
        try:
            shares = read_app_shares(Path(WOLTS_DIR), app_name)
        except RuntimeError:
            return await _reject(scope, receive, send, 403, "App access unavailable")
        if email != access.owner_email and not email_is_shared(email, shares):
            return await _reject(scope, receive, send, 403, "This app is not shared with you")
        scope.setdefault("state", {})["access_email"] = email
        if scope["type"] == "http":
            response = await proxy_app_http(Request(scope, receive), app_name)
            return await response(scope, receive, send)
        websocket = WebSocket(scope, receive, send)
        await proxy_app_websocket(websocket, app_name, scope.get("path", "/"))


app = AppGateway()
