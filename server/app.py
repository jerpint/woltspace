"""Woltspace server — FastAPI replacement for server.js."""

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import contextlib
import unicodedata
from collections import deque
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import Body, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
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
from .access import (
    access_settings_dict,
    access_token_verifier,
    load_access_settings,
    save_access_settings,
)
from .notify import NoNotificationTarget, send_notification
from .app_sharing import read_app_shares, write_app_shares
from .sparks import get_spark_with_chain, list_sparks
from .pty_bridge import (
    PtyBridgeError,
    PtyTextDecoder,
    attach_tmux,
)

# Session spawning — shared with bot
import sys as _sys
_sys.path.insert(0, str(WOLTSPACE_DIR / "container" / "lib"))
from sessions import (
    resume_session, start_session, stop_session,
    deliver_message, resolve_active_session, format_attributed_message,
    format_spawned_prompt,
    wolt_harness, ResumeUnavailable, ResumeFailed,
    stored_resume_id, recover_resume_id, taken_resume_ids, claim_resume_id,
)
from session_expiry import get_idle_timeout, get_pane_activity, set_idle_timeout
from session_runtime import RuntimeHandle, get_runtime
from session_targets import SessionTarget
from execution_policy import AutoGrantStore, POLICY_VERSION
from runtime_context import RuntimeContext
from harness_auth import auth_source, claude_authenticated
from skills_sync import wolt_skills_delivery
from harnesses import (
    harness_metadata,
    get_default_harness,
    set_default_harness,
    resolve_harness,
    platform_skill_invoke,
    is_valid_model,
    model_catalog,
    tier_default_model,
    HARNESSES,
)
from apps import (
    WoltspaceApp,
    apps_restore,
    discover_apps,
    get_app,
    app_dir,
    app_log_file,
    running_apps,
    share_app,
    start_app,
    stop_app,
    restart_app,
    set_app_keeper,
    unshare_all_apps,
    unshare_app,
)
from sites import ensure_site, site_dir
import wolfcore
from site_shell import inject_shell, load_site_config, memory_payload, shell_manifest, shell_tags, shell_wanted
from .state import (
    bot_log,
    get_current_meta,
    get_current_url,
    log_view,
    read_views_history,
    sanitize_session,
    set_current_url,
    onboarding_status,
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
    tool_registry.restore()
    apps_restore()
    _start_file_watcher()
    _start_tool_gc()
    tunnel_mgr.start_tunnel()
    print(f"""
  woltspace server (python) · http://localhost:{PORT}
  wolt: {WOLT_NAME}
  browser terminal: embedded Python PTY bridge
  tunnel: {tunnel_mgr.get_tunnel_url() or 'disabled'}
    """)
    yield
    # Watchers first: they are what used to outlive the shutdown window.
    stop_file_watchers()
    tunnel_mgr.stop_tunnel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    from woltspace import __version__ as woltspace_version
    from woltspace.adoption import read_adoption_report
    from woltspace.channel_supervisor import read_connector_report
    from woltspace.layout import RuntimeLayout

    layout = RuntimeLayout.from_env()
    return {
        "ok": True,
        # The lodge's own version. The tui reads it to check the minimum
        # woltspace it needs; a lodge too old to report it is treated as 0.5.0.
        "version": woltspace_version,
        "instance_id": os.environ.get("WOLTSPACE_INSTANCE_ID", ""),
        "pid": os.getpid(),
        "wolts_dir": str(WOLTS_DIR),
        "isolation": os.environ.get("WOLTSPACE_ISOLATION", "external"),
        "adoption": read_adoption_report(layout),
        "connectors": read_connector_report(layout).get("connectors", []),
    }

# --- Templates & Static ---

TEMPLATES_DIR = WOLTSPACE_DIR / "templates"
STATIC_DIR = PUBLIC_DIR / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Mount static files (CSS/JS shared across pages)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


_APP_HOST_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _lodge_config_path() -> Path:
    return WOLTS_DIR / "woltspace.json"


def _load_lodge_config() -> dict:
    path = _lodge_config_path()
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError):
        raise RuntimeError("woltspace.json unreadable")
    if not isinstance(value, dict):
        raise RuntimeError("woltspace.json unreadable")
    return value


def _normalize_apps_domain(value: object) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ValueError("apps_domain must be a domain name or null")
    domain = value.strip().lower().rstrip(".")
    if (not domain or len(domain) > 253 or "." not in domain
            or any(not _DNS_LABEL_RE.fullmatch(label) for label in domain.split("."))):
        raise ValueError("apps_domain must be a valid domain name without a scheme, path, or port")
    return domain


