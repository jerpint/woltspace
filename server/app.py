"""Woltspace server — FastAPI replacement for server.js."""

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import contextlib
import unicodedata
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from fastapi import Body, FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import tools as tool_registry
from .config import (
    APP_MIME_TYPES,
    dotenv_env,
    CONTAINER_HOME,
    DEN_REPLY_FOOTER,
    MIME_TYPES,
    PORT,
    APPS_DIR,
    PUBLIC_DIR,
    SITE_DIR,
    SPARKS_DIR,
    STATE_DIR,
    WOLT_DIR,
    WOLT_NAME,
    WOLTS_DIR,
    WOLTSPACE_DIR,
    load_dotenv,
)
from . import tunnel as tunnel_mgr
from . import ways_in
from .app_proxy import (
    client_response_headers,
    relay_websocket,
    upstream_request_headers,
    websocket_upstream_headers,
)
from .notify import AttachmentError, NoNotificationTarget, send_notification
from .speech import SpeechError
from .sparks import get_spark_with_chain, list_sparks
from .pty_bridge import (
    PtyBridgeError,
    PtyTextDecoder,
    attach_tmux,
)

# Session spawning — shared with bot
import sys as _sys
_sys.path.insert(0, str(WOLTSPACE_DIR / "container" / "lib"))
from creature_types import base_type, creature_emoji, custom_creature_models
from settings_files import settings_lock, write_json_atomic, write_text_atomic
from sessions import (
    resume_session, start_session, stop_session,
    deliver_message, resolve_active_session, format_attributed_message,
    format_spawned_prompt,
    wolt_harness, ResumeUnavailable, ResumeFailed, HarnessNotInstalled,
    stored_resume_id, recover_resume_id, taken_resume_ids, claim_resume_id,
)
from session_expiry import (
    HOLD_MAX_SECONDS, get_idle_timeout, hold_active, last_use, public_hold, set_idle_timeout,
)
from session_runtime import RuntimeHandle, get_runtime
from session_targets import SessionTarget
from execution_policy import AutoGrantStore, POLICY_VERSION
import app_sharing
import lodge_auth
from runtime_context import RuntimeContext
from harness_auth import auth_source, claude_authenticated
from skills_sync import wolt_skills_delivery
from harnesses import (
    harness_metadata,
    get_default_harness,
    set_default_harness,
    set_tier_defaults,
    resolve_harness,
    harness_install_problem,
    platform_skill_invoke,
    is_valid_model,
    model_catalog,
    tier_default_model,
    creature_model,
    HARNESSES,
    refresh_model_catalogs,
)
from apps import (
    WoltspaceApp,
    apps_restore,
    discover_apps,
    get_app,
    app_log_file,
    running_apps,
    start_app,
    stop_app,
    restart_app,
    set_app_keeper,
)
from sites import ensure_site, site_dir
import wolfcore
import wolt_archive
from site_shell import load_site_config, memory_payload, shell_manifest, shell_tags, shell_wanted
from .state import (
    bot_log,
    get_current_meta,
    get_current_url,
    log_view,
    read_views_history,
    sanitize_session,
    set_current_url,
    onboarding_status,
    apply_default_harness_from_env,
    install_starter_seed,
    select_onboarding_harness,
)

# --- Live reload ---

_livereload_clients: set[WebSocket] = set()

LIVERELOAD_SCRIPT = '<script>(function(){var p=location.protocol==="https:"?"wss:":"ws:";function connect(){var ws=new WebSocket(p+"//"+location.host+"/livereload");ws.onmessage=function(){location.reload()};ws.onclose=function(){setTimeout(connect,3000)}}connect()})()</script>'


async def _broadcast_reload():
    dead = []
    for ws in _livereload_clients:
        try:
            await ws.send_text("reload")
        except Exception:
            dead.append(ws)
    for ws in dead:
        _livereload_clients.discard(ws)


# --- Livereload watchers ---
# Two of them, both running a blocking rust file-watch loop, and neither used to
# be told when to stop:
#
#   * the lodge watcher below is a daemon thread. Interpreter shutdown kills a
#     daemon thread wherever it happens to be — mid-rust-call, here — which the
#     C runtime reports as `FATAL: exception not rethrown` and docker reports as
#     exit 133 instead of 0. It only ever appeared on colonies that had a wolt,
#     because that is when SITE_DIR exists and this thread starts.
#   * the per-connection `awatch` in the site livereload websocket outlives
#     uvicorn's graceful window, because `to_thread` cannot be cancelled: hence
#     `Cancel N running task(s), timeout graceful shutdown exceeded` followed by
#     a CancelledError traceback, and ~3 seconds added to every `docker stop`.
#
# One stop event each, set on shutdown, ends both. `rust_timeout` is how long a
# blocked watch waits before coming up for air to check the flag.
_WATCHER_POLL_MS = 500
_WATCHER_JOIN_TIMEOUT = 2.0
# How long a livereload handler waits for its own watcher to notice the flag
# and finish, before it stops being polite about it.
_WATCHER_SETTLE_S = 2.0

_watcher_stop = threading.Event()
_watcher_threads: list[threading.Thread] = []
_livereload_stops: set[threading.Event] = set()


def _start_file_watcher():
    """Watch wolt/site/ for changes and broadcast reload."""
    from watchfiles import watch

    def _watch():
        loop = None
        for _changes in watch(str(SITE_DIR), stop_event=_watcher_stop,
                              rust_timeout=_WATCHER_POLL_MS):
            if loop is None:
                loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(asyncio.ensure_future, _broadcast_reload())

    if SITE_DIR.exists():
        _watcher_stop.clear()
        t = threading.Thread(target=_watch, daemon=True)
        t.start()
        _watcher_threads.append(t)
        print(f"[livereload] watching {SITE_DIR}")


def signal_watchers_to_stop() -> None:
    """Raise every stop flag. Cheap, non-blocking, safe to call repeatedly."""
    _watcher_stop.set()
    for event in list(_livereload_stops):
        event.set()


def stop_file_watchers(timeout: float = _WATCHER_JOIN_TIMEOUT) -> None:
    """Ask every file watcher to stop, and wait for the threads to actually go.

    Called from the lifespan's shutdown half so the watchers are gone *before*
    uvicorn starts counting down its graceful window — which is what turns a
    noisy 3-second stop into a quiet one.
    """
    signal_watchers_to_stop()
    for thread in _watcher_threads:
        thread.join(timeout=timeout)
    _watcher_threads.clear()


# Digest cron removed — the wolf creature owns all scheduling via wolt/wolf.json.
# See creatures/wolf.py.


# --- Tool GC ---
# jerpint: what is this lol

def _start_tool_gc():
    import threading

    def _gc_loop():
        while True:
            time.sleep(30)
            tool_registry.gc()

    t = threading.Thread(target=_gc_loop, daemon=True)
    t.start()


# --- Lifespan ---
# jerpint: also this?

@asynccontextmanager
async def lifespan(app: FastAPI):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(apply_default_harness_from_env)
    refresh_model_catalogs()
    tool_registry.restore()
    apps_restore()
    # Shared apps' quick tunnels, in the background: each can take seconds.
    threading.Thread(target=app_sharing.restore, args=(WOLTS_DIR, PORT), daemon=True).start()
    _start_file_watcher()
    _start_tool_gc()
    if lodge_auth.enabled():
        lodge_auth.ensure_keys(WOLTS_DIR)
        print("[lodge auth] on: pair a browser with `woltspace pair`")
    tunnel_mgr.start_tunnel()
    try:
        import connector_host
        await connector_host.start(WOLTS_DIR)
    except Exception as exc:  # a connector must never keep the lodge from starting
        print(f"[connectors] socket broker not started: {type(exc).__name__}: {exc}")
    print(f"""
  woltspace server (python) · http://localhost:{PORT}
  wolt: {WOLT_NAME}
  browser terminal: embedded Python PTY bridge
  tunnel: {tunnel_mgr.get_tunnel_url() or 'disabled'}
    """)
    yield
    # Watchers first: they are what used to outlive the shutdown window.
    stop_file_watchers()
    try:
        import connector_host
        await connector_host.stop()
    except Exception as exc:
        print(f"[connectors] socket broker did not stop cleanly: {exc}")
    tunnel_mgr.stop_tunnel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    from woltspace import __version__ as woltspace_version
    from woltspace.adoption import read_adoption_report
    from woltspace.channel_supervisor import read_connector_report
    from woltspace.layout import RuntimeLayout

    layout = RuntimeLayout.from_env()
    adoption = read_adoption_report(layout)
    return {
        "ok": True,
        # The lodge's own version. The tui reads it to check the minimum
        # woltspace it needs; a lodge too old to report it is treated as 0.5.0.
        "version": woltspace_version,
        "instance_id": os.environ.get("WOLTSPACE_INSTANCE_ID", ""),
        "pid": os.getpid(),
        "wolts_dir": str(WOLTS_DIR),
        "isolation": os.environ.get("WOLTSPACE_ISOLATION", "external"),
        "adoption": adoption,
        "connectors": read_connector_report(layout).get("connectors", []),
        "db": _db_health(adoption),
    }


def _db_health(adoption: dict) -> dict:
    """Where lodge.db is, which migrations it has, what was imported into it."""
    import lodge_db
    db = {"path": str(lodge_db.path(WOLTS_DIR)), "schema": [], "imported": {}}
    try:
        conn = lodge_db.connect(WOLTS_DIR, readonly=True)
        try:
            db["schema"] = lodge_db.applied_migrations(conn)
            db["imported"] = lodge_db.imports(conn)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        db["error"] = f"{type(exc).__name__}: {exc}"
    if adoption.get("import_errors"):
        db["import_error"] = adoption["import_errors"]
    return db

# --- Templates & Static ---

TEMPLATES_DIR = WOLTSPACE_DIR / "templates"
STATIC_DIR = PUBLIC_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files (CSS/JS shared across pages)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")




def _split_host(host_header: str) -> tuple[str, int | None]:
    """Return a normalized hostname and optional port from an HTTP Host value."""
    try:
        parsed = urlsplit(f"//{host_header.strip()}")
        return (parsed.hostname or "").lower().rstrip("."), parsed.port
    except ValueError:
        return "", None


def _allowed_http_hostname(hostname: str) -> bool:
    """Allow only the lodge's own names: loopback and its tunnel host.

    A site or app host never reaches the lodge's routes: the Mill forwarder
    takes every one it recognises, and anything else shaped like one (a
    reserved `site-` name, an invalid label) is refused here.
    """
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    # Each way in (server/ways_in.py) names the lodge its own way.
    return hostname in ways_in.lodge_hosts()


def _same_http_authority(origin: str, host_header: str) -> bool:
    """Require a browser write's Origin to name the exact request authority."""
    try:
        parsed = urlsplit(origin)
        origin_host = (parsed.hostname or "").lower().rstrip(".")
        origin_port = parsed.port
    except ValueError:
        return False
    request_host, request_port = _split_host(host_header)
    return (
        parsed.scheme in {"http", "https"}
        and origin_host == request_host
        and origin_port == request_port
    )


def _websocket_request_allowed(ws: WebSocket) -> bool:
    """Apply the localhost/tunnel authority boundary before WS acceptance."""
    host_header = ws.headers.get("host") or ""
    hostname, _port = _split_host(host_header)
    if not _allowed_http_hostname(hostname):
        return False
    origin = ws.headers.get("origin")
    # Browser WebSocket handshakes always carry Origin. Keeping an Origin-less
    # path permits native clients only after their Host passed the allowlist.
    return not origin or _same_http_authority(origin, host_header)


def _lodge_websocket_request_allowed(ws: WebSocket) -> bool:
    host_header = ws.headers.get("host") or ""
    return _websocket_request_allowed(ws) and _extract_app_subdomain(host_header) is None

def _extract_app_subdomain(host_header: str) -> str | None:
    """Extract app name from subdomain hostname, or None if not an app subdomain.

    Matches: corework.localhost, corework.woltspace.com
    Excludes: localhost, jerpint.woltspace.com (the lodge itself)
    """
    host, _port = _split_host(host_header)
    if host.endswith(".localhost") and host != "localhost":
        return host.removesuffix(".localhost")
    td = tunnel_mgr.get_tunnel_domain()
    th = tunnel_mgr.get_tunnel_hostname()
    if td and host.endswith(f".{td}") and host != th:
        return host.removesuffix(f".{td}")
    return None




from .app_proxy import APP_PROXY_TIMEOUT, APP_WS_MAX_MESSAGE  # noqa: E402


def _is_lodge_key(token: str) -> bool:
    return bool(token) and lodge_auth.key_scope(WOLTS_DIR, token) is not None


# Security boundary for the localhost control plane. Host is checked on every
# request, even when Origin is absent: a DNS-rebinding page sees itself as
# same-origin and browsers therefore omit Origin on its reads. Browser writes
# additionally have to be same-authority and not cross-site. Native CLI and
# connector clients send no browser provenance headers and keep working.
_READ_ONLY_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


