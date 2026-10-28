"""What crosses the lodge's proxy to an app, and what never does.

An app is code a wolt wrote. It gets the request its own page made, but none
of the credentials that Cloudflare Access attached to reach the lodge: with
those an app could replay the owner's login against the lodge or another app.
"""

from __future__ import annotations

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
        if name and name not in ACCESS_COOKIES:
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
    and an Access JWT can be sent as a bearer token. Either is stripped. Any
    other value is the app's own and passes.
    """
    text = value.strip()
    if "cf-access-client" in text.lower():
        return True
    scheme, _, rest = text.partition(" ")
    token = rest.strip() if scheme.lower() == "bearer" else text
    issuer = _jwt_issuer(token).lower().rstrip("/")
    return issuer.endswith(".cloudflareaccess.com")


def upstream_request_headers(headers: list[tuple[str, str]], port: int) -> list[tuple[str, str]]:
    """Headers to send an app on 127.0.0.1:`port`, duplicates kept in order."""
    drop = HOP_BY_HOP | ACCESS_HEADERS | _connection_named(headers) | {"host"}
    out = [("host", f"localhost:{port}")]
    for name, value in headers:
        lower = name.lower()
        if lower in drop:
            continue
        if lower == "cookie":
            value = _without_access_cookies(value)
            if not value:
                continue
        if lower == "authorization" and is_access_authorization(value):
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
            if cookie_name in ACCESS_COOKIES:
                continue
            value = _host_only_set_cookie(value)
        out.append((name, value))
    return out


def websocket_upstream_headers(headers: list[tuple[str, str]], port: int) -> list[tuple[str, str]]:
    """Headers for the app's WebSocket handshake: the app's own cookies,
    origin and auth, without Access credentials. The client library writes
    `host` and the handshake's own fields itself, and appends extra headers
    rather than replacing them, so those are left out here."""
    handshake = {
        "host", "sec-websocket-key", "sec-websocket-version",
        "sec-websocket-extensions", "sec-websocket-protocol", "content-length",
    }
    return [
        (name, value) for name, value in upstream_request_headers(headers, port)
        if name.lower() not in handshake
    ]