def get_apps_domain() -> str | None:
    try:
        return _normalize_apps_domain(_load_lodge_config().get("apps_domain"))
    except (RuntimeError, ValueError):
        return None


def set_apps_domain(value: object) -> str | None:
    domain = _normalize_apps_domain(value)
    if domain is not None:
        tunnel_hostname = tunnel_mgr.get_tunnel_hostname().lower().rstrip(".")
        if (domain in {"localhost", "127.0.0.1", "::1"}
                or tunnel_hostname == domain
                or (tunnel_hostname and tunnel_hostname.endswith(f".{domain}"))):
            raise ValueError("apps_domain must not equal or contain the lodge hostname")
    cfg = _load_lodge_config()
    if domain is None:
        cfg.pop("apps_domain", None)
    else:
        cfg["apps_domain"] = domain
    path = _lodge_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    wolfcore.atomic_write(path, json.dumps(cfg, indent=2) + "\n")
    return domain


def get_app_gateway_port() -> int:
    default_port = PORT - 660
    env_port = os.environ.get("WOLTSPACE_APP_GATEWAY_PORT", "").strip()
    if not 1024 <= default_port <= 65535:
        raise RuntimeError("derived app gateway port must be from 1024 to 65535")
    try:
        gateway = _load_lodge_config().get("app_gateway", {})
    except RuntimeError:
        return default_port
    configured = gateway.get("port", default_port) if isinstance(gateway, dict) else default_port
    try:
        port = int(env_port) if env_port else configured
    except ValueError:
        return default_port
    return port if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535 else default_port


