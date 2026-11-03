"""Static file serving for a static app on its own origin, in the Mill.

The Mill has already decided that the visitor may see this app. This module
only turns a request into bytes from the app's canonical root.

Every response carries the same policy headers: `nosniff`, the response
sandbox (`allow-same-origin` is the app's own origin, so fetch and storage
work), and `private, no-store`, so a revoked share leaves nothing in a cache.
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

STATIC_HEADERS = {
    "x-content-type-options": "nosniff",
    # The app's own origin, with what ordinary pages need: forms, new tabs,
    # dialogs and downloads. Its script still can't reach the lodge's origin.
    "content-security-policy": (
        "sandbox allow-scripts allow-same-origin allow-forms allow-popups "
        "allow-modals allow-downloads"
    ),
    "cache-control": "private, no-store",
}

_CHUNK = 64 * 1024


@dataclass(frozen=True)
class StaticContext:
    root: Path          # canonical root (realpath, anchored)
    # A static app's folder may be a whole repository: nothing whose real
    # path, under the root, has a dot-named part (.git, .env) is served,
    # however the request reached it (a symlink included).
    hide_dotfiles: bool = True


_ENCODED_SEPARATOR = re.compile(rb"%(?:2f|5c)", re.I)


def split_static_path(raw_path: bytes) -> tuple[list[str], bool] | None:
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
    for key, value in STATIC_HEADERS.items():
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
    """Stream a regular file: HEAD, If-None-Match and a single Range (not with If-Range) honoured."""
    opened = _open_regular(realpath)
    if opened is None:
        return not_found()
    fd, st = opened
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }
    etag = f'W/"{st.st_mtime_ns:x}-{st.st_size:x}"'
    base = dict(STATIC_HEADERS, etag=etag, **{"accept-ranges": "bytes"})
    if headers.get("if-none-match", "").strip() == etag:
        os.close(fd)
        return Response(status_code=304, headers=base)
    start, end, status = 0, st.st_size - 1, 200
    # If-Range needs a strong validator to match, and this ETag is weak (no
    # Last-Modified is sent either): a resumed download whose copy may be stale
    # always gets the whole file, never new bytes spliced onto old ones.
    if "range" in headers and "if-range" not in headers:
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


def serve_static(scope, ctx: StaticContext) -> Response:
    """Answer a GET/HEAD for a file; the caller has done every check."""
    raw_path = scope.get("raw_path") or scope.get("path", "/").encode("utf-8")
    parsed = split_static_path(raw_path)
    if parsed is None:
        return not_found()
    segments, trailing = parsed
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
    if ctx.hide_dotfiles and any(
            part.startswith(".") for part in Path(os.path.relpath(target, ctx.root)).parts):
        return not_found()
    return _file_response(scope, target)
