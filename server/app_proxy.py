"""What crosses the lodge's proxy to an app, and what never does.

An app is code a wolt wrote. It gets the request its own page made, but none
of the credentials that Cloudflare Access attached to reach the lodge: with
those an app could replay the owner's login against the lodge or another app.
"""

from __future__ import annotations

import asyncio
import base64
import json

# Hop-by-hop headers (RFC 9110 section 7.6.1) belong to one connection, never
# to the next hop. `host` is set to the app's own address by the caller.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
})

# Cloudflare Access credentials, as a header or as a cookie. The app's own
# `Authorization` header and cookies pass through, unless the value is shaped
# like an Access credential (see `is_access_authorization`).
ACCESS_HEADERS = frozenset({
    "cf-access-jwt-assertion", "cf-access-token",
    "cf-access-client-id", "cf-access-client-secret",
})
ACCESS_COOKIES = frozenset({"CF_Authorization", "CF_AppSession"})
# The lodge's own credentials (container/lib/lodge_auth.py): a paired
# browser's cookies and a lodge key. An app or tool never sees them, and can't
# set those cookies either.
LODGE_COOKIES = frozenset({"woltspace-lodge", "__Host-woltspace-lodge", "woltspace-site"})
PRIVATE_COOKIES = ACCESS_COOKIES | LODGE_COOKIES


def _private_cookie(name: str) -> bool:
    """Access's and the lodge's cookies, port-suffixed ones included."""
    return name in PRIVATE_COOKIES or any(name.startswith(f"{base}-") for base in LODGE_COOKIES)
LODGE_HEADERS = frozenset({"x-woltspace-key", "x-woltspace-mill-identity"})


def _connection_named(headers: list[tuple[str, str]]) -> set[str]:
    named = set()
    for name, value in headers:
        if name.lower() == "connection":
            named.update(t.strip().lower() for t in value.split(",") if t.strip())
    return named


def _without_access_cookies(value: str) -> str:
    kept = []
    for part in value.split(";"):
        name = part.split("=", 1)[0].strip()
        if name and not _private_cookie(name):
            kept.append(part.strip())
    return "; ".join(kept)


def _jwt_issuer(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 3:
        return ""
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, TypeError):
        return ""
    issuer = claims.get("iss") if isinstance(claims, dict) else None
    return issuer if isinstance(issuer, str) else ""


def is_access_authorization(value: str) -> bool:
    """True for an `Authorization` value that carries an Access credential.

    Access can be set to read a service token from one header, `Authorization`
    included, as JSON with `cf-access-client-id` and `cf-access-client-secret`;
    an Access JWT can be sent as a bearer token; and Managed OAuth issues
    opaque bearer tokens prefixed `oauth:`. All are stripped. Any other value
    is the app's own and passes.
    """
    text = value.strip()
    if "cf-access-client" in text.lower():
        return True
    if text.startswith("{"):
        # JSON may spell a key with escapes; judge the decoded keys.
        try:
            decoded = json.loads(text)
        except ValueError:
            decoded = None
        if isinstance(decoded, dict) and any(
            isinstance(key, str) and key.strip().lower().startswith("cf-access-")
            for key in decoded
        ):
            return True
    scheme, _, rest = text.partition(" ")
    token = rest.strip() if scheme.lower() == "bearer" else text
    if token.lower().startswith("oauth:"):
        return True
    issuer = _jwt_issuer(token).lower().rstrip("/")
    return issuer.endswith(".cloudflareaccess.com")


def _bearer(value: str) -> str:
    scheme, _, token = value.strip().partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def upstream_request_headers(
    headers: list[tuple[str, str]], port: int, *, is_lodge_key=lambda token: False,
) -> list[tuple[str, str]]:
    """Headers to send an app on 127.0.0.1:`port`, duplicates kept in order.

    `is_lodge_key` says whether a bearer token is one of the lodge's keys,
    which is stripped like an Access credential; any other bearer passes.
    """
    drop = HOP_BY_HOP | ACCESS_HEADERS | LODGE_HEADERS | _connection_named(headers) | {"host"}
    out = [("host", f"localhost:{port}")]
    for name, value in headers:
        lower = name.lower()
        if lower in drop:
            continue
        if lower == "cookie":
            value = _without_access_cookies(value)
            if not value:
                continue
        if lower == "authorization" and (
                is_access_authorization(value) or is_lodge_key(_bearer(value))):
            continue
        out.append((name, value))
    return out


def _host_only_set_cookie(value: str) -> str:
    """Drop `Domain=` so an app's cookie stays on the app's own host."""
    parts = value.split(";")
    kept = [parts[0]] + [p for p in parts[1:] if p.strip().split("=", 1)[0].strip().lower() != "domain"]
    return ";".join(kept)


def client_response_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Headers to return to the browser from an app's response.

    The body is relayed raw (still compressed), so `content-encoding` and
    `content-length` stay as the app sent them.
    """
    drop = HOP_BY_HOP | _connection_named(headers)
    out = []
    for name, value in headers:
        lower = name.lower()
        if lower in drop:
            continue
        if lower == "set-cookie":
            cookie_name = value.split("=", 1)[0].strip()
            if _private_cookie(cookie_name):
                continue
            value = _host_only_set_cookie(value)
        out.append((name, value))
    return out


def websocket_upstream_headers(
    headers: list[tuple[str, str]], port: int, *, is_lodge_key=lambda token: False,
) -> list[tuple[str, str]]:
    """Headers for the app's WebSocket handshake: the app's own cookies,
    origin and auth, without Access credentials. The client library writes
    `host` and the handshake's own fields itself, and appends extra headers
    rather than replacing them, so those are left out here."""
    handshake = {
        "host", "sec-websocket-key", "sec-websocket-version",
        "sec-websocket-extensions", "sec-websocket-protocol", "content-length",
    }
    return [
        (name, value) for name, value in upstream_request_headers(headers, port, is_lodge_key=is_lodge_key)
        if name.lower() not in handshake
    ]



async def relay_websocket(ws, upstream) -> None:
    """Relay frames between a browser's socket and an upstream one.

    Frames keep their type both ways: Vite and other dev servers send binary
    frames, and a text-only relay drops them. When either side ends, the other
    is stopped: a closed tab must not leave the upstream socket open, and a
    closed upstream must close the tab's.
    """
    async def client_to_upstream():
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                await upstream.send(message["bytes"])
            elif message.get("text") is not None:
                await upstream.send(message["text"])

    async def upstream_to_client():
        async for msg in upstream:
            if isinstance(msg, bytes):
                await ws.send_bytes(msg)
            else:
                await ws.send_text(msg)

    tasks = [
        asyncio.ensure_future(client_to_upstream()),
        asyncio.ensure_future(upstream_to_client()),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
