"""The Mill: apps, each on an origin of its own, in a process of its own.

An app's page must never run with the lodge's origin, where its script could
call the lodge's API as the owner. The Mill serves apps on their own hosts
(`<app>.localhost`) and has no lodge routes at all.

It listens only on a unix socket, `<data root>/.space/run/mill/mill.sock`, in
a folder only the owner can open. The lodge forwards app hosts from its one
port to that socket, and says who the visitor is in IDENTITY_HEADER after
stripping any copy the visitor sent. The Mill serves the owner, or anyone on
an app the owner shared, and answers:

- a running app: proxied to its process on 127.0.0.1, or ::1 if that refuses;
- a static app: GET and HEAD for files under its real folder.

Wolt sites stay on the lodge at `/wolt/<name>/site/` for now.

If the Mill is down, the lodge answers 502 for apps and keeps serving itself.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import WebSocket
from fastapi.responses import HTMLResponse

from .config import WOLTS_DIR
from .app_proxy import (
    APP_PROXY_TIMEOUT,
    APP_WS_MAX_MESSAGE,
    client_response_headers,
    relay_websocket,
    upstream_request_headers,
    websocket_upstream_headers,
)
from .content_hosts import parse_app_host
from .static_files import STATIC_HEADERS, StaticContext, serve_static

IDENTITY_HEADER = "x-woltspace-mill-identity"
OWNER = "owner"
# Anyone, on an app the owner (or a wolt they asked) shared.
PUBLIC = "public"
_STATIC_METHODS = {"GET", "HEAD"}
# Where an app's process listens: IPv4 loopback first, then IPv6, for a dev
# server that bound `localhost` and got ::1 (Vite on macOS does).
APP_LOOPBACKS = ("127.0.0.1", "[::1]")
# A unix socket path must fit sockaddr_un (104 bytes on macOS, 108 on Linux).
SOCKET_PATH_MAX = 100


def socket_path(wolts_dir) -> Path:
    # A container's data root is often a bind mount, which can't always host a
    # unix socket (Docker Desktop on a Mac can't chmod one), so it keeps its own.
    if override := os.environ.get("WOLTSPACE_MILL_SOCKET"):
        return Path(override)
    return Path(wolts_dir) / ".space" / "run" / "mill" / "mill.sock"


def _headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _log(scope, headers, status: int, reason: str) -> None:
    method = "ws" if scope["type"] == "websocket" else scope.get("method", "HTTP")
    host = ascii(headers.get("host", "-"))[1:-1]
    path = ascii(scope.get("path", "/"))[1:-1]
    print(f"{datetime.now(timezone.utc).isoformat()} mill host={host} path={path} "
          f"method={method} status={status} decision={reason}", file=sys.stderr, flush=True)


def _origin_is_this_app(headers: dict[str, str]) -> bool:
    """Origin names exactly this host and port, over http (the Mill's hosts are local)."""
    try:
        origin = urlsplit(headers.get("origin", ""))
        request = urlsplit(f"//{headers.get('host', '')}")
        origin_port = origin.port if origin.port is not None else 80
        request_port = request.port if request.port is not None else 80
    except ValueError:
        return False
    return (
        origin.scheme == "http"
        and bool(origin.hostname)
        and (origin.hostname or "").lower() == (request.hostname or "").lower()
        and origin_port == request_port
        and origin.path in {"", "/"} and not origin.query and not origin.fragment
    )


async def _refuse(scope, receive, send, headers, status: int, reason: str, message: str, **extra):
    _log(scope, headers, status, reason)
    if scope["type"] == "websocket":
        await send({"type": "websocket.close", "code": 1008})
        return
    response = HTMLResponse(f"<h1>{message}</h1>", status_code=status,
                            headers=dict(STATIC_HEADERS, **extra))
    await response(scope, receive, send)


async def _lifespan(receive, send):
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
            return


_READ_METHODS = {"GET", "HEAD", "OPTIONS"}


def _apps_module():
    import apps
    return apps


def static_app_root(app_dir: Path) -> Path | None:
    """A static app's files: its real `dist/` if there is one, else its folder.

    Neither may be a symlink, so the root is always inside the app.
    """
    app_dir = Path(app_dir)
    if os.path.islink(app_dir) or not app_dir.is_dir():
        return None
    dist = app_dir / "dist"
    if os.path.islink(dist):
        return None
    root = dist if dist.is_dir() else app_dir
    real = os.path.realpath(root)
    inside = os.path.commonpath([real, os.path.realpath(app_dir)]) == os.path.realpath(app_dir)
    return Path(real) if inside else None


class Mill:
    def __init__(self, wolts_dir=None, apps=None):
        self.wolts_dir = Path(wolts_dir or WOLTS_DIR)
        self._apps = apps

    def _shared(self, name: str) -> bool:
        """The lodge says who the visitor is; the Mill checks a public one
        against the share record too."""
        import app_sharing
        return app_sharing.shared(self.wolts_dir, name) is not None

    @property
    def apps(self):
        return self._apps if self._apps is not None else _apps_module()

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            return await _lifespan(receive, send)
        if scope.get("type") not in {"http", "websocket"}:
            return
        headers = _headers(scope)
        refuse = lambda status, reason, message, **extra: _refuse(  # noqa: E731
            scope, receive, send, headers, status, reason, message, **extra)

        hosts = [v for k, v in scope.get("headers", []) if k.lower() == b"host"]
        app_host = parse_app_host(headers.get("host", "")) if len(hosts) == 1 else None
        if app_host is None:
            return await refuse(404, "unknown-host", "Not found")
        return await self._app(scope, receive, send, headers, refuse, app_host.name)

    async def _app(self, scope, receive, send, headers, refuse, name: str):
        """An app: proxied to its process on 127.0.0.1 (or ::1), or a static app's files."""
        if scope["type"] == "websocket":
            if not _origin_is_this_app(headers):
                return await refuse(403, "bad-origin", "Invalid websocket origin")
        elif scope.get("method", "GET").upper() not in _READ_METHODS and (
                headers.get("sec-fetch-site", "").lower() == "cross-site"
                or (headers.get("origin") and not _origin_is_this_app(headers))):
            # A browser write must come from the app's own page.
            return await refuse(403, "cross-site", "Cross-site request rejected")
        identity = headers.get(IDENTITY_HEADER)
        if identity == PUBLIC and not self._shared(name):
            return await refuse(403, "not-shared", "This app is the owner's")
        if identity not in (OWNER, PUBLIC):
            return await refuse(403, "not-owner", "This app is the owner's")

        running = {r["name"]: r for r in self.apps.running_apps()}
        state = running.get(name)
        if state is not None:
            if scope["type"] == "websocket":
                return await self._app_websocket(scope, receive, send, headers, int(state["port"]))
            return await self._app_http(scope, receive, send, headers, name, int(state["port"]))

        app = self.apps.get_app(name)
        if app is None:
            return await refuse(404, "unknown-app", "Not found")
        if app.can_start():
            return await refuse(503, "stopped", f"App '{name}' is not running")
        root = static_app_root(self.apps.app_dir(name))
        if scope["type"] == "websocket" or root is None:
            return await refuse(404, "no-static-root", "Not found")
        if scope.get("method", "GET").upper() not in _STATIC_METHODS:
            return await refuse(405, "method", "A static app is read-only", allow="GET, HEAD")
        # A static app's folder may be a whole repository: no dotfiles (.git,
        # .env), at any depth.
        if any(part.startswith(".") for part in scope.get("path", "/").split("/") if part):
            return await refuse(404, "dotfile", "Not found")
        response = serve_static(scope, StaticContext(root=root))
        _log(scope, headers, response.status_code, "static-app")
        return await response(scope, receive, send)

    async def _app_http(self, scope, receive, send, headers, name: str, port: int):
        import httpx
        from starlette.requests import Request
        from starlette.responses import StreamingResponse
        request = Request(scope, receive)
        raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
        has_body = "content-length" in headers or "transfer-encoding" in headers
        query = scope.get("query_string") or b""
        # The raw target, as sent: decoding it would turn %3F or %23 into a
        # query or a fragment.
        raw_path = (scope.get("raw_path") or scope.get("path", "/").encode()).decode("latin-1")
        target = raw_path + ("?" + query.decode("latin-1") if query else "")
        client = httpx.AsyncClient(timeout=APP_PROXY_TIMEOUT)
        try:
            for index, loopback in enumerate(APP_LOOPBACKS):
                req = client.build_request(scope.get("method", "GET"), f"http://{loopback}:{port}{target}",
                                           headers=upstream_request_headers(raw, port),
                                           content=request.stream() if has_body else None)
                try:
                    resp = await client.send(req, stream=True, follow_redirects=False)
                    break
                except httpx.ConnectError:
                    # Refused before a byte of the body was read: the next
                    # loopback gets the request whole.
                    if index == len(APP_LOOPBACKS) - 1:
                        raise
        except httpx.ConnectError:
            await client.aclose()
            response = HTMLResponse(f"<h1>Cannot reach app '{name}'</h1>", status_code=502)
            _log(scope, headers, 502, "upstream-down")
            return await response(scope, receive, send)
        except httpx.TimeoutException:
            await client.aclose()
            response = HTMLResponse(f"<h1>App '{name}' did not answer in time</h1>", status_code=504)
            _log(scope, headers, 504, "upstream-timeout")
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
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in client_response_headers(resp.headers.multi_items())
        ]
        _log(scope, headers, resp.status_code, "app")
        return await response(scope, receive, send)

    async def _app_websocket(self, scope, receive, send, headers, port: int):
        import websockets
        ws = WebSocket(scope, receive, send)
        raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])]
        protocols = [p.strip() for p in headers.get("sec-websocket-protocol", "").split(",") if p.strip()]
        query = scope.get("query_string") or b""
        raw_path = (scope.get("raw_path") or scope.get("path", "/").encode()).decode("latin-1")
        target = raw_path + ("?" + query.decode("latin-1") if query else "")
        upstream = None
        for loopback in APP_LOOPBACKS:
            try:
                upstream = await websockets.connect(
                    f"ws://{loopback}:{port}{target}", subprotocols=protocols or None,
                    max_size=APP_WS_MAX_MESSAGE, proxy=None,
                    additional_headers=websocket_upstream_headers(raw, port))
                break
            except OSError:
                continue  # refused here: try the other loopback
            except Exception:
                break
        if upstream is None:
            _log(scope, headers, 502, "upstream-ws-down")
            return await send({"type": "websocket.close", "code": 1011})
        try:
            await ws.accept(subprotocol=upstream.subprotocol)
            _log(scope, headers, 101, "app")
            await relay_websocket(ws, upstream)
        finally:
            await upstream.close()
            try:
                await ws.close()
            except Exception:
                pass


app = Mill()


# --- Running it -------------------------------------------------------------------

def prepare_socket(path: Path) -> None:
    """Make the socket's folder private, and clear a stale socket.

    A socket another Mill still answers on is left alone, and this one stops:
    never unlink a live socket.
    """
    if len(os.fsencode(str(path))) > SOCKET_PATH_MAX:
        raise SystemExit(f"mill: socket path is too long for a unix socket ({path}); "
                         "use a shorter data root")
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.exists() or path.is_symlink():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(str(path))
        except OSError:
            path.unlink()
        else:
            raise SystemExit(f"mill: another Mill is already serving {path}")
        finally:
            probe.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m server.mill")
    parser.add_argument("--socket", default="")
    args = parser.parse_args(argv)
    path = Path(args.socket) if args.socket else socket_path(WOLTS_DIR)
    prepare_socket(path)
    import uvicorn
    try:
        uvicorn.run(app, uds=str(path), log_level="warning", lifespan="on")
    finally:
        try:
            path.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
