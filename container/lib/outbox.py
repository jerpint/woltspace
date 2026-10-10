"""
The outbox: a lodge-owned spool for files a wolt wants to send.

`notify --file` copies the file here under a random id and names that id in
its request. The lodge opens the entry by id and nothing else, so no caller can
make it read a path of the caller's choosing.

Stdlib only: the notify command and the server both import this.
"""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import stat
import time
from pathlib import Path

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ENTRY_AGE_SECONDS = 3600
ENTRY_ID_RE = re.compile(r"[0-9a-f]{32}")

_DOTENV_TEMPLATE_SUFFIXES = (".example", ".sample")
_MAX_NAME_CHARS = 200
_MAX_SUFFIX_CHARS = 16


class OutboxError(ValueError):
    """`reason` is machine-readable; str(error) is for a human."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def credential_rule(path: Path) -> str | None:
    """Which credential rule a path trips, if any. The rules of backup.secret_rule."""
    name = path.name
    parent = path.parent.name
    if name == ".env" or (
        name.startswith(".env.") and not name.endswith(_DOTENV_TEMPLATE_SUFFIXES)
    ):
        return "dotenv"
    if parent == ".claude" and name.startswith(".credentials.json"):
        return "claude-credentials"
    if parent == ".codex" and name == "auth.json":
        return "codex-auth"
    return None


def check_source(path: Path) -> int:
    """Size in bytes of a file that may be staged; OutboxError says why not."""
    path = Path(path)
    try:
        info = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        raise OutboxError("not_found", f"file not found: {path}") from None
    except OSError as exc:
        raise OutboxError(
            "unreadable", f"cannot read {path}: {(exc.strerror or str(exc)).lower()}"
        ) from None
    if not stat.S_ISREG(info.st_mode):
        raise OutboxError("not_a_file", f"not a regular file: {path}")
    # The rules read names, so judge the name as typed and the file it leads to.
    for candidate in (os.path.abspath(path), os.path.realpath(path)):
        if credential_rule(Path(candidate)):
            raise OutboxError(
                "credential", f"refusing to send a credential file: {path.name}"
            )
    if not os.access(path, os.R_OK):
        raise OutboxError("unreadable", f"cannot read {path}: permission denied")
    if info.st_size == 0:
        raise OutboxError("empty", f"file is empty: {path}")
    if info.st_size > MAX_FILE_BYTES:
        raise OutboxError(
            "too_large", f"{path} is {info.st_size} bytes; the limit is {MAX_FILE_BYTES}"
        )
    return info.st_size


def stage(source: Path, outbox: Path) -> str:
    """Copy `source` into the outbox and return the new entry's id."""
    check_source(source)
    outbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        outbox.chmod(0o700)  # mkdir leaves the mode of a directory that exists alone
    except OSError:
        pass  # not ours to change; entries are 0600 either way
    entry_id = secrets.token_hex(16)
    temp = outbox / f".{entry_id}.tmp"
    try:
        # Written under another name first: the lodge never sees half a file.
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as target, open(source, "rb") as origin:
            shutil.copyfileobj(origin, target)
        os.replace(temp, outbox / entry_id)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return entry_id


def resolve(outbox: Path, entry_id: str) -> tuple[Path, int]:
    """The path and measured size of an entry, opened without following links."""
    if not isinstance(entry_id, str) or ENTRY_ID_RE.fullmatch(entry_id) is None:
        raise OutboxError("attachment_invalid", "invalid attachment id")
    path = outbox / entry_id
    try:
        # O_NONBLOCK: a FIFO planted here must not hang the lodge.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except (FileNotFoundError, NotADirectoryError):
        raise OutboxError(
            "attachment_not_found", "attachment is no longer in the outbox"
        ) from None
    except OSError as exc:
        # ELOOP is a symlink and ENXIO a socket. The OS text names the lodge's
        # paths, so a refusal never carries it.
        if exc.errno in (errno.ELOOP, errno.ENXIO):
            raise OutboxError(
                "attachment_invalid", "attachment is not a regular file"
            ) from None
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise OutboxError(
                "attachment_invalid", "attachment cannot be opened"
            ) from None
        raise
    try:
        info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise OutboxError("attachment_invalid", "attachment is not a regular file")
    return path, info.st_size


def discard(outbox: Path, entry_id: object) -> None:
    """Remove an entry if the id is well-formed. Never raises."""
    if not isinstance(entry_id, str) or ENTRY_ID_RE.fullmatch(entry_id) is None:
        return
    try:
        os.unlink(outbox / entry_id)
    except OSError:
        pass


def sweep(
    outbox: Path, *, now: float | None = None, max_age: float = MAX_ENTRY_AGE_SECONDS,
) -> int:
    """Remove entries and temp leftovers older than `max_age`; return how many."""
    cutoff = (time.time() if now is None else now) - max_age
    removed = 0
    try:
        entries = list(os.scandir(outbox))
    except OSError:
        return 0
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
                continue
            if info.st_mtime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue  # gone already, or not ours to remove
    return removed


def display_name(name: object) -> str:
    """A basename safe to show a human, whatever the caller sent."""
    if not isinstance(name, str):
        return "file"
    name = re.split(r"[/\\]", name)[-1]
    name = "".join(ch for ch in name if ord(ch) >= 0x20 and ord(ch) != 0x7F).strip()
    if not name.strip("."):
        return "file"
    if len(name) > _MAX_NAME_CHARS:
        dot = name.rfind(".")
        suffix = name[dot:] if 0 < dot and len(name) - dot <= _MAX_SUFFIX_CHARS else ""
        name = name[: _MAX_NAME_CHARS - len(suffix)] + suffix
    return name
