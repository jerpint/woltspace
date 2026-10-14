"""Minimal content-only ASGI gateway: apps and wolt sites. It contains no lodge routes.

Every host it answers is named by `content_hosts.parse_content_host`: an app
(proxied to its process) or a wolt's site (static files from the wolt's own
`wolt/site`, on an origin of its own). Anything else is 404.

Identity is the same for both kinds. Locally, a loopback host with a loopback
peer and no forwarding headers is the owner; this is the trusted-local-process
model, and in container `external` mode it rests on the launcher publishing
the gateway port on host loopback only (see local_request.owner_local_request).
Remotely, a verified Access token for the apps audience names an email; the
lodge audience never opens content, and an empty apps audience refuses.

Sites are served locally only, on the new `<wolt>.sites.localhost` suffix that
nothing used before. Remote site hosts (`site-<wolt>.<apps domain>`) are gated
for every request, HTTP and websocket, whatever credentials come with it: a
browser origin keeps its storage and any installed service worker after the
app that lived there is renamed or removed, refusing new workers evicts
nothing, and there is no record of which `site-*` labels apps used before this
release. `content_origins` now records every host served, so a later remote
policy can tell a clean host from a used one; a used origin is never reused.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, PlainTextResponse

from .access import access_token_verifier, load_access_settings
from .app_proxy import get_app, proxy_app_http, proxy_app_websocket
from .app_sharing import email_is_shared, read_app_shares, read_site_shares
from .config import WOLTS_DIR
from .content_hosts import SITE_PREFIX, ContentHost, find_wolt_dir, anchored_site_root, parse_content_host
from .content_origins import host_kinds, record_served
from .gateway_settings import load_gateway_settings, lodge_origin
from .local_request import owner_local_request
from .site_livereload import hub as livereload_hub, serve_site_livereload
from .site_server import LIVERELOAD_PATH, SITE_HEADERS, SiteContext, serve_site


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


async def request_identity(scope, headers, *, local_host: bool, access):
    """Return the request identity, an audit reason, and when it stops being true.

    The expiry is the Access token's `exp` (None locally, where the loopback
    rule is stateless); long-lived connections close by it.
    """
    if local_host:
        hostname = _host_name(headers.get("host", "")) or ""
        if not owner_local_request(scope, hostname, allow_localhost_subdomain=True):
            return None, "non-loopback-peer", None
        owner = access.owner_email if access is not None else "owner@localhost"
        return owner, "owner-local", None
    if access is None or not access.apps_aud:
        return None, "no-identity-provider", None
    token = headers.get("cf-access-jwt-assertion", "").strip()
    if not token:
        return None, "no-token", None
    try:
        claims = await access_token_verifier.verify(token, access, access.apps_aud)
    except Exception:
        return None, "bad-token", None
    email = claims.get("email")
    if not isinstance(email, str) or not email.strip():
        return None, "no-identity", None
    exp = claims.get("exp")
    return email.strip().lower(), "access", float(exp) if isinstance(exp, (int, float)) else None


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
_SITE_METHODS = {"GET", "HEAD"}
# Remote site hosts stay gated until the used-origin policy ships (module
# docstring). Not a setting: nothing in woltspace.json or the environment
# turns it on.
REMOTE_SITES_ENABLED = False


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


def _site_origin_ok(headers: dict[str, str], *, local: bool) -> bool:
    """Origin names exactly this site: scheme, host and effective port.

    A site's only socket is a browser's, so Origin is required; `null` and
    foreign origins fail. The scheme is what the browser sees (http locally,
    https behind the tunnel), not what the gateway's own listener speaks.
    """
    origin = headers.get("origin", "")
    scheme = "http" if local else "https"
    default_port = 80 if local else 443
    try:
        parsed = urlsplit(origin)
        request = urlsplit(f"//{headers.get('host', '')}")
        origin_port = parsed.port if parsed.port is not None else default_port
        request_port = request.port if request.port is not None else default_port
    except ValueError:
        return False
    return (
        parsed.scheme == scheme
        and bool(parsed.hostname)
        and (parsed.hostname or "").lower().rstrip(".") == (request.hostname or "").lower().rstrip(".")
        and origin_port == request_port
        and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    )


async def _reject(scope, receive, send, status: int, message: str, *, headers=None):
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
    else:
        response = HTMLResponse(f"<h1>{message}</h1>", status_code=status, headers=headers)
        await response(scope, receive, send)


async def _lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await livereload_hub.shutdown()
            await send({"type": "lifespan.shutdown.complete"})
            return


class AppGateway:
    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            return await _lifespan(receive, send)
        if scope.get("type") not in {"http", "websocket"}:
            return await _reject(scope, receive, send, 404, "Not found")
        headers = _headers(scope)
        settings = load_gateway_settings(Path(WOLTS_DIR))
        content = parse_content_host(headers.get("host", ""), settings.apps_domain)
        if content is None:
            _log_request(scope, headers, 404, "unknown-host")
            return await _reject(scope, receive, send, 404, "Not found")
        if content.kind == "reserved":
            # site-<x>.localhost: an app may not take a site's name, and sites
            # live under .sites.localhost locally.
            _log_request(scope, headers, 404, "reserved-name")
            return await _reject(
                scope, receive, send, 404,
                f"Names starting with {SITE_PREFIX} are reserved for wolt sites; "
                f"a site is at &lt;wolt&gt;.sites.localhost",
            )
        if content.kind == "site":
            return await self._site(scope, receive, send, headers, settings, content)
        return await self._app(scope, receive, send, headers, content)

    async def _app(self, scope, receive, send, headers, content: ContentHost):
        app_name, local_host = content.name, content.local
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
        email, identity_reason, _expires = await request_identity(
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
        record_served(Path(WOLTS_DIR), _host_name(headers.get("host", "")) or "", "app")
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

    async def _site(self, scope, receive, send, headers, settings, content: ContentHost):
        """A wolt's site: static GET/HEAD and the owner's reload socket, nothing else.

        Order matters: every refusal below happens before any site file is
        opened or any socket accepted, for HEAD, conditional and range requests
        and directory redirects alike.
        """
        label, local_host = content.name, content.local
        wolts_dir = Path(WOLTS_DIR)
        host = _host_name(headers.get("host", "")) or ""

        async def refuse(status, reason, message, **extra):
            _log_request(scope, headers, status, reason)
            return await _reject(
                scope, receive, send, status, message, headers=dict(SITE_HEADERS, **extra),
            )

        # Unconditional: before method, origin or identity. No credential and
        # no setting opens a remote site host today (module docstring).
        if not local_host and not REMOTE_SITES_ENABLED:
            return await refuse(
                503, "remote-sites-pending",
                "Sites on the app domain are not served yet (pending setup); "
                "a wolt's site is at &lt;wolt&gt;.sites.localhost on the gateway port",
            )
        if scope["type"] == "http":
            method = scope.get("method", "GET").upper()
            if method not in _SITE_METHODS:
                return await refuse(405, "method", "Sites are read-only", allow="GET, HEAD")
        else:
            # The reserved socket is the only one, and only from this site's
            # own pages. Everything else closes before the handshake.
            if scope.get("path", "") != LIVERELOAD_PATH:
                return await refuse(404, "no-such-socket", "Not found")
            if not _site_origin_ok(headers, local=local_host):
                return await refuse(403, "bad-origin", "Invalid websocket origin")
        # A service worker would outlive the content and its share. Refused on
        # every path, whatever the script is called (decision 4 of the spec).
        # This stops new registrations only; it evicts nothing already installed.
        if "service-worker" in headers:
            return await refuse(403, "service-worker", "Service workers are not allowed on sites")
        try:
            access = load_access_settings(wolts_dir)
        except RuntimeError:
            access = None
        email, identity_reason, expires_at = await request_identity(
            scope, headers, local_host=local_host, access=access,
        )
        if email is None:
            return await refuse(403, identity_reason, "Site identity verification required")
        owner_email = access.owner_email if access is not None else email
        is_owner = email == owner_email
        # The share list adds readers; it never grants or removes the owner's
        # own authority, so the owner passes without reading it.
        if not is_owner:
            try:
                shares = read_site_shares(wolts_dir, label)
            except RuntimeError:
                return await refuse(403, "sharing-unavailable", "Site access unavailable")
            if not email_is_shared(email, shares):
                return await refuse(403, "not-shared", "This site is not shared with you")
        if not local_host:
            # A host that ever answered as an app, or whose history cannot be
            # read, is never reused for a site: its browsers may still hold
            # that app's storage and workers. The site gets a fresh host.
            try:
                used = "app" in host_kinds(wolts_dir, host)
            except RuntimeError:
                used = True
            if used or get_app(f"{SITE_PREFIX}{label}") is not None:
                return await refuse(
                    409, "used-origin",
                    f"{host} was an app's origin; a used origin is never reused for a "
                    f"site, so this site needs a different host",
                )
        wolt_dir = find_wolt_dir(wolts_dir, label)
        root = anchored_site_root(wolt_dir) if wolt_dir is not None else None
        if root is None:
            _log_request(scope, headers, 404, "unknown-site")
            response = PlainTextResponse("Not found", status_code=404, headers=SITE_HEADERS)
            return await response(scope, receive, send)
        record_served(wolts_dir, host, "site")
        if scope["type"] == "http":
            ctx = SiteContext(
                wolt_name=wolt_dir.name, wolt_dir=wolt_dir, root=root, owner=is_owner,
                lodge_origin=lodge_origin(settings, wolts_dir, local=local_host),
            )
            response = serve_site(scope, ctx)
            _log_request(scope, headers, response.status_code, identity_reason)
            return await response(scope, receive, send)
        if not is_owner:
            return await refuse(403, "owner-only", "Livereload is for the owner")

        async def still_authorized() -> bool:
            try:
                current = load_access_settings(wolts_dir)
            except RuntimeError:
                return False
            now_email, _reason, _exp = await request_identity(
                scope, headers, local_host=local_host, access=current,
            )
            now_owner = current.owner_email if current is not None else now_email
            if now_email is None or now_email != now_owner or now_email != email:
                return False
            current_dir = find_wolt_dir(wolts_dir, label)
            return current_dir is not None and anchored_site_root(current_dir) == root

        _log_request(scope, headers, 101, identity_reason)
        await serve_site_livereload(
            WebSocket(scope, receive, send), str(root),
            still_authorized=still_authorized, expires_at=expires_at,
        )


app = AppGateway()