def set_app_gateway_port(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
        raise ValueError("port must be an integer from 1024 to 65535")
    if value == PORT:
        raise ValueError("app gateway port must differ from the lodge port")
    from .gateway_settings import _BLOCKED_PORTS
    if value in _BLOCKED_PORTS:
        raise ValueError(f"port {value} is blocked by web browsers; choose a safe port")
    cfg = _load_lodge_config()
    cfg["app_gateway"] = {"port": value}
    path = _lodge_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    wolfcore.atomic_write(path, json.dumps(cfg, indent=2) + "\n")
    return value


def _split_host(host_header: str) -> tuple[str, int | None]:
    """Return a normalized hostname and optional port from an HTTP Host value."""
    try:
        parsed = urlsplit(f"//{host_header.strip()}")
        return (parsed.hostname or "").lower().rstrip("."), parsed.port
    except ValueError:
        return "", None


def _allowed_http_hostname(hostname: str) -> bool:
    """Allow only the lodge's loopback/tunnel names and valid app subdomains."""
    if hostname in {"localhost", "127.0.0.1", "::1"}:
        return True
    if hostname.endswith(".localhost"):
        return bool(_APP_HOST_RE.fullmatch(hostname.removesuffix(".localhost")))

    tunnel_hostname = tunnel_mgr.get_tunnel_hostname().lower().rstrip(".")
    if tunnel_hostname and hostname == tunnel_hostname:
        return True
    # Quick tunnels have no wildcard app domain, so tunnel.py intentionally
    # does not derive one from configuration. Their exact runtime URL is still
    # a valid lodge host.
    try:
        current_tunnel_hostname = (
            urlsplit(tunnel_mgr.get_tunnel_url()).hostname or ""
        ).lower().rstrip(".")
    except ValueError:
        current_tunnel_hostname = ""
    if current_tunnel_hostname and hostname == current_tunnel_hostname:
        return True
    tunnel_domain = tunnel_mgr.get_tunnel_domain().lower().rstrip(".")
    if tunnel_domain and hostname.endswith(f".{tunnel_domain}"):
        app_name = hostname.removesuffix(f".{tunnel_domain}")
        return bool(_APP_HOST_RE.fullmatch(app_name))
    return False


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


class RejectAppHostWebSocketMiddleware:
    """Refuse app-host websockets; apps are served only by the gateway."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "websocket":
            headers = {
                key.decode("latin-1").lower(): value.decode("latin-1")
                for key, value in scope.get("headers", [])
            }
            if _extract_app_subdomain(headers.get("host", "")) is not None:
                await send({"type": "websocket.close", "code": 1008})
                return
        await self.inner(scope, receive, send)


app.add_middleware(RejectAppHostWebSocketMiddleware)


@app.middleware("http")
async def legacy_app_host_redirect(request: Request, call_next):
    """Redirect legacy lodge app hosts to the app-only gateway."""
    host = request.headers.get("host") or ""
    app_name = _extract_app_subdomain(host)
    if app_name:
        target = _gateway_target(app_name, request, request.url.path)
        if target is None:
            return HTMLResponse(_apps_domain_required(), status_code=404)
        if request.url.query:
            target += f"?{request.url.query}"
        return RedirectResponse(target, status_code=302)
    return await call_next(request)


def _app_access_settings():
    """Return verified-access configuration when that optional feature exists."""
    try:
        from .access import load_access_settings
    except ImportError:
        return None
    try:
        return load_access_settings(WOLTS_DIR)
    except RuntimeError:
        return None


def _apps_domain_required() -> str:
    return """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>App address</title><body style="font:16px system-ui;max-width:34rem;margin:12vh auto;padding:2rem">
<h1>Apps are served on the app domain</h1><p>Configure an app domain in Settings, then open the app there.</p></body></html>"""


def _gateway_target(app_name: str, request: Request, path: str = "/") -> str | None:
    hostname = (request.url.hostname or "").lower().rstrip(".")
    local = hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost")
    clean_path = path if path.startswith("/") else f"/{path}"
    if local:
        return f"http://{app_name}.localhost:{get_app_gateway_port()}{clean_path}"
    apps_domain = get_apps_domain()
    return f"https://{app_name}.{apps_domain}{clean_path}" if apps_domain else None


def _separate_apps_domain_configured() -> bool:
    try:
        config = json.loads((WOLTS_DIR / "woltspace.json").read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(config, dict) and bool(config.get("apps_domain"))


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


def _loopback_http_hostname(hostname: str) -> bool:
    return hostname in {"localhost", "127.0.0.1", "::1"} or hostname.endswith(".localhost")


def _loopback_asgi_client(scope: dict) -> bool:
    client = scope.get("client")
    if not client or not client[0]:
        return False
    try:
        return ipaddress.ip_address(client[0]).is_loopback
    except ValueError:
        return False


class AccessTokenMiddleware:
    """Verify remote HTTP and websocket scopes before either can be routed."""

    def __init__(self, inner):
        self.inner = inner

    async def _reject(self, scope, receive, send, message: str):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
        else:
            await JSONResponse({"error": message}, status_code=403)(scope, receive, send)

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in {"http", "websocket"}:
            return await self.inner(scope, receive, send)
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        host_header = headers.get("host", "")
        hostname, _port = _split_host(host_header)
        if _loopback_http_hostname(hostname) and _loopback_asgi_client(scope):
            return await self.inner(scope, receive, send)
        if not _allowed_http_hostname(hostname):
            return await self._reject(scope, receive, send, "untrusted request host")
        try:
            settings = load_access_settings(WOLTS_DIR)
        except RuntimeError as exc:
            message = "Access verification is misconfigured"
            if "unreadable" in str(exc).lower():
                message = (
                    "woltspace.json is unreadable - fix it or remove the access block; "
                    "localhost still works"
                )
            return await self._reject(scope, receive, send, message)
        if settings is None:
            return await self.inner(scope, receive, send)
        token = headers.get("cf-access-jwt-assertion", "").strip()
        if not token:
            return await self._reject(scope, receive, send, "Access token required")
        app_name = _extract_app_subdomain(host_header)
        audience = settings.apps_aud if app_name else settings.lodge_aud
        try:
            claims = await access_token_verifier.verify(token, settings, audience)
        except Exception:
            return await self._reject(scope, receive, send, "invalid Access token")
        email = claims.get("email")
        if not isinstance(email, str) or not email.strip():
            return await self._reject(scope, receive, send, "Access token has no email")
        email = email.strip().lower()
        if not app_name and email != settings.owner_email:
            return await self._reject(scope, receive, send, "owner identity required")
        scope.setdefault("state", {})["access_email"] = email
        await self.inner(scope, receive, send)


app.add_middleware(AccessTokenMiddleware)


@app.options("/{path:path}")
async def options_handler():
    return Response(status_code=204)


# --- Helpers ---

def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _app_not_found(name: str) -> str:
    """Styled 404 page for an app that doesn't exist."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — not found</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #1a1a1a; color: #c8b89a;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    max-width: 420px; width: 100%; text-align: center;
    border: 1px solid #3a3a2a; border-radius: 12px;
    padding: 2.5rem 2rem; background: #222218;
  }}
  .emoji {{ font-size: 3rem; margin-bottom: 1rem; }}
  h1 {{ font-size: 1.3rem; color: #e8d8b8; margin-bottom: 0.5rem; }}
  .desc {{ font-size: 0.85rem; color: #8a8060; margin-bottom: 1.5rem; }}
  .hint {{ font-size: 0.75rem; color: #5a5a4a; }}
</style>
</head>
<body>
<div class="card">
  <div class="emoji">🌲</div>
  <h1>{name}</h1>
  <p class="desc">This app doesn't exist yet.</p>
  <p class="hint">Ask your wolt to create it, or check the name.</p>
</div>
</body>
</html>"""


def _app_placeholder(name: str, app_obj: "WoltspaceApp | None" = None) -> str:
    """Styled placeholder page for an app that has no servable content."""
    emoji = app_obj.emoji if app_obj else "📦"
    desc = app_obj.description if app_obj and app_obj.description else "No description yet"
    keeper = app_obj.keeper if app_obj else "unknown"
    start_cmd = app_obj.start if app_obj and app_obj.start else None
    status = "Press start to start your app" if start_cmd else "No start command configured"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} — off</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #1a1a1a; color: #c8b89a;
    display: flex; align-items: center; justify-content: center;
    min-height: 100vh; padding: 2rem;
  }}
  .card {{
    max-width: 420px; width: 100%; text-align: center;
    border: 1px solid #3a3a2a; border-radius: 12px;
    padding: 2.5rem 2rem; background: #222218;
  }}
  .emoji {{ font-size: 3rem; margin-bottom: 1rem; }}
  h1 {{ font-size: 1.3rem; color: #e8d8b8; margin-bottom: 0.5rem; }}
  .desc {{ font-size: 0.85rem; color: #8a8060; margin-bottom: 1.5rem; }}
  .status {{
    font-size: 0.8rem; color: #6a7a4a;
    border-top: 1px solid #3a3a2a; padding-top: 1rem;
  }}
  .keeper {{ font-size: 0.75rem; color: #5a5a4a; margin-top: 0.8rem; }}
</style>
</head>
<body>
<div class="card">
  <div class="emoji">{emoji}</div>
  <h1>{name}</h1>
  <p class="desc">{desc}</p>
  <p class="status">{status}</p>
  <p class="keeper">keeper: {keeper}</p>
</div>
</body>
</html>"""


# jerpint: what trickery is this well need to revise all these hacks
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
        set_current_url(url, session, body.get("port", 7777))
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
    try:
        result = await send_notification(session, message, explicit=explicit or None)
        print(f"[notify] → {result.get('adapter')} | {message[:80]}")
        bot_log("notify_sent", {"session": session, **result, "message": message})
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
    except Exception as e:
        print(f"[notify] error: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


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
    if status == "delivered":
        print(f"[message] → {safe}: {text[:80]}")
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


@app.post("/sessions/{session_id}/message")
async def session_message(session_id: str, request: Request):
    """Deliver to a running session, or resume a resting one on contact."""
    safe = sanitize_session(session_id)
    body = await request.json()
    text = body.get("text")
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    return await _deliver_or_resume(safe, text, body)


def _rest_session_locked(safe: str, expected_digest: str):
    """Verify and stop one session without blocking the server event loop."""
    from sessions import SessionRegistry
    registry = SessionRegistry(WOLTS_DIR)
    data = registry.get(safe, check_alive=False)
    if data is None:
        return JSONResponse({"error": "session not found"}, status_code=404)
    wolt = data.get("wolt", "")
    with registry._lock(wolt, safe):
        data = registry.get(safe, wolt=wolt, check_alive=False)
        if data is None:
            return JSONResponse({"error": "session not found"}, status_code=404)
        if data.get("status") != "running":
            return {"ok": True, "status": data.get("status"), "session": safe}
        # Resting must be reversible: stamp a recoverable id, or refuse.
        # (we already hold this session's lock, so persist with the raw write)
        if not claim_resume_id(registry, data, lambda d: registry._write(wolt, safe, d)):
            return JSONResponse({"error": "session has no conversation to resume; not resting it"}, status_code=409)
        try:
            pane = get_runtime().capture(RuntimeHandle.from_record(data), start=None)
        except Exception:
            return JSONResponse({"error": "session activity could not be verified"}, status_code=409)
        current_digest = hashlib.sha256(pane.encode("utf-8")).hexdigest()
        if current_digest != expected_digest:
            return JSONResponse({"error": "session became active; rest aborted"}, status_code=409)
        get_runtime().stop(RuntimeHandle.from_record(data))
        now = int(time.time())
        data["status"] = "resting"
        data["rested_at"] = now
        registry._write(wolt, safe, data)
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
    expected_digest = body.get("pane_digest") if isinstance(body, dict) else None
    if not isinstance(expected_digest, str) or len(expected_digest) != 64:
        return JSONResponse({"error": "pane_digest required"}, status_code=400)
    return await asyncio.to_thread(_rest_session_locked, safe, expected_digest)


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
        resting = [s for s in registry.list(wolt=safe_wolt) if s.get("status") == "resting"]
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
      - type: creature type (required, one of: otter, beaver, raccoon)

    The server scaffolds the full wolt directory before spawning the session.
    External mode adds per-wolt harness isolation; native mode inherits host auth.
    """
    body = await request.json()
    wolt_name = (body.get("name") or "").strip().lower()
    wolt_type = (body.get("type") or "").strip().lower()
    requested_harness = (body.get("harness") or "").strip()
    selected_harness = requested_harness or get_default_harness()

    # Validate name
    if not wolt_name:
        return JSONResponse({"detail": "name is required"}, status_code=400)
    import re
    if not re.match(r'^[a-z][a-z0-9-]*$', wolt_name):
        return JSONResponse({"detail": "name must start with a letter and contain only lowercase letters, numbers, and hyphens"}, status_code=400)
    if len(wolt_name) > 20:
        return JSONResponse({"detail": "name must be 20 characters or less"}, status_code=400)

    # Validate type — only rodent types can be created from the lodge
    if wolt_type not in ("otter", "beaver", "raccoon"):
        return JSONResponse({"detail": "type must be otter, beaver, or raccoon"}, status_code=400)
    if selected_harness not in HARNESSES:
        return JSONResponse({"detail": f"unknown harness: {selected_harness}"}, status_code=400)

    try:
        # Step 1: Scaffold the wolt with environment-appropriate harness config.
        from wolts import create_creature_wolt
        # Only an explicit request becomes a durable per-wolt override. An API
        # caller that omits harness keeps following the lodge default later.
        create_creature_wolt(wolt_name, wolt_type, harness=requested_harness)
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
        return result
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
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
    observations = get_pane_activity() if timeout is not None else {}
    now = int(time.time())
    for session in sessions:
        session["idle_timeout_seconds"] = timeout
        observed = observations.get(session.get("name", ""), {}).get("unchanged_since")
        if timeout is not None and isinstance(observed, int) and session.get("status") == "running":
            session["idle_seconds"] = max(0, now - observed)
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


@app.get("/history/{spark_id}")
async def history_detail(spark_id: str):
    try:
        data = await get_spark_with_chain(spark_id)
        return HTMLResponse(
            data["html"],
            headers={
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
# Enough of the journal's tail to satisfy the largest allowed page even when
# every line is filtered out; the file is append-only and never rewritten.
_WOLF_JOURNAL_TAIL = 5000


def _wolf_state_dir() -> Path:
    from paths import space_wolf_dir

    return space_wolf_dir(WOLTS_DIR)


def _wolf_last_run(state_dir: Path, wolt: str, cron_name: str) -> str | None:
    """The `YYYY-MM-DD-HH:MM` stamp the scheduler writes after each fire.

    A cron name comes from a wolt's own `wolf.json`, which is a file a wolt can
    write — so it is untrusted input on the way to a path join. `../../secret`
    reads outside the state dir otherwise. `wolfcore.stamp_path` holds both
    gates: wolt and name must look like plain identifiers, and the resolved
    path must still be inside the state dir.
    """
    return wolfcore.read_last_run(state_dir, wolt, cron_name)


@app.get("/wolf/schedules")
def wolf_schedules():
    """Every wolt's registered crons, with the last time each one fired.

    Names, timings and stamps only — see the note above on what is withheld.
    A plain `def`: this walks the filesystem, and doing that on the event loop
    blocks every other request while it runs.
    """
    state_dir = _wolf_state_dir()
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
                "last_run": _wolf_last_run(state_dir, wolt, cron.get("name", "")),
            } for cron in crons],
        })
    return {"wolts_dir": str(WOLTS_DIR), "schedules": schedules}


@app.get("/wolf/fires")
def wolf_fires(limit: int = 50, cron: str = "", wolt: str = ""):
    """Recent cron fires, newest first, from the scheduler's job journal.

    `limit` caps the response; `cron` and `wolt` narrow it. A journal that does
    not exist yet is an empty list, not an error — a colony whose wolf has never
    fired is a normal colony. Only the journal's tail is parsed: this answers
    "what happened lately", and a colony that has been firing crons for a year
    should not pay for all of it to serve `limit=1`.
    """
    limit = max(0, min(limit, _WOLF_FIRES_MAX))
    journal = _wolf_state_dir() / "jobs.jsonl"
    fires = []
    try:
        with journal.open() as handle:
            tail = deque(handle, maxlen=_WOLF_JOURNAL_TAIL)
    except OSError:
        tail = ()
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn last line while the wolf is mid-append
        if cron and entry.get("cron") != cron:
            continue
        if wolt and entry.get("owner") != wolt:
            continue
        fires.append(entry)
    fires.reverse()
    return {"count": len(fires), "fires": fires[:limit]}


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
    state_dir = _wolf_state_dir()
    crons, errors = wolfcore.load_all(WOLTS_DIR)
    out = [wolfcore.describe(c, c["_owner"], state_dir, now) for c in crons]
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
        wolfcore.describe(entry, wolt, _wolf_state_dir(), now), status_code=201)


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
            wolfcore.move_last_run(_wolf_state_dir(), wolt, name, target, name)
    print(f"[wolf] updated {wolt}/{name}" + (f" → {target}" if target != wolt else ""))
    return wolfcore.describe(entry, target, _wolf_state_dir(), wolfcore.now_local())


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
    stamp = wolfcore.stamp_path(_wolf_state_dir(), wolt, name)
    if stamp is not None:
        stamp.unlink(missing_ok=True)
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
    state_dir = _wolf_state_dir()
    try:
        result = start_session(
            wolt=wolt,
            prompt=entry.get("prompt", ""),
            routing={"adapter": "lodge"},
        )
    except Exception as exc:
        wolfcore.append_journal(state_dir, name, "session", event="manual",
                                owner=wolt, error=str(exc))
        status = 403 if isinstance(exc, PermissionError) else 500
        return _wolf_error(status, str(exc))
    session, url = result.get("name"), result.get("url")
    wolfcore.append_journal(state_dir, name, "session", event="manual",
                            owner=wolt, session=session, link=url)
    print(f"[wolf] manual run {wolt}/{name} → {session}")
    return {"session": session, "url": url}


# --- Harnesses ---
# The agent engine a wolt runs on (claude, codex, …). Pickers/badges read
# /harnesses; the two POSTs set the lodge default and per-wolt overrides.

@app.get("/harnesses")
async def list_harnesses():
    """Registered engines (id, label, emoji, per-tier models) + lodge default."""
    return {"default": get_default_harness(), "harnesses": harness_metadata()}


@app.get("/settings/access")
async def get_access_setting():
    return {"access": access_settings_dict(load_access_settings(WOLTS_DIR))}


@app.post("/settings/access")
async def set_access_setting(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"access"}:
        return JSONResponse({"error": "access is required"}, status_code=400)
    try:
        settings = save_access_settings(WOLTS_DIR, body["access"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {"ok": True, "access": access_settings_dict(settings)}


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


@app.get("/settings/apps-domain")
async def get_apps_domain_setting():
    return {"apps_domain": get_apps_domain()}


@app.post("/settings/apps-domain")
async def set_apps_domain_setting(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"apps_domain"}:
        return JSONResponse({"error": "apps_domain is required"}, status_code=400)
    try:
        domain = set_apps_domain(body["apps_domain"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {"ok": True, "apps_domain": domain}


@app.get("/settings/app-gateway")
async def get_app_gateway_setting():
    return {"port": get_app_gateway_port(), "applies": "next lodge start"}


@app.post("/settings/app-gateway")
async def set_app_gateway_setting(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"port"}:
        return JSONResponse({"error": "port is required"}, status_code=400)
    try:
        port = set_app_gateway_port(body["port"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, "port": port, "applies": "next lodge start"}


@app.get("/onboarding/status")
async def get_onboarding_status():
    """First-run UI state, independent of every harness's authentication."""
    return onboarding_status()


@app.post("/onboarding/harness")
async def choose_onboarding_harness(request: Request):
    """Choose the lodge default and complete the first-open prompt."""
    body = await request.json()
    name = (body.get("harness") or "").strip()
    if name not in HARNESSES:
        return JSONResponse({"error": f"unknown harness: {name}"}, status_code=400)
    select_onboarding_harness(name)
    return {"ok": True, "default": name, **onboarding_status()}


@app.post("/harness/default")
async def set_harness_default(request: Request):
    """Set the lodge-wide default engine (woltspace.json harness.default)."""
    body = await request.json()
    name = (body.get("harness") or "").strip()
    if name not in HARNESSES:
        return JSONResponse({"error": f"unknown harness: {name}"}, status_code=400)
    set_default_harness(name)
    return {"ok": True, "default": name}


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
    model = pinned_model if is_valid_model(harness, pinned_model) else tier_default_model(harness, creature)
    return {
        "wolt": safe,
        "harness": harness,
        "model": model,
        "configured": {"harness": cfg.get("harness"), "model": cfg.get("model")},
        "applies": "next session",
        "note": "Applies from the next session.",
    }


def _update_wolt_settings(name: str, body: dict) -> dict:
    if not body or not set(body).issubset({"harness", "model"}):
        raise ValueError("body must contain harness and/or model only")
    safe, wolt_json, cfg = _load_wolt_settings(name)
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
    tier_model = tier_default_model(chosen_harness, creature)
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
    tmp = wolt_json.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(wolt_json)
    result = _wolt_settings_response(safe, cfg)
    result.update({"ok": True, "harness": chosen_harness, "model": chosen_model})
    return result


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
async def list_apps_api(request: Request):
    """List all apps that have woltspace.json."""
    apps = discover_apps()
    running = {r["name"]: r for r in running_apps()}
    result = []
    apps_domain = get_apps_domain()
    for a in apps:
        entry = a.model_dump()
        entry["configured_port"] = a.port
        run_state = running.get(a.name)
        entry["running"] = run_state is not None
        entry["port"] = run_state["port"] if run_state else None
        entry["url"] = _gateway_target(a.name, request)
        entry["tunnel_url"] = run_state.get("tunnel_url") if run_state else None
        entry["own_url"] = f"https://{a.name}.{apps_domain}" if apps_domain else None
        entry["sharing"] = bool(run_state.get("tunnel_pid") and run_state.get("tunnel_url")) if run_state else False
        result.append(entry)
    return result


@app.get("/apps/{name}")
async def app_detail(name: str, request: Request):
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
    entry["url"] = _gateway_target(name, request)
    entry["tunnel_url"] = run_state.get("tunnel_url") if run_state else None
    apps_domain = get_apps_domain()
    entry["own_url"] = f"https://{name}.{apps_domain}" if apps_domain else None
    entry["sharing"] = bool(run_state.get("tunnel_pid") and run_state.get("tunnel_url")) if run_state else False
    access_settings = _app_access_settings()
    entry["share_controls_enabled"] = access_settings is not None
    entry["share_entries"] = read_app_shares(WOLTS_DIR, name)
    entry["separate_app_domain"] = _separate_apps_domain_configured()
    return entry


@app.get("/apps/{name}/sharing")
async def app_sharing_get(name: str):
    if invalid := _invalid_app_name(name):
        return invalid
    if not get_app(name):
        return JSONResponse({"error": f"app {name} not found"}, status_code=404)
    return {
        "entries": read_app_shares(WOLTS_DIR, name),
        "enabled": _app_access_settings() is not None,
    }


@app.put("/apps/{name}/sharing")
async def app_sharing_put(name: str, request: Request):
    if invalid := _invalid_app_name(name):
        return invalid
    if not get_app(name):
        return JSONResponse({"error": f"app {name} not found"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON object body required"}, status_code=400)
    if not isinstance(body, dict) or set(body) != {"entries"}:
        return JSONResponse({"error": "entries is required"}, status_code=400)
    try:
        entries = write_app_shares(WOLTS_DIR, name, body["entries"])
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return {"ok": True, "entries": entries, "enabled": _app_access_settings() is not None}


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


@app.post("/apps/{name}/share")
async def app_share(name: str):
    """Start a cloudflared tunnel to the app port and return the public URL."""
    if invalid := _invalid_app_name(name):
        return invalid
    import asyncio
    try:
        # share_app blocks (polls cloudflared log up to 30s) — run in thread
        result = await asyncio.to_thread(share_app, name)
        print(f"[apps] shared {name} at {result['tunnel_url']}")
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/apps/{name}/unshare")
async def app_unshare(name: str):
    """Stop the cloudflared tunnel for an app."""
    if invalid := _invalid_app_name(name):
        return invalid
    was_sharing = unshare_app(name)
    if was_sharing:
        print(f"[apps] unshared {name}")
        return {"ok": True, "name": name}
    return JSONResponse({"error": f"{name} has no active tunnel"}, status_code=404)


@app.post("/apps/unshare-all")
async def app_unshare_all():
    """Panic button — stop ALL app tunnels."""
    unshared = unshare_all_apps()
    print(f"[apps] unshare-all: stopped {len(unshared)} tunnels: {unshared}")
    return {"ok": True, "unshared": unshared}


# --- Session resume ---

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
            found.append({"wolt": wolt_dir.name, "url": f"/wolt/{wolt_dir.name}/site/"})
    return found


@app.get("/sites/{wolt_name}")
async def site_detail(wolt_name: str):
    """Get a wolt's site state."""
    sdir = site_dir(wolt_name)
    return {
        "wolt": wolt_name,
        "url": f"/wolt/{wolt_name}/site/",
        "dir_exists": sdir.exists(),
    }


@app.api_route("/wolt/{wolt_name}/site/{path:path}", methods=["GET", "HEAD"])
@app.api_route("/wolt/{wolt_name}/site", methods=["GET", "HEAD"])
async def serve_wolt_site(wolt_name: str, request: Request, path: str = ""):
    """Serve a wolt's site directly from disk, injecting the livereload script."""
    if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", wolt_name):
        return JSONResponse({"error": "invalid wolt name"}, status_code=400)

    wolt_dir = WOLTS_DIR / wolt_name
    if not wolt_dir.exists():
        # Self-refresh: the viewport can load this URL before the wolt dir
        # exists (first-boot race) — recover without a manual refresh.
        return HTMLResponse(
            f"<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head>"
            f"<body>Wolt {wolt_name} not found"
            f"<script>setTimeout(()=>location.reload(), 2000)</script></body></html>",
            status_code=404,
        )

    # HTTP GET stays read-only. Session creation/resume owns site scaffolding;
    # merely requesting a guessed wolt URL must never create files.
    sdir = site_dir(wolt_name)
    if not sdir.is_dir():
        return PlainTextResponse("Site not found", status_code=404)

    target = (sdir / path) if path else sdir
    # Security: stay inside the site dir (also catches symlinks pointing out)
    try:
        target.resolve().relative_to(sdir.resolve())
    except ValueError:
        return PlainTextResponse("Not found", status_code=404)

    if target.is_dir():
        # Canonicalize directory URLs to a trailing slash so relative
        # links inside index.html resolve against the directory.
        if not request.url.path.endswith("/"):
            return RedirectResponse(request.url.path + "/", status_code=308)
        # A site with no home page yet lands on the wolt's About page, which
        # the lodge renders from identity.md - a real page from minute zero.
        if not path and not (target / "index.html").is_file():
            return RedirectResponse(f"/wolt/{wolt_name}/_/", status_code=307)
        target = target / "index.html"
    if not target.is_file():
        return PlainTextResponse("Not found", status_code=404)

    if target.suffix in (".html", ".htm"):
        text = target.read_text(encoding="utf-8", errors="replace")
        # Inject the reload script using a wolt-scoped WS path
        reload_script = (
            '<script>(function(){'
            'var p=location.protocol==="https:"?"wss:":"ws:";'
            f'function c(){{var ws=new WebSocket(p+"//"+location.host+"/wolt/{wolt_name}/site/livereload");'
            'ws.onmessage=function(){location.reload()};'
            'ws.onclose=function(){setTimeout(c,3000)}}'
            'c()})()</script>'
        )
        if '</body>' in text:
            text = text.replace('</body>', reload_script + '</body>')
        else:
            text += reload_script
        # The site shell: nav drawer + built-in pages, owned by the lodge and
        # skinned by the wolt's site.json. See container/lib/site_shell.py.
        site_cfg = load_site_config(sdir)
        if shell_wanted(site_cfg, text):
            text = inject_shell(text, shell_manifest(wolt_name, wolt_dir, sdir, site_cfg))
        return HTMLResponse(
            text, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
        )
    return FileResponse(
        target, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )


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
    """Redirect the legacy lodge path to the app-only gateway."""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", app_name):
        return JSONResponse({"error": "invalid app name"}, status_code=400)
    adir = app_dir(app_name)
    if not adir.exists():
        return HTMLResponse(_app_not_found(app_name), status_code=404)

    sub_path = "/" + path if path else "/"
    target = _gateway_target(app_name, request, sub_path)
    if target is None:
        return HTMLResponse(_apps_domain_required(), status_code=404)
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(target, status_code=302)


# --- Tools ---

# jerpint: this is for the telegram bots right? good idea to have them all in one place, but maybe we can have this in a
# separate /bot/ route
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
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.request(
                request.method, target,
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=await request.body(),
            )
            return Response(resp.content, status_code=resp.status_code, headers=dict(resp.headers))
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
    access_settings = load_access_settings(WOLTS_DIR)
    return templates.TemplateResponse(request, "settings.html", context={
        "active_nav": "settings",
        "cache_bust": int(time.time()),
        "harness_default": get_default_harness(),
        "harnesses": harnesses,
        "harness_labels": {harness["id"]: harness["label"] for harness in harnesses},
        "idle_timeout": get_idle_timeout(),
        "access": access_settings_dict(access_settings) or {},
        "apps_domain": get_apps_domain() or "",
        "app_gateway_port": get_app_gateway_port(),
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
        {"name": wolt["dir"], "type": wolt.get("type", "rodent")}
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

    # Try wolt site first, then platform public dir
    resp = await _serve_static(f"/{path}", request)
    if resp:
        return resp
    resp = await _serve_platform_file(path)
    if resp:
        return resp
    return PlainTextResponse("Not found", status_code=404)


_start_time = time.time()
