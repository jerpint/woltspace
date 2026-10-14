"""Static file serving for a wolt's site on its own gateway host.

The gateway has already decided who the visitor is and that they may see this
site. This module only turns a request into bytes from the site's canonical
root, plus two platform extras under the reserved `/_woltspace/` namespace:
the shell assets a site page needs, and (in site_livereload) the owner's
reload socket. Nothing else lives on a site host, so a site file named
`settings` is a file named settings, and nothing a site contains can shadow
the reserved namespace.

Every response carries the same policy headers: `nosniff`, the response
sandbox (`allow-same-origin` is this site's own origin, so fetch, storage and
its reload socket work), and `private, no-store`, so a revoked share leaves
nothing in a cache.
"""

from __future__ import annotations

import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

from fastapi.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse

from site_shell import (
    PUBLIC_ASSET_BASE,
    PUBLIC_ASSETS,
    inject_shell,
    load_site_config,
    public_shell_manifest,
    shell_wanted,
)

from .config import PUBLIC_DIR

RESERVED_PREFIX = "/_woltspace"
LIVERELOAD_PATH = "/_woltspace/livereload"
# The only platform files a site host serves, by name: nothing else under
# public/ is reachable from a content host.
SHELL_ASSET_DIR = PUBLIC_DIR / "static" / "wolt-shell"
SHELL_ASSETS = {name: SHELL_ASSET_DIR / name for name in PUBLIC_ASSETS}

SITE_HEADERS = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "sandbox allow-scripts allow-same-origin",
    "cache-control": "private, no-store",
}

# An HTML page bigger than this is served as-is: no shell, no reload client.
HTML_INJECT_MAX = 4 * 1024 * 1024
_CHUNK = 64 * 1024

# Owner only: reconnects with backoff, reloads on the one event the server
# sends. Sent to nobody else, so a shared viewer's page never opens a socket.
RELOAD_CLIENT = (
    '<script>(function(){var p=location.protocol==="https:"?"wss:":"ws:",d=1000;'
    'function c(){var ws=new WebSocket(p+"//"+location.host+"' + LIVERELOAD_PATH + '");'
    'ws.onopen=function(){d=1000};'
    'ws.onmessage=function(e){try{if(JSON.parse(e.data).type==="reload")location.reload()}catch(_){}};'
    'ws.onclose=function(){setTimeout(c,d);d=Math.min(d*2,30000)}}c()})()</script>'
)


@dataclass(frozen=True)
class SiteContext:
    wolt_name: str      # the wolt's directory name
    wolt_dir: Path
    root: Path          # canonical site root (realpath, anchored)
    owner: bool
    lodge_origin: str | None


_ENCODED_SEPARATOR = re.compile(rb"%(?:2f|5c)", re.I)


def split_site_path(raw_path: bytes) -> tuple[list[str], bool] | None:
    """Percent-decode a request target into path segments.

    Returns (segments, trailing_slash), or None when the target cannot name a
    file: a percent-encoded slash or backslash anywhere, NUL or backslash in
    a segment, `.` or `..`, an empty segment in the middle, or bytes that are
    not UTF-8. Separators are only ever the literal `/` the visitor sent.
    """
    if not raw_path.startswith(b"/"):
        return None
    if _ENCODED_SEPARATOR.search(raw_path):
        return None
    try:
        decoded = unquote_to_bytes(raw_path).decode("utf-8")
    except UnicodeDecodeError:
        return None
    parts = decoded.split("/")[1:]
    trailing = parts and parts[-1] == ""
    if trailing:
        parts = parts[:-1]
    for part in parts:
        if part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part:
            return None
    return parts, bool(trailing)


def resolve_in_root(root: Path, segments: list[str]) -> str | None:
    """The realpath of root/segments, only if it stays inside root."""
    anchor = str(root)
    target = os.path.realpath(os.path.join(anchor, *segments)) if segments else anchor
    if target == anchor or target.startswith(anchor + os.sep):
        return target
    return None


def _with_policy(response: Response) -> Response:
    for key, value in SITE_HEADERS.items():
        response.headers[key] = value
    return response


def not_found() -> Response:
    return _with_policy(PlainTextResponse("Not found", status_code=404))


def _media_type(path: str) -> str:
    guessed, _encoding = mimetypes.guess_type(path, strict=False)
    media = guessed or "application/octet-stream"
    if media.startswith("text/") or media in {"application/json", "application/javascript"}:
        media += "; charset=utf-8"
    return media