@app.middleware("http")
async def request_origin_guard(request: Request, call_next):
    host_header = request.headers.get("host") or ""
    hostname, _port = _split_host(host_header)
    if not _allowed_http_hostname(hostname):
        return JSONResponse({"error": "untrusted request host"}, status_code=403)

    if request.method.upper() not in _READ_ONLY_HTTP_METHODS:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse({"error": "cross-site request rejected"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and not _same_http_authority(origin, host_header):
            return JSONResponse({"error": "cross-origin request rejected"}, status_code=403)

    return await call_next(request)


# --- Lodge auth (opt-in: WOLTSPACE_LODGE_AUTH=1) ---------------------------
# Being on localhost is not proof of being the owner: any program on the
# machine can send whatever Host and Origin it likes. With lodge auth on,
# every lodge request carries a key (programs) or a paired browser's cookie.
# App hosts are proxied to their app and are not gated here.

_LODGE_AUTH_OPEN_PATHS = ("/pair", "/health", "/favicon.svg")
_pairing_failures = lodge_auth.FailureLimit()

_UNPAIRED_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Pair this browser</title>
<style>body{margin:0;font:16px/1.5 system-ui,sans-serif;background:#EDE8DE;color:#2a2622}
main{max-width:520px;margin:0 auto;padding:48px 16px}h1{font:400 26px Georgia,serif;color:#C4531E}
code{background:#e4ddd0;padding:2px 6px;border-radius:4px}</style></head><body><main>
<h1>Pair this browser</h1><p>This lodge only opens in a paired browser.</p>
<p>On the lodge's machine, run <code>woltspace pair</code> and open the link it prints.
The link works once, for ten minutes.</p></main></body></html>"""


def _lodge_auth_scope(headers) -> str | None:
    """The caller's scope from a key or a paired-browser cookie, else None."""
    presented = headers.get(lodge_auth.KEY_HEADER, "")
    if not presented:
        scheme, _, value = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer":
            presented = value.strip()
    if presented:
        return lodge_auth.key_scope(WOLTS_DIR, presented)
    # Lenient parsing: other servers on localhost share this cookie jar, and
    # one malformed cookie of theirs must not lock the owner out.
    from starlette.requests import cookie_parser
    jar = cookie_parser(headers.get("cookie", ""))
    host = headers.get("host", "")
    for secure in (True, False):
        token = jar.get(lodge_auth.cookie_name(host, secure), "")
        if token and lodge_auth.browser_paired(WOLTS_DIR, token):
            return lodge_auth.OWNER
    return None


class LodgeAuthMiddleware:
    """Refuse lodge HTTP and WebSocket requests without a key or paired browser."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket") or not lodge_auth.enabled():
            return await self.inner(scope, receive, send)
        raw = scope.get("headers", [])
        hosts = [value for key, value in raw if key.lower() == b"host"]
        if len(hosts) != 1:
            # Starlette reads the first Host, a dict the last: two Hosts could
            # pass this gate as an app host and reach the lodge as itself.
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            response = JSONResponse({"error": "exactly one Host header required"}, status_code=400)
            return await response(scope, receive, send)
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in raw
        }
        path = scope.get("path", "/")
        # Site and app hosts never get here (the Mill forwarder takes them),
        # so every host is the lodge's: no exemption by host name.
        if path in _LODGE_AUTH_OPEN_PATHS or path.startswith("/static/"):
            return await self.inner(scope, receive, send)

        caller = _lodge_auth_scope(headers)
        if scope["type"] == "websocket":
            # Terminals and reload sockets are the owner's; wolt tools use HTTP.
            if caller != lodge_auth.OWNER:
                return await send({"type": "websocket.close", "code": 1008})
            return await self.inner(scope, receive, send)

        method = scope.get("method", "GET")
        if caller is None:
            if method == "GET" and "text/html" in headers.get("accept", ""):
                response = HTMLResponse(_UNPAIRED_PAGE, status_code=401)
            else:
                response = JSONResponse({"error": "lodge key or paired browser required"}, status_code=401)
            return await response(scope, receive, send)
        if caller == lodge_auth.WOLT and not lodge_auth.wolt_may(method, path):
            response = JSONResponse({"error": "only the owner can do this"}, status_code=403)
            return await response(scope, receive, send)
        return await self.inner(scope, receive, send)


app.add_middleware(LodgeAuthMiddleware)


@app.get("/pair")
async def pair_browser(request: Request, code: str = ""):
    """Turn a one-time code from `woltspace pair` into this browser's cookie."""
    if not lodge_auth.enabled():
        return JSONResponse({"error": "lodge auth is off"}, status_code=404)
    no_leak = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
    if _pairing_failures.blocked():
        return JSONResponse({"error": "too many attempts; wait a minute"}, status_code=429, headers=no_leak)
    token = lodge_auth.redeem_pairing_code(WOLTS_DIR, code)
    if token is None:
        _pairing_failures.record()
        return HTMLResponse(
            _UNPAIRED_PAGE.replace("<p>This lodge only opens in a paired browser.</p>",
                                   "<p>That pairing link was already used, or has expired.</p>"),
            status_code=403, headers=no_leak,
        )
    secure = (request.url.scheme == "https"
              or request.headers.get("x-forwarded-proto", "").lower() == "https")
    response = RedirectResponse("/", status_code=303, headers=no_leak)
    response.set_cookie(
        lodge_auth.cookie_name(request.headers.get("host", ""), secure), token,
        max_age=lodge_auth.BROWSER_TTL, path="/", httponly=True, samesite="lax", secure=secure,
    )
    return response


# --- Wolt sites, through the Mill -----------------------------------------------
# `<wolt>.sites.localhost` is a wolt's site on an origin of its own. The lodge
# only forwards it, over the Mill's unix socket, and never runs a lodge route
# for it. Who the visitor is gets decided here and handed to the Mill in one
# header; the visitor's own credentials stay behind.

from . import mill as mill_server  # noqa: E402
from .content_hosts import find_wolt_dir as _find_wolt_dir, parse_app_host, parse_site_host  # noqa: E402
from dataclasses import dataclass, replace  # noqa: E402

# What a site's request may carry to the Mill: content negotiation, caching,
# ranges and browser provenance. No cookie, no Authorization, no lodge key.
_MILL_REQUEST_HEADERS = frozenset({
    "accept", "accept-language", "cache-control", "if-modified-since", "if-none-match",
    "if-range", "origin", "pragma", "range", "sec-fetch-dest", "sec-fetch-mode",
    "sec-fetch-site", "sec-fetch-user", "service-worker", "user-agent",
})
_FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip",
                       "cf-connecting-ip")
_SITE_GRANT_PATH = "/_woltspace/grant"
MILL_TIMEOUT = httpx.Timeout(connect=5.0, write=30.0, read=60.0, pool=5.0)
# How often an open site socket rechecks that its visitor may still be there.
SITE_SOCKET_RECHECK_SECONDS = 30.0


def _cookie(headers: dict[str, str], name: str) -> str:
    from starlette.requests import cookie_parser
    return cookie_parser(headers.get("cookie", "")).get(name, "")


def _site_visitor(scope, headers: dict[str, str], label: str) -> str | None:
    """"owner", or None: a site is the owner's only, locally, for now.

    With lodge auth on, the owner holds this site's own cookie (from
    /sites/open). With it off, the owner is a loopback caller that came
    straight in, not through a tunnel, as for the lodge's old site route.
    """
    if lodge_auth.enabled():
        token = _cookie(headers, lodge_auth.SITE_COOKIE)
        return "owner" if lodge_auth.site_session_ok(WOLTS_DIR, label, token) else None
    client = (scope.get("client") or ("", 0))[0]
    if client not in {"127.0.0.1", "::1"} or any(h in headers for h in _FORWARDING_HEADERS):
        return None
    return "owner"


def _share_content(host_header: str) -> "_Content | None | bool":
    """A quick tunnel's share host: the shared app as `_Content` (way "share"),
    False for a host shaped like one that isn't a current share, None for
    any other host."""
    if not app_sharing.is_share_host_shaped(host_header):
        return None
    parsed = app_sharing.parse_share_host(host_header)
    record = app_sharing.current_share(WOLTS_DIR, *parsed) if parsed else None
    if record is None or not record.get("tunnel_url"):
        return False
    public = urlsplit(record["tunnel_url"]).netloc
    return _Content("app", parsed[0], f"app:{parsed[0]}", remote=public, way="share", token=parsed[1])


def _safe_next(value: str) -> str:
    """A path on this same host, or "/"."""
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(
            ord(ch) < 32 for ch in value):
        return "/"
    return value


def _site_url(label: str, port: int | None, path: str = "/") -> str:
    return f"http://{label}.sites.localhost{f':{port}' if port else ''}{path}"


@dataclass(frozen=True)
class _Content:
    """A site or app host the Mill serves, as the lodge sees it."""
    kind: str        # "site" or "app"
    name: str
    grant_label: str
    remote: str = ""   # the public authority, when it came through a way in
    way: str = ""      # which way in (server/ways_in.py), or "share"
    token: str = ""    # a quick tunnel's share token

    @property
    def open_path(self) -> str:
        return (f"/sites/open?wolt={self.name}" if self.kind == "site"
                else f"/apps/open?app={self.name}")


def _content_host(host: str) -> _Content | None:
    site = parse_site_host(host)
    if site is not None:
        return _Content("site", site.label, site.label)
    app_host = parse_app_host(host)
    if app_host is not None:
        return _Content("app", app_host.name, f"app:{app_host.name}")
    remote = _remote_content_host(host)
    if remote is not None:
        return remote
    return None


def _remote_content_host(host_header: str) -> _Content | None:
    """A wolt's site or an app reached through a way in (server/ways_in.py),
    such as `<app>.<tunnel domain>` through Cloudflare."""
    way, public = ways_in.content_host(host_header)
    if public is None:
        return None
    label = public.name if public.kind == "site" else f"app:{public.name}"
    return _Content(public.kind, public.name, label, remote=public.authority, way=way.name)


async def _remote_visitor(scope, headers: dict[str, str], content: "_Content"):
    """(identity, expires_at) for a request through a way in, or (None, None):
    the way in it came through proves the owner its own way."""
    way = ways_in.named(content.way)
    visitor = await way.owner(headers, scope) if way is not None else None
    if visitor is None:
        return None, None
    return visitor.identity, visitor.expires_at


def _public_origin_ok(scope, headers: dict[str, str], content: "_Content") -> bool:
    """A write or a WebSocket through the tunnel must come from the content's
    own public page: `Origin` exactly `https://<its host>`, checked before the
    request is rewritten to its local form. Another origin, a local-form one,
    `null`, or a cross-site fetch is refused. Reads may carry any Origin."""
    origin = headers.get("origin")
    write = scope["type"] == "websocket" or scope.get("method", "GET").upper() not in _READ_ONLY_HTTP_METHODS
    if not write:
        return True
    if headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    # Through the tunnel every caller is a browser past Access, and browsers
    # send Origin on every write: a write without one is refused too.
    return origin == f"https://{content.remote}"


def _remote_sites_enabled(wolt: str) -> bool:
    """Remote sites are off until the owner turns them on.

    `site-<wolt>.<domain>` may once have been an app's origin, whose
    storage and service workers outlive it in visitors' browsers. Only the
    owner knows no app ever used those names: `"mill": {"remote_sites": true}`
    in woltspace.json says so. Even then, while an app named `site-<wolt>`
    still exists (it's no longer served: the name is reserved), the matching
    site stays off.
    """
    try:
        config = json.loads((Path(WOLTS_DIR) / "woltspace.json").read_text())
    except (OSError, ValueError):
        return False
    mill = config.get("mill") if isinstance(config, dict) else None
    if not (isinstance(mill, dict) and mill.get("remote_sites") is True):
        return False
    return get_app(f"site-{wolt}") is None


def _as_local(scope, content: "_Content") -> dict:
    """The same request as if it had come to `<app>.localhost` on this lodge's
    port: the Mill names apps by their local host, and an Origin from the
    app's public page becomes the local one so the Mill's same-page check holds."""
    local = (f"{content.name}.sites.localhost:{PORT}" if content.kind == "site"
             else f"{content.name}.localhost:{PORT}")
    public_origin = f"https://{content.remote}"
    rewritten = []
    for key, value in scope.get("headers", []):
        name = key.lower()
        if name == b"host":
            value = local.encode("latin-1")
        elif name == b"origin":
            if value.decode("latin-1") != public_origin:
                continue  # only the content's own page's Origin is carried
            value = f"http://{local}".encode("latin-1")
        rewritten.append((key, value))
    return dict(scope, headers=rewritten)


class MillForwardMiddleware:
    """Send site and local app hosts to the Mill; everything else on to the lodge."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket"):
            return await self.inner(scope, receive, send)
        hosts = [value.decode("latin-1") for key, value in scope.get("headers", [])
                 if key.lower() == b"host"]
        # A quick tunnel's share host is only ever public: it never reaches
        # the owner's checks, grants or the lodge, whatever it carries.
        shares = [_share_content(host) for host in hosts]
        if any(share is not None for share in shares):
            if len(hosts) != 1 or shares[0] is False:
                return await self._refuse(scope, receive, send, 404, "Not found")
            headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                       for key, value in scope.get("headers", [])}
            return await self._remote(scope, receive, send, headers, shares[0])
        contents = [_content_host(host) for host in hosts]
        if not any(contents):
            return await self.inner(scope, receive, send)
        if len(hosts) != 1:
            return await self._refuse(scope, receive, send, 400, "exactly one Host header required")
        headers = {key.decode("latin-1").lower(): value.decode("latin-1")
                   for key, value in scope.get("headers", [])}
        content = contents[0]
        _host, port = mill_server_split(hosts[0])
        if content.remote:
            return await self._remote(scope, receive, send, headers, content)
        if scope["type"] == "http" and scope.get("path") == _SITE_GRANT_PATH:
            return await self._grant(scope, receive, send, content.grant_label)
        visitor = _site_visitor(scope, headers, content.grant_label)
        if visitor is None:
            if scope["type"] == "http" and lodge_auth.enabled():
                lodge = f"http://localhost{f':{port}' if port else ''}"
                target = _safe_next(scope.get("path", "/"))
                link = f"{lodge}{content.open_path}&path={quote(target, safe='/')}"
                response = HTMLResponse(
                    f"<h1>Open this {content.kind} from the lodge</h1><p><a href=\"{link}\">"
                    f"Open {content.name}</a></p>", status_code=401,
                    headers={"Cache-Control": "no-store"})
                return await response(scope, receive, send)
            return await self._refuse(scope, receive, send, 403, f"This {content.kind} is the owner's")
        if scope["type"] == "websocket":
            return await self._websocket(scope, receive, send, headers, content, visitor=visitor)
        return await self._http(scope, receive, send, headers, content, visitor=visitor)

    async def _remote(self, scope, receive, send, headers, content):
        """A site or app through the tunnel: Access says who, then the same
        path as local."""
        if content.kind == "site" and not _remote_sites_enabled(content.name):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            response = HTMLResponse(
                "<h1>Wolt sites aren't served through the tunnel on this lodge</h1>"
                "<p>See docs/mill.md: remote sites are turned on in woltspace.json.</p>",
                status_code=503)
            return await response(scope, receive, send)
        if not _public_origin_ok(scope, headers, content):
            return await self._refuse(scope, receive, send, 403, "Cross-site request rejected")
        if content.way == "share":
            visitor, expires_at = app_sharing.PUBLIC, None
        else:
            visitor, expires_at = await _remote_visitor(scope, headers, content)
            shared = app_sharing.shared(WOLTS_DIR, content.name) if content.kind == "app" else None
            if visitor is None and shared and shared.get("domain_url") == f"https://{content.remote}":
                visitor, expires_at = app_sharing.PUBLIC, None
                content = replace(content, token=shared.get("token", ""))
        if visitor is None:
            return await self._refuse(scope, receive, send, 403, "This app is the owner's")
        local = _as_local(scope, content)
        local_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in local["headers"]}
        if scope["type"] == "websocket":
            return await self._websocket(local, receive, send, local_headers, content,
                                         expires_at=expires_at, visitor=visitor)
        return await self._http(local, receive, send, local_headers, content, visitor=visitor)

    @staticmethod
    async def _refuse(scope, receive, send, status, message):
        if scope["type"] == "websocket":
            return await send({"type": "websocket.close", "code": 1008})
        response = JSONResponse({"error": message}, status_code=status)
        return await response(scope, receive, send)

    @staticmethod
    def _upstream_headers(headers: dict[str, str]) -> list[tuple[str, str]]:
        """A site's request to the Mill: an allow-list, nothing that identifies."""
        out = [("host", headers["host"]), (mill_server.IDENTITY_HEADER, mill_server.OWNER)]
        out += [(name, value) for name, value in headers.items() if name in _MILL_REQUEST_HEADERS]
        return out

    @staticmethod
    def _app_headers(scope, *, websocket: bool, visitor: str = "owner") -> list[tuple[str, str]]:
        """An app's request to the Mill: what the app's own page sent, minus the
        lodge's and Access's credentials, with the original host kept so the
        Mill knows which app."""
        raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
        helper = websocket_upstream_headers if websocket else upstream_request_headers
        # Every way in's identity headers are dropped too, whichever way the
        # request came, so none can be forged to an app.
        vouching = ways_in.credential_headers()
        filtered = [(k, v) for k, v in helper(raw, 0, is_lodge_key=_is_lodge_key)
                    if k.lower() != "host" and k.lower() not in vouching]
        host = next(v for k, v in raw if k.lower() == "host")
        identity = [(mill_server.IDENTITY_HEADER,
                     mill_server.PUBLIC if visitor == app_sharing.PUBLIC else mill_server.OWNER)]
        return identity + filtered if websocket else [("host", host)] + identity + filtered

    async def _http(self, scope, receive, send, headers, content, visitor="owner"):
        path = scope.get("raw_path") or scope.get("path", "/").encode()
        query = scope.get("query_string") or b""
        url = "http://mill" + path.decode("latin-1") + (("?" + query.decode("latin-1")) if query else "")
        is_app = content.kind == "app"
        client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=str(mill_server.socket_path(WOLTS_DIR))),
            timeout=APP_PROXY_TIMEOUT if is_app else MILL_TIMEOUT,
        )
        has_body = is_app and ("content-length" in headers or "transfer-encoding" in headers)
        try:
            req = client.build_request(
                scope.get("method", "GET"), url,
                headers=(self._app_headers(scope, websocket=False, visitor=visitor) if is_app
                         else self._upstream_headers(headers)),
                content=Request(scope, receive).stream() if has_body else None,
            )
            resp = await client.send(req, stream=True, follow_redirects=False)
        except (httpx.ConnectError, FileNotFoundError, ConnectionRefusedError):
            await client.aclose()
            response = HTMLResponse("<h1>The Mill isn't running, so wolt sites and apps are down</h1>"
                                    "<p>The lodge itself still works.</p>", status_code=502)
            return await response(scope, receive, send)
        except httpx.TimeoutException:
            await client.aclose()
            response = HTMLResponse("<h1>The Mill did not answer in time</h1>", status_code=504)
            return await response(scope, receive, send)
        except BaseException:
            await client.aclose()  # cancelled, or any other failure before a response
            raise

        async def body():
            try:
                async for chunk in resp.aiter_raw():
                    yield chunk
            finally:
                await resp.aclose()
                await client.aclose()

        response = StreamingResponse(body(), status_code=resp.status_code)
        response.raw_headers = [
            (name.encode("latin-1"), value.encode("latin-1"))
            for name, value in client_response_headers(resp.headers.multi_items())
        ]
        return await response(scope, receive, send)

    async def _websocket(self, scope, receive, send, headers, content, expires_at=None, visitor="owner"):
        from websockets.asyncio.client import unix_connect
        ws = WebSocket(scope, receive, send)
        query = scope.get("query_string") or b""
        raw_path = (scope.get("raw_path") or scope.get("path", "/").encode()).decode("latin-1")
        uri = f"ws://{headers['host']}{raw_path}" + ("?" + query.decode("latin-1") if query else "")
        protocols = None
        if content.kind == "app":
            extra = self._app_headers(scope, websocket=True, visitor=visitor)
            protocols = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",")
                         if p.strip()] or None
        else:
            extra = [(mill_server.IDENTITY_HEADER, mill_server.OWNER)]
            if headers.get("origin"):
                extra.append(("Origin", headers["origin"]))
        try:
            upstream = await unix_connect(str(mill_server.socket_path(WOLTS_DIR)), uri,
                                          additional_headers=extra, subprotocols=protocols,
                                          max_size=APP_WS_MAX_MESSAGE)
        except Exception:
            return await send({"type": "websocket.close", "code": 1008})
        label = content.grant_label

        async def authority_holds():
            # On a monotonic timer, whatever the traffic: an expired or
            # forgotten session ends the socket too. Through the tunnel, the
            # Access token's expiry is the bound.
            # A shared app's visitor holds only while that same share does: an
            # unshare and re-share ends the old share's sockets too.
            if visitor == app_sharing.PUBLIC:
                while True:
                    await asyncio.sleep(SITE_SOCKET_RECHECK_SECONDS)
                    if app_sharing.current_share(WOLTS_DIR, content.name, content.token) is None:
                        return
            if content.remote:
                await asyncio.sleep(max(0.0, (expires_at or time.time()) - time.time()))
                return
            while True:
                await asyncio.sleep(SITE_SOCKET_RECHECK_SECONDS)
                if _site_visitor(scope, headers, label) is None:
                    return

        try:
            await ws.accept(subprotocol=upstream.subprotocol)
            tasks = [asyncio.ensure_future(relay_websocket(ws, upstream)),
                     asyncio.ensure_future(authority_holds())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            await upstream.close()
            try:
                await ws.close()
            except Exception:
                pass

    @staticmethod
    async def _grant(scope, receive, send, label):
        """Turn a one-time grant from /sites/open or /apps/open into this host's own cookie."""
        request = Request(scope, receive)
        no_leak = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
        if not lodge_auth.enabled() or request.method != "GET":
            response = JSONResponse({"error": "not found"}, status_code=404)
            return await response(scope, receive, send)
        if _pairing_failures.blocked():
            response = JSONResponse({"error": "too many attempts; wait a minute"},
                                    status_code=429, headers=no_leak)
            return await response(scope, receive, send)
        token = lodge_auth.redeem_site_grant(WOLTS_DIR, label, request.query_params.get("code", ""))
        if token is None:
            _pairing_failures.record()
            response = HTMLResponse("<h1>That link was already used, or has expired</h1>"
                                    "<p>Open it from the lodge again.</p>",
                                    status_code=403, headers=no_leak)
            return await response(scope, receive, send)
        response = RedirectResponse(_safe_next(request.query_params.get("next", "/")),
                                    status_code=303, headers=no_leak)
        response.set_cookie(lodge_auth.SITE_COOKIE, token, max_age=lodge_auth.SITE_SESSION_TTL,
                            path="/", httponly=True, samesite="lax")
        return await response(scope, receive, send)


def mill_server_split(host_header: str) -> tuple[str, int | None]:
    from .content_hosts import split_host
    return split_host(host_header) or ("", None)


app.add_middleware(MillForwardMiddleware)


def _remote_base(request: Request, kind: str, name: str) -> str:
    """Where a browser that reached the lodge through a way in opens this site
    or app (`https://<app>.<tunnel domain>` through Cloudflare), else "": a
    remote browser can't open a `*.localhost` address."""
    from .content_hosts import split_host
    host = (split_host(request.headers.get("host", "")) or ("", None))[0]
    return ways_in.public_base(host, kind, name)


@app.get("/apps/open")
async def open_app(request: Request, app_name: str = Query("", alias="app"), path: str = "/"):
    """Open an app on its own origin, with a one-time grant when auth is on."""
    if lodge_auth.enabled() and _lodge_auth_scope(request.headers) != lodge_auth.OWNER:
        return JSONResponse({"error": "only the owner can open an app"}, status_code=403)
    name = (app_name or "").lower()
    if parse_app_host(f"{name}.localhost") is None or get_app(name) is None:
        return JSONResponse({"error": "no such app"}, status_code=404)
    _host, port = mill_server_split(request.headers.get("host", ""))
    target = _safe_next(path)
    base = f"http://{name}.localhost{f':{port}' if port else ''}"
    headers = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
    remote = _remote_base(request, "app", name)
    if remote:
        return RedirectResponse(remote + target, status_code=303, headers=headers)
    if not lodge_auth.enabled():
        return RedirectResponse(base + target, status_code=303, headers=headers)
    code = lodge_auth.new_site_grant(WOLTS_DIR, f"app:{name}")
    grant = f"{_SITE_GRANT_PATH}?code={code}&next={quote(target, safe='/')}"
    return RedirectResponse(base + grant, status_code=303, headers=headers)


@app.get("/sites/open")
async def open_site(request: Request, wolt: str = "", path: str = "/"):
    """Open a wolt's site on its own origin, with a one-time grant when auth is on."""
    if lodge_auth.enabled() and _lodge_auth_scope(request.headers) != lodge_auth.OWNER:
        return JSONResponse({"error": "only the owner can open a site"}, status_code=403)
    label = (wolt or "").lower()
    from .content_hosts import anchored_site_root
    wolt_dir = _find_wolt_dir(WOLTS_DIR, label) if parse_site_host(f"{label}.sites.localhost") else None
    if wolt_dir is None or anchored_site_root(wolt_dir) is None:
        return JSONResponse({"error": "no such wolt site"}, status_code=404)
    _host, port = mill_server_split(request.headers.get("host", ""))
    target = _safe_next(path)
    headers = {"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}
    remote = _remote_base(request, "site", label)
    if remote and not _remote_sites_enabled(label):
        return JSONResponse({"error": "wolt sites aren't served through the tunnel on this lodge"},
                            status_code=503)
    if remote:
        # Through the tunnel: Cloudflare Access is the site's door.
        return RedirectResponse(remote + target, status_code=303, headers=headers)
    if not lodge_auth.enabled():
        return RedirectResponse(_site_url(label, port, target), status_code=303, headers=headers)
    code = lodge_auth.new_site_grant(WOLTS_DIR, label)
    grant = f"{_SITE_GRANT_PATH}?code={code}&next={quote(target, safe='/')}"
    return RedirectResponse(_site_url(label, port, grant), status_code=303, headers=headers)


@app.options("/{path:path}")
async def options_handler():
    return Response(status_code=204)


# --- Helpers ---

def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _inject_livereload(html: str) -> str:
    if "</body>" in html:
        return html.replace("</body>", LIVERELOAD_SCRIPT + "</body>")
    return html + LIVERELOAD_SCRIPT


# jerpint: what do we serve as static vs not?
# overall i think it might be simpler to think of each serve as a single service
# tbd how memory intensive this is, but e.g. each session is powered by its own single python --serve index.html with
# its own port so theyre really easy to just swap out and share. we shouldnt have different routes for different types
# maybe static pages an have something less overkill, but it would be nice to have a unified way of doing things
async def _serve_static(url_path: str, request: Request | None = None) -> Response | None:
    """Serve from wolt/site/."""
    full_path = SITE_DIR / url_path.lstrip("/")
    if not full_path.exists() or not full_path.is_file():
        return None
    # Security: stay inside SITE_DIR
    try:
        full_path.resolve().relative_to(SITE_DIR.resolve())
    except ValueError:
        return None
    content = full_path.read_bytes()
    ext = full_path.suffix
    is_iframe = request and request.headers.get("sec-fetch-dest") == "iframe"
    if ext == ".html" and not is_iframe:
        html = _inject_livereload(content.decode())
        return HTMLResponse(html, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    mime = MIME_TYPES.get(ext, "application/octet-stream")
    return Response(content, media_type=mime, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


# jerpint: will have to look into how this works and security around it
async def _serve_platform_file(filename: str) -> Response | None:
    """Serve from public/ (platform UI), with the type the file actually is.

    This used to answer `text/html` for everything in `public/`, which is fine
    right up until the browser is asked to *execute* one: chromium refuses
    `/sw.js` with "unsupported MIME type ('text/html')", so the service worker
    never registers and woltspace is quietly not installable as a PWA. The
    sibling `_serve_static` had the lookup right all along — same table here.
    """
    path = PUBLIC_DIR / filename
    if not path.exists() or not path.is_file():
        return None
    ext = path.suffix
    mime = MIME_TYPES.get(ext) or APP_MIME_TYPES.get(ext)
    headers = {"Cache-Control": "no-cache, no-store, must-revalidate"}
    if mime == "text/html":
        return HTMLResponse(path.read_text(), headers=headers)
    # Bytes, not text: fonts, icons and images in public/ are not decodable.
    return Response(path.read_bytes(),
                    media_type=mime or "application/octet-stream", headers=headers)


# ============================================================
# ROUTES
# ============================================================

# jerpint: good idea, but eventually should be tied to version of woltspace package not just plaintext
@app.get("/version")
async def version():
    return PlainTextResponse("woltspace-v1")


# --- Viewport control ---

# jerpint: not sure if a concept of current is useful, but could be, e.g. current vs last
@app.post("/current")
async def post_current(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    body = await request.json()
    url = body.get("url")
    if url:
        # set_viewport writes the record under the session's flock, which a rest
        # holds while it stops tmux: never wait for it on the event loop.
        await asyncio.to_thread(set_current_url, url, session, body.get("port", 7777))
        log_view(url, body.get("title"))
    return {"url": get_current_url(session)}


@app.get("/current")
async def get_current(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    url = get_current_url(session)
    if url:
        return RedirectResponse(url, status_code=302)
    return Response(status_code=204)


@app.get("/current/meta")
async def get_current_meta_route(request: Request):
    session = sanitize_session(request.query_params.get("session", "main"))
    return get_current_meta(session)


# jerpint: why this?
@app.post("/sessions/redirect")
async def post_session_redirect(request: Request):
    body = await request.json()
    from_s = body.get("from")
    to_s = body.get("to")
    if not from_s or not to_s:
        return JSONResponse({"error": "from and to required"}, status_code=400)
    safe_from = sanitize_session(from_s)
    safe_to = sanitize_session(to_s)
    from sessions import SessionRegistry
    reg = SessionRegistry(WOLTS_DIR)
    reg.set_redirect(safe_from, safe_to)
    print(f"[redirect] {safe_from} → {safe_to}")
    return {"ok": True}


def _extract_login_url() -> str:
    """Capture tmux main pane and extract the OAuth login URL.

    The URL wraps across multiple lines in the terminal, so we find the
    starting line and concatenate continuation lines until we hit a blank
    or non-URL line.
    """
    try:
        # -S -200 on purpose here: the login URL has usually scrolled out of
        # the visible pane by the time this is polled.
        pane = get_runtime().capture(RuntimeHandle("main", "main"), start="-200")
        lines = pane.splitlines()
        url = ""
        capturing = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("https://claude.com/"):
                url = stripped
                capturing = True
            elif capturing:
                # URL continuation lines contain path/query chars, no spaces
                if stripped and " " not in stripped and ("%" in stripped or "&" in stripped or "=" in stripped):
                    url += stripped
                else:
                    capturing = False
        return url
    except Exception:
        pass
    return ""


@app.get("/onboard-status")
async def onboard_status():
    env = load_dotenv()
    return {
        "wolts_dir": str(WOLTS_DIR),
        "wolt_name": WOLT_NAME,
        "has_oauth": claude_authenticated(CONTAINER_HOME),
        # Which credential answered — "credentials-file", "env-token" or "none".
        # Presence is not validity: a dead env token still reads as authenticated,
        # and this is how someone debugging that finds out what to remove.
        "auth_source": auth_source(CONTAINER_HOME),
        "has_llm_key": bool(env.get("ANTHROPIC_API_KEY") or env.get("OPENROUTER_API_KEY")),
        "has_telegram": env.get("ENABLE_TELEGRAM_BOT") == "true" and bool(env.get("TELEGRAM_BOT_TOKEN")),
        "login_url": _extract_login_url(),
    }


@app.get("/views/history")
async def views_history():
    return read_views_history(100)


# --- Notify ---

def _touch_origin(name: str) -> None:
    """A session that talks is in use: its own notify, message or spawn stamps it.

    Only last_activity, only on a running record, through the locked touch();
    never the status, so a resting sender stays resting. The name comes from
    the caller, on the same trust as /notify's session and /current.
    """
    if not name or sanitize_session(name) != name or name == "main":
        return
    from sessions import SessionRegistry
    try:
        SessionRegistry(WOLTS_DIR).touch(name)
    except Exception as exc:
        # Stamping use never fails the send it rides on.
        print(f"[sessions] could not stamp use on {name}: {exc}")


@app.post("/notify")
async def post_notify(request: Request):
    body = await request.json()
    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message required"}, status_code=400)
    session = body.get("session", "")
    # Explicit adapter routing — caller says where to send
    explicit = {}
    if body.get("adapter"):
        explicit["adapter"] = body["adapter"]
        if body["adapter"] == "slack":
            explicit["channel"] = body.get("channel", "")
            explicit["thread_ts"] = body.get("thread_ts")
        elif body["adapter"] == "telegram":
            explicit["chat_id"] = body.get("chat_id", "")
        else:
            # A connector declared in the registry: its own route text (notify --to).
            explicit["route"] = body.get("route", "")
    # Outbox entries `notify --file` staged: [{"id", "name"}]. Never a path.
    attachments = body.get("attachments") or []
    # `notify --speak`: {"text"}, the words the lodge is to say. Passed on only
    # when the request has it, so every other call is the one it always was.
    speak = body.get("speak")
    try:
        result = await send_notification(
            session, message, explicit=explicit or None, attachments=attachments,
            **({} if speak is None else {"speak": speak}),
        )
        print(f"[notify] → {result.get('adapter')} | {message[:80]}")
        bot_log("notify_sent", {"session": session, **result, "message": message})
        await asyncio.to_thread(_touch_origin, session)
        return {"ok": True, **result}
    except NoNotificationTarget as e:
        # Not a server fault: no chat has been connected yet. 409 so a caller
        # can tell "woltspace is broken" from "you have not set this up".
        print(f"[notify] undeliverable: {e}")
        return JSONResponse({
            "ok": False,
            "error": str(e),
            "reason": "no_notification_target",
            "remedy": "Set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS in the data "
                      "root's .env, or run the /telegram skill to connect a chat.",
        }, status_code=409)
    except AttachmentError as e:
        # The file cannot go, and `reason` tells the caller which rule it met.
        print(f"[notify] attachment refused: {e}")
        return JSONResponse(
            {"ok": False, "error": str(e), "reason": e.reason},
            status_code=413 if e.reason == "attachment_too_large" else 400,
        )
    except SpeechError as e:
        # The words could not be made into audio; nothing was sent.
        print(f"[notify] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} speech failed: "
              f"session={session or '-'}: {e}")
        return JSONResponse(
            {"ok": False, "error": str(e), "reason": e.reason},
            status_code=502 if e.reason == "speech_failed" else 400,
        )
    except Exception as e:
        # Some errors carry no text (an httpx timeout, say); name them instead.
        error = str(e) or type(e).__name__
        print(f"[notify] error: {error}")
        return JSONResponse({"error": error}, status_code=500)


# --- Memory ---

@app.post("/memory/read")
async def memory_read(request: Request):
    body = await request.json()
    mem_path = body.get("path")
    if not mem_path or not isinstance(mem_path, str):
        return JSONResponse({"error": "path required"}, status_code=400)
    memory_dir = WOLT_DIR / "wolt" / "memory"
    abs_path = (memory_dir / mem_path).resolve()
    if not str(abs_path).startswith(str(memory_dir.resolve())):
        return JSONResponse({"error": "path outside memory directory"}, status_code=403)
    if not abs_path.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        content = abs_path.read_text()
        return {"path": mem_path, "content": content}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Session messaging ---

async def _deliver_or_resume(safe: str, text: str, body: dict):
    result = await asyncio.to_thread(
        deliver_message,
        safe,
        text,
        from_wolt=body.get("from_wolt", "") or "",
        from_session=body.get("from_session", "") or "",
    )
    status = result.get("status")
    sender = body.get("from_session", "") or ""
    if status == "delivered":
        print(f"[message] → {safe}: {text[:80]}")
        await asyncio.to_thread(_touch_origin, sender)
        return {"ok": True, **result}
    if status in {"session-dead", "agent-gone"}:
        from sessions import SessionRegistry
        record = SessionRegistry(WOLTS_DIR).get(safe, check_alive=False)
        if not record or record.get("status") not in {"running", "resting"}:
            return JSONResponse({"ok": False, **result}, status_code=409)
        prompt = format_attributed_message(
            text,
            body.get("from_wolt", "") or "",
            body.get("from_session", "") or "",
        )
        try:
            resumed = await asyncio.to_thread(resume_session, safe, prompt)
            print(f"[message] resumed → {safe}: {text[:80]}")
            await asyncio.to_thread(_touch_origin, sender)
            return {"ok": True, **resumed, "status": "resumed", "session": safe}
        except (ValueError, ResumeUnavailable, ResumeFailed, subprocess.CalledProcessError) as exc:
            return JSONResponse(
                {"ok": False, "status": status, "session": safe, "error": str(exc)},
                status_code=409,
            )
    code = 404 if status == "no-session" else 409
    return JSONResponse({"ok": False, **result}, status_code=code)


@app.post("/sessions/{session_id}/describe")
async def session_describe(session_id: str, request: Request):
    """Give a session a concise title and one-line summary."""
    from sessions import SessionRegistry

    safe = sanitize_session(session_id)
    if not safe or safe != session_id:
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    title = body.get("title")
    summary = body.get("summary")
    if not isinstance(title, str) or not title.strip():
        return JSONResponse({"error": "title required"}, status_code=400)
    if not isinstance(summary, str) or not summary.strip():
        return JSONResponse({"error": "summary required"}, status_code=400)
    title = " ".join(title.split())
    summary = " ".join(summary.split())
    title = "".join(c for c in title if not unicodedata.category(c).startswith("C"))
    summary = "".join(c for c in summary if not unicodedata.category(c).startswith("C"))
    if not title:
        return JSONResponse({"error": "title required"}, status_code=400)
    if not summary:
        return JSONResponse({"error": "summary required"}, status_code=400)
    if len(title) > 80:
        return JSONResponse({"error": "title must be 80 characters or fewer"}, status_code=400)
    if len(summary) > 240:
        return JSONResponse({"error": "summary must be 240 characters or fewer"}, status_code=400)
    described = SessionRegistry(WOLTS_DIR).describe(safe, title, summary)
    if described is None:
        return JSONResponse({"error": "session not found"}, status_code=404)
    return described


_HOLD_REASON_MAX = 200


def _hold_reason(value) -> str:
    """One line, no control characters, like a session description."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    text = "".join(c for c in text if not unicodedata.category(c).startswith("C"))
    return " ".join(text.split())


def _hold_locked(safe: str, seconds: int | None, reason: str):
    from sessions import SessionRegistry
    with SessionRegistry(WOLTS_DIR).edit(safe) as data:
        if data is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if data.get("status") != "running":
            return JSONResponse({"error": f"session is {data.get('status') or 'not running'}; only a running session can hold"},
                                status_code=409)
        now = int(time.time())
        data["hold"] = {"until": None if seconds is None else now + seconds, "reason": reason, "since": now}
        data["last_activity"] = now  # taking a hold is use
    return {"ok": True, "session": safe, "hold": data["hold"]}


def _release_hold_locked(safe: str):
    from sessions import SessionRegistry
    with SessionRegistry(WOLTS_DIR).edit(safe) as data:
        if data is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        # A raw write: releasing is not use, so an idle session rests on the next sweep.
        data.pop("hold", None)
    return {"ok": True, "session": safe, "hold": None}


@app.post("/sessions/{session_id}/hold")
async def hold_session(session_id: str, request: Request):
    """Keep a running session from resting while a background task runs in it.

    Body: {"seconds": N | null, "reason": "..."}; null holds until released.
    """
    safe = sanitize_session(session_id)
    if not safe or safe != session_id:
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if "seconds" not in body:
        return JSONResponse({"error": "seconds required: a whole number of seconds, or null for forever"}, status_code=400)
    seconds = body["seconds"]
    if seconds is not None and (not isinstance(seconds, int) or isinstance(seconds, bool)
                                or not 0 < seconds <= HOLD_MAX_SECONDS):
        return JSONResponse({"error": "seconds must be a positive whole number, or null for forever"}, status_code=400)
    reason = _hold_reason(body.get("reason"))
    if not reason:
        return JSONResponse({"error": "reason required: name the task that is running"}, status_code=400)
    if len(reason) > _HOLD_REASON_MAX:
        return JSONResponse({"error": f"reason must be {_HOLD_REASON_MAX} characters or fewer"}, status_code=400)
    return await asyncio.to_thread(_hold_locked, safe, seconds, reason)


@app.delete("/sessions/{session_id}/hold")
async def release_hold(session_id: str):
    """Drop a session's hold. It rests at the next sweep only if nobody is using it."""
    safe = sanitize_session(session_id)
    if not safe or safe != session_id:
        return JSONResponse({"error": "invalid session id"}, status_code=400)
    return await asyncio.to_thread(_release_hold_locked, safe)


@app.post("/sessions/{session_id}/message")
async def session_message(session_id: str, request: Request):
    """Deliver to a running session, or resume a resting one on contact."""
    safe = sanitize_session(session_id)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    return await _deliver_or_resume(safe, text, body)


def _rest_session_locked(safe: str, idle_since: int):
    """Verify and stop one idle session without blocking the server event loop.

    The authority judges the rule itself: the session is running, can come
    back, holds nothing, and nobody has used it since idle_since or for the
    whole window. stop_and_settle then claims it `stopping` only if no use, hold
    or resume came after the judgment, and stops tmux outside any write.
    """
    from sessions import SessionRegistry, stamp_resume_id, stop_and_settle
    registry = SessionRegistry(WOLTS_DIR)
    data = registry.get(safe, check_alive=False)
    if data is None:
        return JSONResponse({"error": "session not found"}, status_code=404)
    wolt = data.get("wolt", "")
    # Resting must be reversible: recover a missing id first, outside the write
    # below (recovery reads the engine's files).
    if data.get("status") == "running" and not stored_resume_id(data):
        claim_resume_id(registry, data, lambda d: stamp_resume_id(
            registry, wolt, safe, d["harness_session_id"]))
    data = registry.get(safe, wolt=wolt, check_alive=False)
    if data is None:
        return JSONResponse({"error": "session not found"}, status_code=404)
    if data.get("status") != "running":
        return {"ok": True, "status": data.get("status"), "session": safe}
    if not stored_resume_id(data):
        return JSONResponse({"error": "session has no conversation to resume; not resting it"}, status_code=409)
    now = int(time.time())
    if hold_active(data, now):
        return JSONResponse({"error": "session is held; not resting it"}, status_code=409)
    try:
        used = last_use(data, get_runtime().last_keys())
    except Exception:
        used = None
    if used is None:
        return JSONResponse({"error": "session activity could not be verified"}, status_code=409)
    if used > idle_since:
        return JSONResponse({"error": "session was used since; rest aborted"}, status_code=409)
    # The authority judges the quiet window itself, never the caller. This
    # also closes the same-second gap: use in the second the vulture
    # looked is "now", far inside any window.
    timeout = get_idle_timeout()
    if timeout is None or now - used < timeout:
        return JSONResponse({"error": "session is not idle long enough; rest aborted"}, status_code=409)
    stopped = stop_and_settle(
        registry, safe, data,
        lambda seen: get_runtime().stop(RuntimeHandle.from_record(seen)),
        lambda record: record.update(status="resting", rested_at=now, rest_reason="idle"),
        wolt=wolt,
    )
    if stopped is None:
        return JSONResponse({"error": "session was used since; rest aborted"}, status_code=409)
    if not stopped:
        return JSONResponse({"error": "session could not be stopped; still running"}, status_code=409)
    return {"ok": True, "status": "resting", "session": safe, "rested_at": now}


@app.post("/sessions/{session_id}/rest")
async def rest_session(session_id: str, request: Request):
    """Stop one idle runtime while keeping its conversation resumable."""
    safe = sanitize_session(session_id)
    if safe == "main":
        return JSONResponse({"error": "main session cannot rest"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    # The last use the caller judged idle; a newer one aborts.
    idle_since = body.get("idle_since") if isinstance(body, dict) else None
    if (not isinstance(idle_since, int) or isinstance(idle_since, bool)
            or not 0 < idle_since <= time.time()):
        return JSONResponse({"error": "idle_since required"}, status_code=400)
    return await asyncio.to_thread(_rest_session_locked, safe, idle_since)


@app.post("/wolts/{name}/message")
async def wolt_message(name: str, request: Request):
    """Message a wolt by NAME — resolves to its most-recently-active live session.

    Same body as /sessions/{id}/message. 404 if the wolt has no live session.
    This is the ergonomic entry point: senders address `codexw`, not a slug.
    """
    safe_wolt = "".join(c for c in name if c.isalnum() or c in "-_")
    from sessions import SessionRegistry
    registry = SessionRegistry(WOLTS_DIR)
    session_id = resolve_active_session(safe_wolt, registry=registry)
    if not session_id:
        resting = [s for s in registry.list(wolt=safe_wolt) if s.get("status") in ("resting", "stopping")]
        resting.sort(key=lambda s: (s.get("last_activity") or 0, s.get("created_at") or 0), reverse=True)
        session_id = resting[0]["name"] if resting else None
    if not session_id:
        return JSONResponse(
            {"ok": False, "status": "no-session", "wolt": safe_wolt,
             "error": f"no live session for wolt '{safe_wolt}'"},
            status_code=404,
        )
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    result = await _deliver_or_resume(session_id, text, body)
    if isinstance(result, dict):
        result["wolt"] = safe_wolt
    return result


# --- Session spawning ---
# All session creation goes through start_session() from container/lib/sessions.py.
# Each adapter (lodge, telegram, slack) has its own route for adapter-specific params.

@app.post("/sessions/new/create")
async def session_new_create(request: Request):
    """Create a new wolt and start its first session.

    Expects JSON body with:
      - name: wolt name (required, lowercase alphanumeric + hyphens)
      - type: creature type (required: otter, beaver, raccoon, or one of the
        lodge's custom creatures)
      - harness: engine for this wolt (optional, lodge default otherwise)
      - model: model for this wolt (optional, the tier default otherwise)

    The server scaffolds the full wolt directory before spawning the session.
    External mode adds per-wolt harness isolation; native mode inherits host auth.
    """
    body = await request.json()
    from wolts import (
        WOLT_DISPLAY_NAME_MAX, clean_display_name, slugify_wolt_name,
    )
    # The typed name is what people read; the lodge derives the folder name.
    typed_name = clean_display_name(body.get("name"))
    wolt_name = slugify_wolt_name(typed_name)
    wolt_type = (body.get("type") or "").strip().lower()
    requested_harness = (body.get("harness") or "").strip()
    selected_harness = requested_harness or get_default_harness()

    # Validate name
    if not typed_name:
        return JSONResponse({"detail": "name is required"}, status_code=400)
    if len(typed_name) > WOLT_DISPLAY_NAME_MAX:
        return JSONResponse(
            {"detail": f"name must be {WOLT_DISPLAY_NAME_MAX} characters or less"},
            status_code=400,
        )
    if not wolt_name:
        return JSONResponse({"detail": "name needs at least one letter"}, status_code=400)
    if (WOLTS_DIR / wolt_name).exists():
        return JSONResponse(
            {"detail": f"a wolt named {wolt_name} already exists"}, status_code=409,
        )

    # Validate type — only rodent types can be created from the lodge: the
    # three sizes, or a custom creature (which is one of them underneath).
    from creature_types import is_custom
    if wolt_type not in ("otter", "beaver", "raccoon") and not is_custom(wolt_type):
        return JSONResponse(
            {"detail": "type must be otter, beaver, raccoon, or a custom creature"}, status_code=400)
    if selected_harness not in HARNESSES:
        return JSONResponse({"detail": f"unknown harness: {selected_harness}"}, status_code=400)
    requested_model = body.get("model") or ""
    if not isinstance(requested_model, str):
        return JSONResponse({"detail": "model must be a string"}, status_code=400)
    requested_model = requested_model.strip()
    if requested_model and not is_valid_model(selected_harness, requested_model):
        return JSONResponse(
            {"detail": f"unknown model for {selected_harness}: {requested_model}"},
            status_code=400,
        )
    # Refuse before scaffolding: a wolt whose engine is missing would only get
    # a session that exits 127, with the reason buried in the boot log.
    problem = harness_install_problem(
        selected_harness, isolation=RuntimeContext.from_env().isolation,
    )
    if problem:
        return JSONResponse({"detail": problem}, status_code=400)

    try:
        # Step 1: Scaffold the wolt with environment-appropriate harness config.
        from wolts import create_creature_wolt
        # Only an explicit request becomes a durable per-wolt override. An API
        # caller that omits harness keeps following the lodge default later.
        # A chosen model is pinned with its harness: a pin only means something
        # on the engine it was picked for.
        create_creature_wolt(
            wolt_name, wolt_type,
            harness=selected_harness if requested_model else requested_harness,
            model=requested_model,
            display_name=typed_name,
        )
        print(f"[sessions/create] scaffolded wolt '{wolt_name}' ({wolt_type})")

        # Step 2: Start a session — full isolation, site auto-start, viewport
        result = start_session(
            wolt=wolt_name,
            # How create-wolt is spelled follows this wolt's harness AND its
            # skills delivery — a freshly scaffolded wolt is on the copy path,
            # so it gets the copy path's names.
            prompt=platform_skill_invoke(
                selected_harness, "create-wolt",
                delivery=wolt_skills_delivery(WOLTS_DIR / wolt_name)),
            harness=selected_harness,
            workdir=body.get("workdir"),
            execution_policy=body.get("execution_policy"),
            routing={"adapter": "lodge"},
        )
        print(f"[sessions/create] spawned {result['name']} for {wolt_name}")
        return result
    except PermissionError as e:
        return JSONResponse({"detail": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"detail": str(e)}, status_code=409)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)


@app.post("/sessions/new/lodge")
async def session_new_lodge(request: Request):
    """Start a session from the lodge (home page gnaw button).

    Also the transport for `woltspace session spawn`: an optional
    from_wolt/from_session pair attributes the spawner, and the seed prompt
    gets a `[spawned by ...]` header so the child knows its parent and how to
    IWCL back. Absent (lodge UI, wolf scheduler), the prompt passes verbatim.
    """
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    prompt = format_spawned_prompt(
        body.get("prompt", ""),
        from_wolt=body.get("from_wolt", "") or "",
        from_session=body.get("from_session", "") or "",
    )
    try:
        result = start_session(
            wolt=wolt,
            prompt=prompt,
            creature=body.get("creature", ""),
            app=body.get("app", ""),
            workdir=body.get("workdir"),
            execution_policy=body.get("execution_policy"),
            routing={"adapter": "lodge"},
        )
        # Site auto-start + viewport URL handled by start_session()
        print(f"[sessions/lodge] spawned {result['name']} for {wolt}")
        await asyncio.to_thread(_touch_origin, body.get("from_session", "") or "")
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except HarnessNotInstalled as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/new/telegram")
async def session_new_telegram(request: Request):
    """Start a session from Telegram."""
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    try:
        result = start_session(
            wolt=wolt,
            prompt=body.get("prompt", ""),
            creature=body.get("creature", ""),
            app=body.get("app", ""),
            workdir=body.get("workdir"),
            execution_policy=body.get("execution_policy"),
            routing={
                "adapter": "telegram",
                "chat_id": body.get("chat_id", ""),
                "user_id": body.get("user_id", ""),
            },
        )
        # Site auto-start + viewport URL handled by start_session()
        print(f"[sessions/telegram] spawned {result['name']} for {wolt}")
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/new/slack")
async def session_new_slack(request: Request):
    """Start a session from Slack."""
    body = await request.json()
    wolt = body.get("wolt")
    if not wolt:
        return JSONResponse({"error": "wolt required"}, status_code=400)
    try:
        result = start_session(
            wolt=wolt,
            prompt=body.get("prompt", ""),
            creature=body.get("creature", ""),
            app=body.get("app", ""),
            workdir=body.get("workdir"),
            execution_policy=body.get("execution_policy"),
            routing={
                "adapter": "slack",
                "chat_id": body.get("channel", ""),
                "user_id": body.get("user_id", ""),
                "thread_ts": body.get("thread_ts", ""),
            },
        )
        # Site auto-start + viewport URL handled by start_session()
        print(f"[sessions/slack] spawned {result['name']} for {wolt}")
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Sessions list ---

def _show_hold(session: dict, now: int) -> bool:
    """Leave only a hold in force on a session row; one that has ended waits
    for the next write to drop it. Returns whether the session holds."""
    held = hold_active(session, now)
    hold = session.pop("hold", None)
    if held:
        session["hold"] = public_hold(hold)
    return held


def _last_keys() -> dict[str, int]:
    """tmux's last client input per session; empty when tmux cannot say."""
    try:
        return get_runtime().last_keys()
    except Exception:
        return {}


@app.get("/sessions")
async def list_sessions(view: str = ""):
    from sessions import SessionRegistry
    reg = SessionRegistry(WOLTS_DIR)
    if view == "lodge":
        payload = await asyncio.to_thread(reg.list_lodge_view)
        sessions = payload["sessions"]
    else:
        sessions = reg.list()
    timeout = get_idle_timeout()
    keys = await asyncio.to_thread(_last_keys) if timeout is not None else {}
    now = int(time.time())
    for session in sessions:
        session["idle_timeout_seconds"] = timeout
        held = _show_hold(session, now)
        if session.get("status") != "running":
            continue
        used = last_use(session, keys)
        if timeout is not None and used is not None:
            session["idle_seconds"] = max(0, now - used)
            # A held session does not close; it rests only after its hold ends.
            if not held:
                session["closes_in_seconds"] = max(0, timeout - session["idle_seconds"])
    sessions.sort(key=lambda s: (0 if s.get("status") == "running" else 1, -(s.get("created_at") or 0)))
    return payload if view == "lodge" else sessions


@app.get("/sessions/{name}")
async def get_session(name: str):
    """Return one session with its agent-accurate liveness state."""
    from sessions import SessionRegistry
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if safe != name:
        return JSONResponse({"error": "not found"}, status_code=404)
    session = await asyncio.to_thread(
        SessionRegistry(WOLTS_DIR).get, safe, check_alive=True,
    )
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    _show_hold(session, int(time.time()))
    return session


# --- Sparks ---
# jerpint: i think the concept of sparks will disappear, or be renamed and rethought as a concept
# wee will need session history, and within a session we might want to support versionoing (though maybe just let users
# use git for that)

@app.get("/history")
async def history():
    return await list_sparks()


@app.get("/history/{spark_id}/meta")
async def history_meta(spark_id: str):
    try:
        data = await get_spark_with_chain(spark_id)
        data.pop("html", None)
        return data
    except Exception:
        return JSONResponse({"error": "not found"}, status_code=404)


# A sandbox without allow-same-origin runs a page in an opaque origin: its
# script can't read the lodge's responses or send a same-origin write, so
# wolt-written HTML the lodge must still show can't act as the owner.
OPAQUE_PAGE_CSP = "sandbox allow-scripts allow-forms allow-popups allow-modals"


def _opaque_page() -> dict[str, str]:
    return {"content-security-policy": OPAQUE_PAGE_CSP}


@app.get("/history/{spark_id}")
async def history_detail(spark_id: str):
    try:
        data = await get_spark_with_chain(spark_id)
        return HTMLResponse(
            data["html"],
            headers={
                **_opaque_page(),
                "x-spark-id": data["id"],
                "x-spark-parent": data.get("parentId") or "",
                "x-spark-child": data.get("childId") or "",
                "x-spark-version": str(data["version"]),
                "x-spark-total": str(data["totalVersions"]),
            },
        )
    except Exception:
        return PlainTextResponse("spark not found", status_code=404)


# --- Wolts ---

def _configured_wolts() -> list[dict]:
    """List wolts from disk for both API and server-rendered screens."""
    wolts = []
    if WOLTS_DIR.exists():
        for entry in sorted(WOLTS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            wolt_json = entry / "wolt" / "wolt.json"
            if not wolt_json.exists():
                continue
            try:
                config = json.loads(wolt_json.read_text())
                wolts.append({
                    "dir": entry.name,
                    "home": str(entry.resolve()),
                    **config,
                    # How every surface shows this wolt; a custom creature's
                    # own emoji included (creature_types).
                    "emoji": creature_emoji(config.get("type") or "rodent"),
                    # What it behaves like: a custom creature's size, else its type.
                    "base_type": base_type(config.get("type") or "rodent"),
                    # A custom creature's own model per engine, so pages show
                    # the model a session would really get (see woltDefaultModel).
                    "creature_models": custom_creature_models(config.get("type")),
                })
            except Exception:
                pass
    return wolts


@app.get("/wolts")
async def list_wolts():
    """List all wolts by scanning WOLTS_DIR for wolt/wolt.json files."""
    return _configured_wolts()


# --- Wolf 🐺 ---
# Read-only windows onto the scheduler's own state. The wolf already writes
# everything here — per-cron last-run stamps and an append-only job journal in
# `.space/wolf/` — but the only way to see a fire was to grep a connector log
# from inside the container, and the source pointed at the wrong directory
# while doing it. No new daemon, no new writer: just the files, served.
#
# Observability, and nothing more. These routes need no additional auth, but
# they remain same-origin like the rest of the lodge: arbitrary websites must
# not be able to read localhost state. What they may say is *when* a cron is
# scheduled and *whether* it fired — never what it asks the wolt to do. A
# cron's `prompt` and `notify` are the user's own words, often about private
# work, and they are deliberately not in the response.

_WOLF_FIRES_MAX = 500


@app.get("/wolf/schedules")
def wolf_schedules():
    """Every wolt's registered crons, with the last time each one fired.

    Names, timings and stamps only — see the note above on what is withheld.
    A plain `def`: this walks the filesystem, and doing that on the event loop
    blocks every other request while it runs.
    """
    schedules = []
    for wolf_json in sorted(WOLTS_DIR.glob("*/wolt/wolf.json")):
        wolt = wolf_json.parent.parent.name
        try:
            crons = json.loads(wolf_json.read_text()).get("crons", [])
        except (json.JSONDecodeError, OSError) as exc:
            schedules.append({"wolt": wolt, "error": str(exc), "crons": []})
            continue
        schedules.append({
            "wolt": wolt,
            "crons": [{
                "name": cron.get("name", ""),
                # recurring crons carry `schedule`, one-offs carry `at`
                "schedule": cron.get("schedule", ""),
                "at": cron.get("at", ""),
                "last_run": wolfcore.read_last_run(WOLTS_DIR, wolt, cron.get("name", "")),
            } for cron in crons],
        })
    return {"wolts_dir": str(WOLTS_DIR), "schedules": schedules}


@app.get("/wolf/fires")
def wolf_fires(limit: int = 50, cron: str = "", wolt: str = ""):
    """Recent cron fires, newest first, from the scheduler's job journal.

    `limit` caps the response; `cron` and `wolt` narrow it. A colony whose wolf
    has never fired has an empty list, not an error.
    """
    limit = max(0, min(limit, _WOLF_FIRES_MAX))
    fires = wolfcore.read_journal(WOLTS_DIR, limit=limit, cron=cron, wolt=wolt)
    return {"count": len(fires), "fires": fires}


# --- Wolf crons: the management API ---
# What `woltspace wolf ...` (and later the lodge page) drives. Unlike the two
# observability routes above, these serve and take a cron's `prompt` and
# `notify` — editing a cron means seeing what it says. They sit behind the same
# lodge host/origin guard as every other write here, and add nothing past it.
#
# Every write goes through `wolfcore`: strict validation, the per-wolt
# wolf.json lock the scheduler also takes, tmp+rename writes. The scheduler
# stays the only thing that fires on schedule; `/fire` is a manual run that
# leaves the schedule and the last-run stamp alone.

_WOLF_ENTRY_KEYS = ("name", "schedule", "at", "prompt", "notify", "catch_up")


def _wolf_error(status: int, message: str, field: str | None = None) -> JSONResponse:
    return JSONResponse({"error": message, "field": field}, status_code=status)


def _wolf_wolt_error(wolt, field: str = "wolt") -> JSONResponse | None:
    """400 for a malformed wolt name, 404 for one that is not a wolt here."""
    if not isinstance(wolt, str) or not wolfcore.NAME_RE.match(wolt):
        return _wolf_error(400, "wolt must be a wolt name", field)
    if not (WOLTS_DIR / wolt / "wolt" / "wolt.json").is_file():
        return _wolf_error(404, f"no wolt named '{wolt}'", field)
    return None


def _wolf_read(wolt: str):
    """(path, data, before-text) for a wolt's wolf.json, or a 409 when it is
    not valid JSON — a hand edit gone wrong is for a human to fix, not us."""
    path = wolfcore.wolf_json_path(WOLTS_DIR, wolt)
    try:
        before = path.read_text() if path.exists() else ""
        return path, wolfcore.read_wolf_json(path), before
    except (ValueError, OSError) as exc:
        return None, _wolf_error(409, f"{wolt}/wolt/wolf.json is unreadable: {exc}"), None


def _wolf_index(data: dict, name: str) -> int:
    for i, cron in enumerate(data["crons"]):
        if isinstance(cron, dict) and cron.get("name") == name:
            return i
    return -1


def _wolf_names(data: dict) -> set[str]:
    return {c.get("name") for c in data["crons"] if isinstance(c, dict)}


def _wolf_entry(payload: dict) -> dict:
    """The recognised fields, in the order wolf.json has always been written."""
    return {k: payload[k] for k in _WOLF_ENTRY_KEYS
            if payload.get(k) is not None and payload.get(k) != ""}


def _wolf_channels() -> list[str]:
    """The ping channels this lodge can actually deliver on, for a picker to
    offer. Slack needs a notify channel as well as a token: a wolf ping has no
    thread to answer in."""
    ready = {
        "telegram": dotenv_env("TELEGRAM_BOT_TOKEN") and dotenv_env("TELEGRAM_ALLOWED_USERS"),
        "slack": dotenv_env("SLACK_BOT_TOKEN") and dotenv_env("SLACK_NOTIFY_CHANNEL"),
    }
    return [c for c in wolfcore.NOTIFY_CHANNELS if ready[c]]


@app.get("/wolf/crons")
def wolf_crons():
    """Every cron in the lodge, soonest first, with its words and its times.

    Times are ISO 8601 in the lodge's local zone, which is named in `tz`: the
    wolf fires on the clock of the machine the lodge runs on.
    """
    now = wolfcore.now_local()
    crons, errors = wolfcore.load_all(WOLTS_DIR)
    out = [wolfcore.describe(c, c["_owner"], WOLTS_DIR, now) for c in crons]
    far = datetime.max.replace(tzinfo=timezone.utc)
    out.sort(key=lambda c: datetime.fromisoformat(c["next_run"]) if c["next_run"] else far)
    return {
        "tz": wolfcore.local_tz_name(),
        "channels": _wolf_channels(),
        "crons": out,
        "errors": [{"wolt": wolt, "error": error} for wolt, error in errors],
    }


@app.post("/wolf/crons")
def wolf_cron_add(payload: dict | None = Body(None), dry_run: bool = False):
    """Add a cron to a wolt. `name` defaults to a slug of the prompt.

    `?dry_run=1` answers with exactly what would be written — the entry and
    the file before and after — and writes nothing.
    """
    payload = payload or {}
    wolt = payload.get("wolt")
    if error := _wolf_wolt_error(wolt):
        return error
    entry = _wolf_entry(payload)
    now = wolfcore.now_local()
    with wolfcore.wolf_json_lock(WOLTS_DIR, wolt):
        path, data, before = _wolf_read(wolt)
        if path is None:
            return data
        taken = _wolf_names(data)
        if "name" not in entry:
            entry = {"name": wolfcore.unique_name(
                wolfcore.slugify(entry.get("prompt", "")), taken), **entry}
        try:
            wolfcore.validate_entry(entry, now)
        except wolfcore.CronError as exc:
            return _wolf_error(400, str(exc), exc.field)
        if entry["name"] in taken:
            return _wolf_error(409, f"{wolt} already has a cron named '{entry['name']}'", "name")
        data["crons"].append(entry)
        after = wolfcore.dump_wolf_json(data)
        if dry_run:
            return {"dry_run": True, "wolt": wolt, "entry": entry,
                    "before": before, "after": after}
        wolfcore.atomic_write(path, after)
    print(f"[wolf] added {wolt}/{entry['name']}")
    return JSONResponse(
        wolfcore.describe(entry, wolt, WOLTS_DIR, now), status_code=201)


def _wolf_find(wolt: str, name: str):
    """(path, data, index) of an existing cron, or (None, error response, None)."""
    if error := _wolf_wolt_error(wolt):
        return None, error, None
    path, data, _before = _wolf_read(wolt)
    if path is None:
        return None, data, None
    index = _wolf_index(data, name)
    if index < 0:
        return None, _wolf_error(404, f"{wolt} has no cron named '{name}'", "name"), None
    return path, data, index


@app.put("/wolf/crons/{wolt}/{name}")
def wolf_cron_update(wolt: str, name: str, payload: dict | None = Body(None)):
    """Change a cron in place: any of schedule | at (switching kind is fine),
    prompt, notify, catch_up — and `wolt` to move it to another wolt, its
    last-run stamp going with it. Keys this API does not know are kept.
    """
    payload = payload or {}
    if payload.get("schedule") and payload.get("at"):
        return _wolf_error(400, "give exactly one of schedule (recurring) or at (one-off)",
                           "schedule")
    target = payload.get("wolt") or wolt
    if error := _wolf_wolt_error(wolt) or _wolf_wolt_error(target):
        return error
    # Two files, two locks, always taken in the same order.
    locks = sorted({wolt, target})
    with contextlib.ExitStack() as stack:
        for held in locks:
            stack.enter_context(wolfcore.wolf_json_lock(WOLTS_DIR, held))
        path, data, index = _wolf_find(wolt, name)
        if path is None:
            return data
        entry = dict(data["crons"][index])
        if entry.get("notify"):  # an old free-text notify is written back as its channel
            entry["notify"] = wolfcore.notify_channel(entry["notify"])
        for kind, other in (("schedule", "at"), ("at", "schedule")):
            if payload.get(kind):
                entry[kind] = payload[kind]
                entry.pop(other, None)
        if "prompt" in payload:
            entry["prompt"] = payload["prompt"]
        for optional in ("notify", "catch_up"):
            if optional in payload:
                if payload[optional] is None or payload[optional] == "":
                    entry.pop(optional, None)
                else:
                    entry[optional] = payload[optional]
        try:
            wolfcore.validate_entry(entry, wolfcore.now_local())
        except wolfcore.CronError as exc:
            return _wolf_error(400, str(exc), exc.field)

        if target == wolt:
            data["crons"][index] = entry
            wolfcore.atomic_write(path, wolfcore.dump_wolf_json(data))
        else:
            target_path, target_data, _before = _wolf_read(target)
            if target_path is None:
                return target_data
            if name in _wolf_names(target_data):
                return _wolf_error(409, f"{target} already has a cron named '{name}'", "name")
            target_data["crons"].append(entry)
            wolfcore.atomic_write(target_path, wolfcore.dump_wolf_json(target_data))
            del data["crons"][index]
            wolfcore.atomic_write(path, wolfcore.dump_wolf_json(data))
            wolfcore.move_last_run(WOLTS_DIR, wolt, name, target, name)
    print(f"[wolf] updated {wolt}/{name}" + (f" → {target}" if target != wolt else ""))
    return wolfcore.describe(entry, target, WOLTS_DIR, wolfcore.now_local())


@app.delete("/wolf/crons/{wolt}/{name}")
def wolf_cron_delete(wolt: str, name: str):
    """Remove a cron, and its last-run stamp with it."""
    if error := _wolf_wolt_error(wolt):
        return error
    with wolfcore.wolf_json_lock(WOLTS_DIR, wolt):
        path, data, index = _wolf_find(wolt, name)
        if path is None:
            return data
        del data["crons"][index]
        wolfcore.atomic_write(path, wolfcore.dump_wolf_json(data))
    wolfcore.forget_last_run(WOLTS_DIR, wolt, name)
    print(f"[wolf] deleted {wolt}/{name}")
    return {"deleted": True, "wolt": wolt, "name": name}


@app.post("/wolf/crons/{wolt}/{name}/fire")
def wolf_cron_fire(wolt: str, name: str):
    """Run a cron now. Its schedule and last-run stamp are untouched; the
    journal records it as a `manual` event. Same in-process session start as
    the lodge's own spawn route, not an HTTP call back into this server."""
    path, data, index = _wolf_find(wolt, name)
    if path is None:
        return data
    entry = data["crons"][index]
    try:
        result = start_session(
            wolt=wolt,
            prompt=entry.get("prompt", ""),
            routing={"adapter": "lodge"},
        )
    except Exception as exc:
        wolfcore.append_journal(WOLTS_DIR, name, "manual", owner=wolt, error=str(exc))
        status = 403 if isinstance(exc, PermissionError) else 500
        return _wolf_error(status, str(exc))
    session, url = result.get("name"), result.get("url")
    wolfcore.append_journal(WOLTS_DIR, name, "manual", owner=wolt, session=session, link=url)
    print(f"[wolf] manual run {wolt}/{name} → {session}")
    return {"session": session, "url": url}


# --- Harnesses ---
# The agent engine a wolt runs on (claude, codex, …). Pickers/badges read
# /harnesses; the two POSTs set the lodge default and per-wolt overrides.

@app.get("/harnesses")
async def list_harnesses():
    """Registered engines (id, label, emoji, per-tier models) + lodge default."""
    return {"default": get_default_harness(), "harnesses": harness_metadata()}


@app.get("/settings/session-expiry")
async def get_session_expiry_setting():
    return {"idle_timeout_seconds": get_idle_timeout()}


@app.post("/settings/session-expiry")
async def set_session_expiry_setting(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if set(body) != {"idle_timeout_seconds"}:
        return JSONResponse({"error": "idle_timeout_seconds required"}, status_code=400)
    value = body.get("idle_timeout_seconds")
    try:
        saved = set_idle_timeout(value)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "idle_timeout_seconds": saved}


@app.get("/onboarding/status")
async def get_onboarding_status():
    """First-run UI state, independent of every harness's authentication."""
    await asyncio.to_thread(apply_default_harness_from_env)
    return onboarding_status()


@app.post("/onboarding/harness")
async def choose_onboarding_harness(request: Request):
    """Choose the lodge default and complete the first-open prompt."""
    body = await request.json()
    name = (body.get("harness") or "").strip()
    if name not in HARNESSES:
        return JSONResponse({"error": f"unknown harness: {name}"}, status_code=400)
    select_onboarding_harness(name)
    await asyncio.to_thread(install_starter_seed)
    return {"ok": True, "default": name, **onboarding_status()}


@app.post("/harness/default")
async def set_harness_default(request: Request):
    """Set the lodge-wide default engine (woltspace.json harness.default)."""
    body = await request.json()
    name = (body.get("harness") or "").strip()
    if name not in HARNESSES:
        return JSONResponse({"error": f"unknown harness: {name}"}, status_code=400)
    try:
        set_default_harness(name)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "default": name}


@app.post("/harness/tiers")
async def set_harness_tiers(request: Request):
    """Set an engine's default model per tier (woltspace.json harness.models)."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"harness", "tiers"}:
        return JSONResponse({"error": "harness and tiers required"}, status_code=400)
    harness = body.get("harness")
    if not isinstance(harness, str):
        return JSONResponse({"error": "harness must be a string"}, status_code=400)
    try:
        saved = set_tier_defaults(harness.strip(), body.get("tiers"))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "harness": harness.strip(), "models": saved}


def _wolt_settings_path(name: str) -> tuple[str, Path]:
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    if not safe or safe != name:
        raise ValueError("invalid wolt name")
    return safe, WOLTS_DIR / safe / "wolt" / "wolt.json"


def _load_wolt_settings(name: str) -> tuple[str, Path, dict]:
    safe, wolt_json = _wolt_settings_path(name)
    if not wolt_json.exists():
        raise FileNotFoundError(f"wolt not found: {safe}")
    try:
        cfg = json.loads(wolt_json.read_text())
    except (json.JSONDecodeError, OSError):
        raise RuntimeError("wolt.json unreadable")
    if not isinstance(cfg, dict):
        raise RuntimeError("wolt.json unreadable")
    return safe, wolt_json, cfg


def _wolt_settings_response(safe: str, cfg: dict) -> dict:
    harness = cfg.get("harness") or get_default_harness()
    creature = cfg.get("type") or "raccoon"
    pinned_model = cfg.get("model") or ""
    model = pinned_model if is_valid_model(harness, pinned_model) else creature_model(harness, creature)
    return {
        "wolt": safe,
        "harness": harness,
        "model": model,
        "configured": {"harness": cfg.get("harness"), "model": cfg.get("model")},
        "display_name": cfg.get("display_name") or "",
        "applies": "next session",
        "note": "Applies from the next session.",
    }


def _write_wolt_settings(wolt_json: Path, cfg: dict) -> None:
    """Callers hold settings_lock(wolt_json) from their read to this write."""
    write_json_atomic(wolt_json, cfg)


def _apply_display_name(cfg: dict, safe: str, value: object) -> None:
    """Rename what people read. The folder name (the slug) never changes here."""
    from wolts import WOLT_DISPLAY_NAME_MAX, clean_display_name

    if value is not None and not isinstance(value, str):
        raise ValueError("display_name must be a string")
    shown = clean_display_name(value or "")
    if len(shown) > WOLT_DISPLAY_NAME_MAX:
        raise ValueError(f"name must be {WOLT_DISPLAY_NAME_MAX} characters or less")
    if not shown or shown == safe:
        cfg.pop("display_name", None)
    else:
        cfg["display_name"] = shown


def _update_wolt_settings(name: str, body: dict) -> dict:
    if not body or not set(body).issubset({"harness", "model", "display_name"}):
        raise ValueError("body must contain harness, model and/or display_name only")
    _safe, path = _wolt_settings_path(name)
    with settings_lock(path):
        return _update_wolt_settings_locked(name, body)


def _update_wolt_settings_locked(name: str, body: dict) -> dict:
    safe, wolt_json, cfg = _load_wolt_settings(name)
    # Validate everything before the single write below.
    if "display_name" in body:
        _apply_display_name(cfg, safe, body["display_name"])
    if set(body) == {"display_name"}:
        _write_wolt_settings(wolt_json, cfg)
        result = _wolt_settings_response(safe, cfg)
        result.update({"ok": True, "applies": "now", "note": "Renamed."})
        return result
    requested_harness = body.get("harness", cfg.get("harness"))
    if requested_harness in (None, ""):
        chosen_harness = get_default_harness()
        pin_harness = None
    elif isinstance(requested_harness, str) and requested_harness in HARNESSES:
        chosen_harness = requested_harness
        pin_harness = requested_harness
    else:
        raise ValueError(f"unknown harness: {requested_harness}")
    creature = cfg.get("type") or "raccoon"
    harness_changed = "harness" in body and chosen_harness != (cfg.get("harness") or get_default_harness())
    tier_model = creature_model(chosen_harness, creature)
    if "model" in body:
        requested_model = body.get("model")
        model_is_tier_default = requested_model in (None, "")
    elif harness_changed:
        requested_model = tier_model
        model_is_tier_default = True
    else:
        requested_model = cfg.get("model") or tier_model
        model_is_tier_default = not cfg.get("model")
    valid = [entry["id"] for entry in model_catalog(chosen_harness)]
    if requested_model in (None, ""):
        chosen_model = tier_model
        pin_model = None
    elif (model_is_tier_default
          or (isinstance(requested_model, str)
              and is_valid_model(chosen_harness, requested_model))):
        chosen_model = requested_model
        pin_model = requested_model
    else:
        raise ValueError(
            f"model {requested_model!r} is not valid for {chosen_harness}; "
            f"valid options: {', '.join(valid)}"
        )
    if pin_harness is None:
        cfg.pop("harness", None)
    else:
        cfg["harness"] = pin_harness
    if pin_model is None:
        cfg.pop("model", None)
    else:
        cfg["model"] = pin_model
    _write_wolt_settings(wolt_json, cfg)
    result = _wolt_settings_response(safe, cfg)
    result.update({"ok": True, "harness": chosen_harness, "model": chosen_model})
    return result


# --- Creatures ---
# The lodge's custom creatures (creature_types). Every write goes through
# these routes, so the woltspace CLI and the creatures skill share one
# validated path, the same way the wolf's crons do.

def _creatures_payload() -> dict:
    from creature_types import SIZES, custom_creatures, wolts_using

    custom = custom_creatures()
    return {
        "sizes": [{"id": size, "emoji": creature_emoji(size)} for size in SIZES],
        "custom": [
            {"id": cid, **entry, "wolts": wolts_using(cid, WOLTS_DIR)}
            for cid, entry in sorted(custom.items())
        ],
    }


@app.get("/creatures")
def list_creatures():
    return _creatures_payload()


@app.post("/creatures")
async def add_creature(request: Request):
    from creature_types import add_custom

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    entry = dict(body)
    cid = entry.pop("id", None)
    try:
        clean = await asyncio.to_thread(add_custom, cid, entry)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "id": cid, **clean}


@app.delete("/creatures/{cid}")
async def remove_creature(cid: str):
    from creature_types import remove_custom

    try:
        await asyncio.to_thread(remove_custom, cid)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "id": cid}


def _assign_creature(name: str, new_type: object) -> dict:
    """Change only a wolt's creature type. Its own harness, model and role pins
    are kept and reported, and the change applies from its next session: a
    running session keeps the model it was spawned with (see resume_session)."""
    from creature_types import SIZES, is_custom
    from wolts import SINGLETON_TYPES

    _safe, path = _wolt_settings_path(name)
    # Lodge lock first: removing a creature checks no wolt is one under it.
    with settings_lock(WOLTS_DIR / "woltspace.json"), settings_lock(path):
        safe, wolt_json, cfg = _load_wolt_settings(name)
        old_type = cfg.get("type") or "rodent"
        if not isinstance(new_type, str) or not new_type:
            raise ValueError("type is required")
        if old_type in SINGLETON_TYPES:
            raise ValueError(f"{safe} is the lodge's {old_type}; its creature does not change here")
        if new_type not in SIZES and not is_custom(new_type):
            raise ValueError("type must be otter, beaver, raccoon, or a custom creature")
        cfg["type"] = new_type
        _write_wolt_settings(wolt_json, cfg)
        # Under the same locks: two assignments must leave the site with the
        # emoji of the one that wrote wolt.json last.
        site_emoji = _follow_site_emoji(safe, old_type, new_type)
    kept = {k: cfg[k] for k in ("harness", "model", "role") if cfg.get(k)}
    result = _wolt_settings_response(safe, cfg)
    result.update({
        "ok": True, "type": new_type, "previous_type": old_type,
        "emoji": creature_emoji(new_type), "kept": kept, "site_emoji_updated": site_emoji,
    })
    return result


def _follow_site_emoji(wolt: str, old_type: str, new_type: str) -> bool:
    """A starter site.json carries a copy of the creature's emoji. When it is
    still that copy, it follows the new creature; a wolt's own choice stays."""
    site_json = WOLTS_DIR / wolt / "wolt" / "site" / "site.json"
    try:
        site = json.loads(site_json.read_text())
        tokens = site.get("tokens")
        if not isinstance(tokens, dict) or tokens.get("emoji") != creature_emoji(old_type):
            return False
        tokens["emoji"] = creature_emoji(new_type)
        write_text_atomic(site_json, json.dumps(site, ensure_ascii=False) + "\n")
        return True
    except (OSError, json.JSONDecodeError, AttributeError):
        return False


@app.post("/wolts/{name}/creature")
async def assign_creature(name: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"type"}:
        return JSONResponse({"error": "body must be {\"type\": ...}"}, status_code=400)
    try:
        return await asyncio.to_thread(_assign_creature, name, body["type"])
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return _wolt_settings_error(exc)


def _wolt_settings_error(exc: Exception) -> JSONResponse:
    if isinstance(exc, FileNotFoundError):
        return JSONResponse({"error": str(exc)}, status_code=404)
    if isinstance(exc, ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/wolts/{name}/settings")
async def get_wolt_settings(name: str):
    try:
        safe, _path, cfg = _load_wolt_settings(name)
        return _wolt_settings_response(safe, cfg)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return _wolt_settings_error(exc)


@app.patch("/wolts/{name}/settings")
async def patch_wolt_settings(name: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    try:
        return _update_wolt_settings(name, body)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return _wolt_settings_error(exc)


@app.post("/wolts/{name}/harness")
async def set_wolt_harness(name: str, request: Request):
    """Compatibility alias for the atomic settings writer."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"harness"}:
        return JSONResponse({"error": "harness is required"}, status_code=400)
    try:
        result = _update_wolt_settings(name, body)
        result["pinned"] = body["harness"] not in (None, "")
        return result
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        return _wolt_settings_error(exc)


# --- Archive, unarchive and delete a wolt ---
# The file work is wolt_archive's. These routes only call it, off the event
# loop: deleting a big folder takes a while.

async def _archive_call(action, name: str):
    try:
        return await asyncio.to_thread(action, name, wolts_dir=WOLTS_DIR)
    except wolt_archive.ArchiveError as exc:
        return JSONResponse({"error": exc.message, **exc.extra}, status_code=exc.status)


@app.get("/wolts/archived")
async def archived_wolts():
    return await asyncio.to_thread(wolt_archive.list_archived_wolts, wolts_dir=WOLTS_DIR)


@app.post("/wolts/{name}/archive")
async def archive_wolt(name: str):
    return await _archive_call(wolt_archive.archive_wolt, name)


@app.post("/wolts/archived/{name}/unarchive")
async def unarchive_wolt(name: str):
    return await _archive_call(wolt_archive.unarchive_wolt, name)


@app.delete("/wolts/archived/{name}")
async def delete_archived_wolt(name: str):
    return await _archive_call(wolt_archive.delete_archived_wolt, name)


# --- Runtime capabilities and repository-scoped Auto consent ---

@app.get("/runtime/capabilities")
async def runtime_capabilities():
    context = RuntimeContext.from_env()
    return {
        "isolation": context.isolation,
        "supports_host_workdirs": context.isolation == "host",
        "default_execution_policy": (
            "prompt" if context.isolation == "host" else "auto"
        ),
        "policy_version": POLICY_VERSION,
    }


def _auto_target(body: dict) -> SessionTarget:
    return SessionTarget.resolve(
        str(body.get("wolt_id") or body.get("wolt") or ""),
        body.get("workdir"),
        wolts_dir=WOLTS_DIR,
    )


@app.post("/auto-grants/check")
async def auto_grant_check(request: Request):
    body = await request.json()
    try:
        target = _auto_target(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    grant = AutoGrantStore(WOLTS_DIR).find(target)
    return {
        "approved": grant is not None,
        "target": target.to_record(),
        "grant": grant.to_record() if grant else None,
    }


@app.post("/auto-grants/grant")
async def auto_grant_create(request: Request):
    body = await request.json()
    try:
        target = _auto_target(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    canonical = str(target.canonical_workdir)
    if body.get("confirm") != canonical:
        return JSONResponse(
            {
                "error": "Auto consent must confirm the exact canonical directory",
                "canonical_workdir": canonical,
            },
            status_code=400,
        )
    grant = AutoGrantStore(WOLTS_DIR).grant(target)
    return {"ok": True, "grant": grant.to_record()}


@app.post("/auto-grants/revoke")
async def auto_grant_revoke(request: Request):
    body = await request.json()
    try:
        target = _auto_target(body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    revoked = AutoGrantStore(WOLTS_DIR).revoke(target)
    return {"ok": True, "revoked": revoked, "target": target.to_record()}


# --- Apps ---
# Centralized app management. Uses woltspace.json manifests.
# App names are globally unique. Keeper (owning wolt) is in woltspace.json.

_APP_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


def _invalid_app_name(name: str) -> JSONResponse | None:
    if not _APP_NAME_RE.fullmatch(name):
        return JSONResponse({"error": "invalid app name"}, status_code=400)
    return None

@app.get("/tunnel")
async def tunnel_status():
    """Describe the lodge tunnel without exposing credentials or process state."""
    url = tunnel_mgr.get_tunnel_url()
    if not url:
        return {"mode": "off", "url": ""}
    return {"mode": tunnel_mgr.get_tunnel_mode(), "url": url}

@app.get("/apps")
async def list_apps_api():
    """List all apps that have woltspace.json."""
    apps = discover_apps()
    running = {r["name"]: r for r in running_apps()}
    result = []
    for a in apps:
        entry = a.model_dump()
        entry["configured_port"] = a.port
        run_state = running.get(a.name)
        entry["running"] = run_state is not None
        entry["port"] = run_state["port"] if run_state else None
        entry["url"] = f"/app/{a.name}/"
        entry["shared"] = _share_summary(a.name)
        result.append(entry)
    return result


@app.get("/apps/{name}")
async def app_detail(name: str):
    """Get a single app's manifest and running state."""
    if invalid := _invalid_app_name(name):
        return invalid
    app_obj = get_app(name)
    if not app_obj:
        return JSONResponse({"error": f"app {name} not found"}, status_code=404)
    running = {r["name"]: r for r in running_apps()}
    entry = app_obj.model_dump()
    entry["configured_port"] = app_obj.port
    run_state = running.get(name)
    entry["running"] = run_state is not None
    entry["port"] = run_state["port"] if run_state else None
    entry["url"] = f"/app/{name}/"
    entry["shared"] = _share_summary(name)
    return entry


def _share_summary(name: str) -> dict | None:
    """What a shared app's listing shows: its public addresses, since when, by whom."""
    record = app_sharing.shared(WOLTS_DIR, name)
    if record is None:
        return None
    return {k: record.get(k) for k in ("tunnel_url", "domain_url", "since", "by", "said_by")}


def _share_by(request: Request) -> str:
    if not lodge_auth.enabled():
        return "local"
    return _lodge_auth_scope(request.headers) or "unknown"


async def _said_by(request: Request) -> str:
    try:
        body = await request.json()
    except Exception:
        return ""
    value = body.get("said_by", "") if isinstance(body, dict) else ""
    return str(value)[:64] if isinstance(value, str) else ""


@app.post("/apps/unshare-all")
async def apps_unshare_all(request: Request):
    """Make every shared app private again."""
    names = await asyncio.to_thread(app_sharing.unshare_all, WOLTS_DIR, PORT,
                                    by=_share_by(request), said_by=await _said_by(request))
    return {"ok": True, "unshared": names}


@app.post("/apps/{name}/share")
async def app_share(name: str, request: Request):
    """Let anyone with the link use this app. An explicit choice, logged."""
    if invalid := _invalid_app_name(name):
        return invalid
    if get_app(name) is None:
        return JSONResponse({"error": f"app {name} not found"}, status_code=404)
    domain = tunnel_mgr.get_tunnel_domain().lower().rstrip(".")
    try:
        record = await asyncio.to_thread(
            app_sharing.share, WOLTS_DIR, name, PORT, by=_share_by(request),
            said_by=await _said_by(request),
            domain_url=f"https://{name}.{domain}" if domain else None)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    print(f"[sharing] {name} shared: {record.get('tunnel_url') or record.get('domain_url')}")
    return {"ok": True, "name": name, "warning": app_sharing.WARNING, **_share_summary(name)}


@app.post("/apps/{name}/unshare")
async def app_unshare(name: str, request: Request):
    """Make a shared app private again."""
    if invalid := _invalid_app_name(name):
        return invalid
    was = await asyncio.to_thread(app_sharing.unshare, WOLTS_DIR, name, PORT,
                                  by=_share_by(request), said_by=await _said_by(request))
    if not was:
        return JSONResponse({"error": f"{name} is not shared"}, status_code=404)
    print(f"[sharing] {name} unshared")
    return {"ok": True, "name": name}


@app.post("/apps/{name}/start")
async def app_start(name: str):
    """Start an app's dev server."""
    if invalid := _invalid_app_name(name):
        return invalid
    try:
        state = await asyncio.to_thread(start_app, name)
        print(f"[apps] started {name} on port {state['port']}")
        return state
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.post("/apps/{name}/stop")
async def app_stop(name: str):
    """Stop a running app."""
    if invalid := _invalid_app_name(name):
        return invalid
    was_running = await asyncio.to_thread(stop_app, name)
    if was_running:
        print(f"[apps] stopped {name}")
        return {"ok": True, "name": name}
    return JSONResponse({"error": f"{name} is not running"}, status_code=404)


@app.post("/apps/{name}/restart")
async def app_restart(name: str):
    if invalid := _invalid_app_name(name):
        return invalid
    try:
        return await asyncio.to_thread(restart_app, name)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.get("/apps/{name}/logs")
async def app_logs(name: str, tail: int = 200, stream: bool = False):
    if invalid := _invalid_app_name(name):
        return invalid
    if not get_app(name):
        return JSONResponse({"error": f"app {name} not found"}, status_code=404)
    tail = max(1, min(tail, 2000))
    path = app_log_file(name)

    def read_tail() -> list[str]:
        try:
            with path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                handle.seek(max(0, size - 65536))
                chunk = handle.read(65536)
            return chunk.decode("utf-8", errors="replace").splitlines()[-tail:]
        except FileNotFoundError:
            return []

    if not stream:
        return {"name": name, "lines": read_tail()}

    async def events():
        position = path.stat().st_size if path.exists() else 0
        for line in read_tail():
            yield f"data: {json.dumps(line)}\n\n"
        while True:
            await asyncio.sleep(1)
            if not path.exists():
                continue
            size = path.stat().st_size
            if size < position:
                position = 0
            if size == position:
                yield ": keepalive\n\n"
                continue
            with path.open("r", errors="replace") as handle:
                handle.seek(position)
                chunk = handle.read()
                position = handle.tell()
            for line in chunk.splitlines():
                yield f"data: {json.dumps(line)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.put("/apps/{name}")
async def app_update(name: str, request: Request):
    if invalid := _invalid_app_name(name):
        return invalid
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if set(body) != {"keeper"}:
        return JSONResponse({"error": "keeper is the only editable field"}, status_code=400)
    keeper = body.get("keeper")
    known = {w["dir"] for w in _configured_wolts()}
    if not isinstance(keeper, str) or keeper not in known:
        return JSONResponse({"error": "keeper must name an existing wolt"}, status_code=400)
    try:
        return set_app_keeper(name, keeper).model_dump()
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)


@app.post("/sessions/{name}/resume")
async def session_resume(name: str, request: Request):
    """Resume a stopped/orphaned session by name."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    body = await request.json()
    prompt = body.get("prompt", "")
    try:
        # resume_session polls the process table for up to 12s waiting for the
        # relaunched agent — a synchronous wait. Called inline it would freeze
        # every other request, the /tui socket and viewport livereload for the
        # whole resume, so it runs in a thread (same shape as app_share).
        result = await asyncio.to_thread(resume_session, safe, prompt)
        print(f"[sessions/resume] {safe} → {result.get('status')}")
        return result
    except ResumeUnavailable as e:
        # Nothing to replay. Retrying will never help, so say so rather than
        # letting the caller loop on a wake that cannot happen.
        print(f"[sessions/resume] {safe} → unavailable: {e}")
        return JSONResponse({"error": str(e)}, status_code=409)
    except ResumeFailed as e:
        # We relaunched and no agent came up. The message names what to look
        # at; the TUI prints it verbatim instead of "won't stir".
        print(f"[sessions/resume] {safe} → failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/sessions/{name}/stop")
async def session_stop(name: str):
    """Stop a running session — kill tmux, mark as stopped."""
    safe = "".join(c for c in name if c.isalnum() or c in "-_")
    try:
        result = stop_session(safe)
        print(f"[sessions/stop] {safe} → stopped (was_alive={result.get('was_alive')})")
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Wolt Sites ---
# Each wolt has a persistent site at wolt/site/. Served via livereload.
# Sites auto-start when a session begins outside an app context.

@app.get("/sites")
async def list_sites_api():
    """List all wolt sites (any wolt with a site dir)."""
    found = []
    for wolt_dir in sorted(WOLTS_DIR.iterdir()):
        if not wolt_dir.is_dir() or wolt_dir.name.startswith("."):
            continue
        if (wolt_dir / "wolt" / "site").is_dir():
            found.append({"wolt": wolt_dir.name, "url": _site_link(wolt_dir.name)})
    return found


def _open_site_page(wolt_name: str, path: str) -> str:
    """The lodge's own small page where a site's frame would be."""
    from html import escape
    link = escape(_site_link(wolt_name, path), quote=True)
    name = escape(wolt_name)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<style>body{margin:0;font:16px/1.5 system-ui,sans-serif;display:grid;place-items:center;"
        "min-height:100vh;background:#EDE8DE;color:#2a2622}a{color:#3A6644}</style></head><body>"
        f"<p>{name}'s site opens on its own address. "
        f"<a href=\"{link}\" target=\"_blank\" rel=\"noopener\">Open it</a></p></body></html>"
    )


def _site_link(wolt_name: str, path: str = "/") -> str:
    """Where a wolt's site opens: on its own origin, through the Mill.

    The lodge never serves a wolt's pages on its own origin, where their
    script would run as the owner.
    """
    return f"/sites/open?wolt={wolt_name.lower()}&path={quote(_safe_next(path), safe='/')}"


@app.get("/sites/{wolt_name}")
async def site_detail(wolt_name: str):
    """Get a wolt's site state."""
    sdir = site_dir(wolt_name)
    return {
        "wolt": wolt_name,
        "url": _site_link(wolt_name),
        "dir_exists": sdir.exists(),
    }


@app.api_route("/wolt/{wolt_name}/site/{path:path}", methods=["GET", "HEAD"])
@app.api_route("/wolt/{wolt_name}/site", methods=["GET", "HEAD"])
async def serve_wolt_site(wolt_name: str, request: Request, path: str = ""):
    """An old site link: the site lives on its own origin now, through the Mill."""
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", wolt_name):
        return JSONResponse({"error": "invalid wolt name"}, status_code=400)
    if lodge_auth.enabled() and request.headers.get("sec-fetch-dest", "") in ("iframe", "frame"):
        # A frame can't keep the site's cookie from a cross-site navigation,
        # so redirecting would only spend a grant on a page that then can't
        # open. The lodge's own frame offers a link.
        return HTMLResponse(_open_site_page(wolt_name, "/" + path))
    return RedirectResponse(_site_link(wolt_name, "/" + path), status_code=303)


_WOLT_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]*")
_BUILTIN_TABS = ("about", "memory", "settings")


def _builtin_wolt_dir(wolt_name: str) -> Path | None:
    if not _WOLT_NAME_RE.fullmatch(wolt_name):
        return None
    wolt_dir = WOLTS_DIR / wolt_name
    return wolt_dir if (wolt_dir / "wolt").is_dir() else None


@app.get("/wolt/{wolt_name}/_/memory.json")
async def wolt_builtin_memory(wolt_name: str):
    """The boot files for the built-in Memory page, windowed like a session boot."""
    wolt_dir = _builtin_wolt_dir(wolt_name)
    if wolt_dir is None:
        return JSONResponse({"error": "no such wolt"}, status_code=404)
    return JSONResponse(memory_payload(wolt_dir), headers={"Cache-Control": "no-store"})


@app.get("/wolt/{wolt_name}/_/manifest.json")
async def wolt_builtin_manifest(wolt_name: str):
    """What the shell draws: public wolt fields, site.json look, the page tree."""
    wolt_dir = _builtin_wolt_dir(wolt_name)
    if wolt_dir is None:
        return JSONResponse({"error": "no such wolt"}, status_code=404)
    return JSONResponse(shell_manifest(wolt_name, wolt_dir, site_dir(wolt_name)),
                        headers={"Cache-Control": "no-store"})


@app.get("/wolt/{wolt_name}/_/")
@app.get("/wolt/{wolt_name}/_/{tab}")
async def wolt_builtin_page(wolt_name: str, tab: str = "about"):
    """About / Memory / Settings: the same page for every wolt, drawn by the lodge."""
    wolt_dir = _builtin_wolt_dir(wolt_name)
    if wolt_dir is None:
        return PlainTextResponse("Not found", status_code=404)
    if tab not in _BUILTIN_TABS:
        return PlainTextResponse("Not found", status_code=404)
    sdir = site_dir(wolt_name)
    site_cfg = load_site_config(sdir)
    manifest = shell_manifest(wolt_name, wolt_dir, sdir, site_cfg)
    page = (STATIC_DIR / "wolt-shell" / "wolt.html").read_text(encoding="utf-8")
    # The built-in page always needs the manifest; the drawer only when the
    # wolt has not switched the shell off.
    page = page.replace("<!--WOLT_SHELL-->", shell_tags(manifest, drawer=shell_wanted(site_cfg, "")))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.websocket("/wolt/{wolt_name}/site/livereload")
async def site_livereload_ws(wolt_name: str, ws: WebSocket):
    """Watch a wolt's site dir for changes and push reload via WebSocket."""
    if not _lodge_websocket_request_allowed(ws):
        await ws.close(code=1008)
        return
    from watchfiles import awatch

    sdir = WOLTS_DIR / wolt_name / "wolt" / "site"
    if not sdir.exists():
        await ws.close()
        return
    await ws.accept()
    stop = threading.Event()
    _livereload_stops.add(stop)

    async def _push_reloads():
        async for _changes in awatch(str(sdir), stop_event=stop,
                                     rust_timeout=_WATCHER_POLL_MS):
            await ws.send_text("reload")

    async def _await_disconnect():
        # Only a disconnect ends this. A client that sends anything at all — a
        # heartbeat, a stray text frame — used to look like one, killing the
        # watcher and leaving the socket open with nothing behind it.
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return

    # Race the watcher against the socket's own receive. That second task is
    # the whole point: uvicorn's shutdown order is signal → close connections →
    # *then* lifespan shutdown, so a handler that only watches files learns
    # about the shutdown after the graceful window it was supposed to fit
    # inside, and gets reported as `Cancel 1 running task(s)`. The disconnect
    # arrives as soon as uvicorn begins shutting down — and equally when the
    # browser simply navigates away.
    watcher = asyncio.create_task(_push_reloads())
    disconnect = asyncio.create_task(_await_disconnect())
    try:
        await asyncio.wait({watcher, disconnect},
                           return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        pass
    finally:
        # Raising the flag is enough: awatch checks it within `rust_timeout`
        # and its generator finishes on its own. Give it that moment before
        # resorting to cancellation — tearing an async generator out of a
        # to_thread call mid-flight is what turns a tidy close into a
        # CancelledError escaping into whoever opened the socket.
        stop.set()
        _livereload_stops.discard(stop)
        disconnect.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(watcher), _WATCHER_SETTLE_S)
        watcher.cancel()
        with contextlib.suppress(BaseException):
            await asyncio.gather(watcher, disconnect, return_exceptions=True)


@app.get("/app/{app_name}/{path:path}")
@app.get("/app/{app_name}")
async def serve_app(app_name: str, request: Request, path: str = ""):
    """An old app link: apps live on their own origin, through the Mill."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", app_name):
        return JSONResponse({"error": "invalid app name"}, status_code=400)
    target = quote(_safe_next("/" + path), safe="/")
    return RedirectResponse(f"/apps/open?app={app_name.lower()}&path={target}", status_code=303)


@app.get("/tools")
async def list_tools():
    return tool_registry.list_all()


@app.post("/tools/spawn")
async def spawn_tool(request: Request):
    body = await request.json()
    name = body.get("name")
    command = body.get("command")
    port = body.get("port")
    if not name or not command or not port:
        return JSONResponse({"error": "name, command, and port required"}, status_code=400)
    try:
        result = tool_registry.spawn(name, command, port)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


@app.api_route("/tools/{tool_name}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_tool(tool_name: str, path: str, request: Request):
    tool = tool_registry.get(tool_name)
    if not tool:
        return PlainTextResponse("tool not found", status_code=404)
    target = f"http://127.0.0.1:{tool['port']}/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    # A tool is a backend like an app: the lodge's and Access's credentials
    # stay behind, both ways.
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                request.method, target,
                headers=upstream_request_headers(request.headers.items(), tool["port"],
                                                 is_lodge_key=_is_lodge_key),
                content=await request.body(),
            )
            # The body comes back decoded, so its encoding and length go too.
            reply = Response(resp.content, status_code=resp.status_code)
            reply.raw_headers = [
                (name.encode("latin-1"), value.encode("latin-1"))
                for name, value in client_response_headers(resp.headers.multi_items())
                if name.lower() not in {"content-encoding", "content-length"}
            ] + [(b"content-length", str(len(resp.content)).encode())]
            # A tool's page is a wolt's page too: opaque on the lodge's origin,
            # so its script can't act as the owner.
            reply.raw_headers = [(k, v) for k, v in reply.raw_headers
                                 if k.lower() != b"content-security-policy"]
            reply.raw_headers.append((b"content-security-policy", OPAQUE_PAGE_CSP.encode()))
            return reply
        except httpx.ConnectError:
            return PlainTextResponse("tool unavailable", status_code=502)


# --- WebSocket: live reload ---

@app.websocket("/livereload")
async def livereload_ws(ws: WebSocket):
    if not _lodge_websocket_request_allowed(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    _livereload_clients.add(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _livereload_clients.discard(ws)


# --- WebSocket: browser terminal ---

@app.websocket("/tui")
async def tui_proxy(ws: WebSocket):
    """Attach the browser terminal directly to the requested tmux session."""
    if not _lodge_websocket_request_allowed(ws):
        await ws.close(code=1008)
        return
    session = ws.query_params.get("session", "main")
    await ws.accept()
    try:
        attachment = await attach_tmux(session, WOLTS_DIR)
    except PtyBridgeError as error:
        try:
            await ws.send_text(f"\r\n[tui] connection failed: {error}\r\n")
            await ws.close()
        except Exception:
            pass
        return

    async def client_to_pty():
        while True:
            data = await ws.receive_text()
            if data.startswith("{"):
                try:
                    message = json.loads(data)
                    if message.get("type") == "resize":
                        attachment.resize(int(message["cols"]), int(message["rows"]))
                        continue
                except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                    pass
            await attachment.write(data)

    async def pty_to_client():
        decoder = PtyTextDecoder()
        while True:
            data = await attachment.read()
            if not data:
                tail = decoder.feed(b"", final=True)
                if tail:
                    await ws.send_text(tail)
                return
            text = decoder.feed(data)
            if text:
                await ws.send_text(text)

    tasks = {
        asyncio.create_task(client_to_pty()),
        asyncio.create_task(pty_to_client()),
    }
    try:
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await attachment.close()


# --- Pages (HTML) ---

@app.get("/tui")
async def tui_page(request: Request):
    # Rendering is read-only. The page wakes a resting session through the
    # existing same-origin-guarded POST /sessions/{name}/resume route.
    return templates.TemplateResponse(request, "tui.html", context={
        "cache_bust": int(time.time()),
    })


@app.get("/terminal")
async def terminal_page(request: Request):
    return templates.TemplateResponse(request, "terminal.html", context={
        "cache_bust": int(time.time()),
    })


@app.get("/settings")
async def settings_page(request: Request):
    """Shared configuration surface for the lodge and desktop shell."""
    harnesses = harness_metadata()
    return templates.TemplateResponse(request, "settings.html", context={
        "active_nav": "settings",
        "cache_bust": int(time.time()),
        "harness_default": get_default_harness(),
        "harnesses": harnesses,
        "harness_labels": {harness["id"]: harness["label"] for harness in harnesses},
        "idle_timeout": get_idle_timeout(),
    })


@app.get("/w/{wolt_name}")
async def lodge_wolt_page(request: Request, wolt_name: str):
    """The lodge-native page for one persistent collaborator."""
    wolt_dir = _builtin_wolt_dir(wolt_name)
    if wolt_dir is None:
        return PlainTextResponse("Not found", status_code=404)
    config = next((w for w in _configured_wolts() if w.get("dir") == wolt_name), {})
    return templates.TemplateResponse(request, "wolt.html", context={
        "active_nav": "",
        "cache_bust": int(time.time()),
        "wolt_name": wolt_name,
        "wolt": config,
    })


@app.get("/connectors")
async def connectors_page(request: Request):
    """Show lodge address and messaging connector health."""
    return templates.TemplateResponse(request, "connectors.html", context={
        "active_nav": "connectors",
        "cache_bust": int(time.time()),
    })


@app.get("/wolves")
async def wolves_page(request: Request):
    """The wolves: every scheduled wake-up in the lodge, editable in place.

    The page renders the wolt picker from disk and reads everything else from
    the /wolf/crons API, the same one `woltspace wolf` drives.
    """
    wolts = [
        {"name": wolt["dir"], "type": wolt.get("type", "rodent"), "emoji": wolt["emoji"]}
        for wolt in _configured_wolts()
    ]
    return templates.TemplateResponse(request, "wolves.html", context={
        "active_nav": "wolves",
        "cache_bust": int(time.time()),
        "wolts": wolts,
    })


@app.get("/a/{app_name}")
async def app_page(app_name: str, request: Request):
    if not get_app(app_name):
        return PlainTextResponse("App not found", status_code=404)
    return templates.TemplateResponse(request, "app.html", context={
        "active_nav": "apps",
        "app_name": app_name,
        "cache_bust": int(time.time()),
    })


# jerpint: this one will be important to nail we might review onboarding flow
@app.get("/onboard")
async def onboard_page():
    """Compatibility route: first-run setup now lives in the lodge itself."""
    return RedirectResponse("/", status_code=307)


@app.get("/placeholder.html")
async def placeholder_page():
    """Idle viewport — shown when no session is active.

    Dedicated route so wolt site dirs can't shadow the platform file.
    """
    resp = await _serve_platform_file("placeholder.html")
    return resp or PlainTextResponse("placeholder.html not found", status_code=500)


# --- Catch-all: static files ---

@app.get("/{path:path}")
async def catch_all(path: str, request: Request):
    # Root → home template (Jinja2)
    if path == "" or path == "/":
        return templates.TemplateResponse(request, "home.html", context={
            "active_nav": "home",
            "cache_bust": int(time.time()),
        })

    # The platform's public files. A wolt's own pages never run on the
    # lodge's origin: they're on their site's, through the Mill.
    resp = await _serve_platform_file(path)
    if resp:
        return resp
    return PlainTextResponse("Not found", status_code=404)


_start_time = time.time()
