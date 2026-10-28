"""The Mill: wolt sites, each on an origin of its own, in a process of its own.

A page a wolt wrote must never run with the lodge's origin, where its script
could call the lodge's API as the owner. The Mill serves sites on their own
hosts (`<wolt>.sites.localhost`) and has no lodge routes at all.

It listens only on a unix socket, `<data root>/.space/run/mill/mill.sock`, in
a folder only the owner can open. The lodge forwards site hosts from its one
port to that socket, and says who the visitor is in IDENTITY_HEADER after
stripping any copy the visitor sent. The Mill serves the owner only (shared
sites come with remote access), refuses everything before opening a file, and
answers:

- GET and HEAD for files under the wolt's real `wolt/site` folder;
- the owner's reload socket at `/_woltspace/livereload`, from the site's own
  pages only.

If the Mill is down, the lodge answers 502 for sites and keeps serving itself.
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
from fastapi.responses import HTMLResponse, PlainTextResponse

from .config import WOLTS_DIR
from .content_hosts import anchored_site_root, find_wolt_dir, parse_site_host
from .site_livereload import hub as livereload_hub, serve_site_livereload
from .site_server import LIVERELOAD_PATH, SITE_HEADERS, SiteContext, serve_site

IDENTITY_HEADER = "x-woltspace-mill-identity"
OWNER = "owner"
_SITE_METHODS = {"GET", "HEAD"}
# A unix socket path must fit sockaddr_un (104 bytes on macOS, 108 on Linux).
SOCKET_PATH_MAX = 100


def socket_path(wolts_dir) -> Path:
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


def _origin_is_this_site(headers: dict[str, str]) -> bool:
    """Origin names exactly this host and port, over http (sites are local)."""
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
                            headers=dict(SITE_HEADERS, **extra))
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


class Mill:
    def __init__(self, wolts_dir=None):
        self.wolts_dir = Path(wolts_dir or WOLTS_DIR)

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "lifespan":
            return await _lifespan(receive, send)
        if scope.get("type") not in {"http", "websocket"}:
            return
        headers = _headers(scope)
        refuse = lambda status, reason, message, **extra: _refuse(  # noqa: E731
            scope, receive, send, headers, status, reason, message, **extra)

        hosts = [v for k, v in scope.get("headers", []) if k.lower() == b"host"]
        site = parse_site_host(headers.get("host", "")) if len(hosts) == 1 else None
        if site is None:
            return await refuse(404, "unknown-host", "Not found")
        if scope["type"] == "http":
            if scope.get("method", "GET").upper() not in _SITE_METHODS:
                return await refuse(405, "method", "Sites are read-only", allow="GET, HEAD")
        elif scope.get("path", "") != LIVERELOAD_PATH:
            return await refuse(404, "no-such-socket", "Not found")
        elif not _origin_is_this_site(headers):
            return await refuse(403, "bad-origin", "Invalid websocket origin")
        # A service worker would outlive the site and its access; refused
        # whatever the script is called.
        if "service-worker" in headers:
            return await refuse(403, "service-worker", "Service workers are not allowed on sites")
        if headers.get(IDENTITY_HEADER) != OWNER:
            return await refuse(403, "not-owner", "This site is the owner's")

        wolt_dir = find_wolt_dir(self.wolts_dir, site.label)
        root = anchored_site_root(wolt_dir) if wolt_dir is not None else None
        if root is None:
            _log(scope, headers, 404, "unknown-site")
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 1008})
            response = PlainTextResponse("Not found", status_code=404, headers=SITE_HEADERS)
            return await response(scope, receive, send)

        if scope["type"] == "http":
            ctx = SiteContext(wolt_name=wolt_dir.name, wolt_dir=wolt_dir, root=root, owner=True)
            response = serve_site(scope, ctx)
            _log(scope, headers, response.status_code, "owner")
            return await response(scope, receive, send)

        async def still_there() -> bool:
            current = find_wolt_dir(self.wolts_dir, site.label)
            return current is not None and anchored_site_root(current) == root

        _log(scope, headers, 101, "owner")
        await serve_site_livereload(WebSocket(scope, receive, send), str(root),
                                    still_authorized=still_there)


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