def _open_regular(realpath: str) -> tuple[int, os.stat_result] | None:
    """Open exactly the validated path; only a regular file comes back.

    O_NOFOLLOW guards the final component and fstat confirms what was opened.
    Intermediate components are the same user's own directories and are not
    re-walked by descriptor; this is a serving policy, not a race-proof
    boundary against that user.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(realpath, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        return None
    return fd, st


def _parse_range(value: str, size: int) -> tuple[int, int] | None | bool:
    """One byte range as (start, end) inclusive; None to ignore; False when unsatisfiable."""
    value = value.strip()
    if not value.lower().startswith("bytes=") or "," in value:
        return None
    first, sep, last = value[6:].strip().partition("-")
    if not sep:
        return None
    try:
        if first == "":
            length = int(last)
            if length <= 0:
                return False
            start, end = max(size - length, 0), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return False
    return start, min(end, size - 1)


def _file_response(scope, realpath: str, *, media_type: str | None = None) -> Response:
    """Stream a regular file: HEAD, If-None-Match and a single Range honoured."""
    opened = _open_regular(realpath)
    if opened is None:
        return not_found()
    fd, st = opened
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
    base = dict(SITE_HEADERS, etag=etag, **{"accept-ranges": "bytes"})
    if headers.get("if-none-match", "").strip() == etag:
        os.close(fd)
        return Response(status_code=304, headers=base)
    start, end, status = 0, st.st_size - 1, 200
    if "range" in headers:
        wanted = _parse_range(headers["range"], st.st_size)
        if wanted is False:
            os.close(fd)
            base["content-range"] = f"bytes */{st.st_size}"
            return Response(status_code=416, headers=base)
        if wanted is not None:
            start, end = wanted
            status = 206
            base["content-range"] = f"bytes {start}-{end}/{st.st_size}"
    length = end - start + 1 if st.st_size else 0
    base["content-length"] = str(length)
    media = media_type or _media_type(realpath)
    if scope.get("method", "GET").upper() == "HEAD":
        os.close(fd)
        return Response(status_code=status, headers=base, media_type=media)

    async def body():
        try:
            os.lseek(fd, start, os.SEEK_SET)
            remaining = length
            while remaining > 0:
                chunk = os.read(fd, min(_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk
        finally:
            os.close(fd)

    return StreamingResponse(body(), status_code=status, headers=base, media_type=media)


def _html_response(scope, realpath: str, ctx: SiteContext) -> Response:
    opened = _open_regular(realpath)
    if opened is None:
        return not_found()
    fd, st = opened
    if st.st_size > HTML_INJECT_MAX:
        os.close(fd)
        return _file_response(scope, realpath, media_type="text/html; charset=utf-8")
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read()
    except OSError:
        return not_found()
    text = raw.decode("utf-8", errors="replace")
    if ctx.owner:
        idx = text.lower().rfind("</body>")
        text = text + RELOAD_CLIENT if idx == -1 else text[:idx] + RELOAD_CLIENT + text[idx:]
    site_cfg = load_site_config(ctx.root)
    if shell_wanted(site_cfg, text):
        manifest = public_shell_manifest(
            ctx.wolt_name, ctx.wolt_dir, ctx.root, site_cfg,
            lodge_origin=ctx.lodge_origin if ctx.owner else None,
        )
        text = inject_shell(text, manifest)
    body = text.encode("utf-8")
    response = Response(
        b"" if scope.get("method", "GET").upper() == "HEAD" else body,
        media_type="text/html; charset=utf-8",
        headers=dict(SITE_HEADERS, **{"content-length": str(len(body))}),
    )
    return response


def _reserved(scope, path: str) -> Response:
    """The platform's own namespace on a site host: shell assets only."""
    if path.startswith(PUBLIC_ASSET_BASE):
        name = path[len(PUBLIC_ASSET_BASE):]
        asset = SHELL_ASSETS.get(name)
        if asset is not None:
            return _file_response(scope, str(asset))
    return not_found()


def serve_site(scope, ctx: SiteContext) -> Response:
    """Answer a GET/HEAD for a site file; the caller has done every check."""
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
    parsed = split_site_path(raw_path)
    if parsed is None:
        return not_found()
    segments, trailing = parsed
    if segments and segments[0] == RESERVED_PREFIX[1:]:
        return _reserved(scope, "/" + "/".join(segments) + ("/" if trailing else ""))
    target = resolve_in_root(ctx.root, segments)
    if target is None:
        return not_found()
    try:
        st = os.stat(target)
    except OSError:
        return not_found()
    if stat.S_ISDIR(st.st_mode):
        if not trailing:
            # Relative links in index.html resolve against the directory.
            # The target is the clean path the visitor sent plus a slash,
            # re-encoded, so the redirect can never leave this host.
            location = raw_path.decode("latin-1") + "/"
            query = scope.get("query_string") or b""
            if query:
                location += "?" + query.decode("latin-1")
            return _with_policy(RedirectResponse(location, status_code=308))
        target = resolve_in_root(ctx.root, segments + ["index.html"])
        if target is None:
            return not_found()
        try:
            st = os.stat(target)
        except OSError:
            return not_found()
    if not stat.S_ISREG(st.st_mode):
        return not_found()
    if target.lower().endswith((".html", ".htm")):
        return _html_response(scope, target, ctx)
    return _file_response(scope, target)
