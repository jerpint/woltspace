"""
Read-modify-write for the lodge's shared settings files.

woltspace.json and each wolt's wolt.json have several writers: the lodge
routes, the bots, the CLI scripts. Writing through a temporary file and a
rename keeps a reader from ever seeing half a file, but it does not stop two
writers from losing each other's change: both read, both modify, the second
rename wins. Every writer of one of these files therefore holds its lock from
the read to the rename:

    with settings_lock(path):
        cfg = json.loads(path.read_text())
        cfg["x"] = 1
        write_json_atomic(path, cfg)

The lock is an flock on a file in the system temp directory, named after the
settings file's path, so it holds across threads and the lodge's processes
and never adds a file to a wolt's folder. It is re-entrant within a thread,
so a locked writer can call another one that locks the same file. Take the lodge
(woltspace.json) lock before a wolt.json lock, never the other way round.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

_held = threading.local()


def _lock_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "woltspace-settings-locks"
    path.mkdir(mode=0o700, exist_ok=True)
    return path


@contextmanager
def settings_lock(path: Path):
    """Hold the lock for one settings file, by its resolved path."""
    key = str(Path(path).resolve())
    depth = getattr(_held, "depth", None)
    if depth is None:
        depth = _held.depth = {}
    if depth.get(key):
        depth[key] += 1
        try:
            yield
        finally:
            depth[key] -= 1
        return
    name = hashlib.sha1(key.encode()).hexdigest()[:16] + ".lock"
    with open(_lock_dir() / name, "a") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        depth[key] = 1
        try:
            yield
        finally:
            depth[key] = 0
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_text_atomic(path: Path, text: str) -> None:
    """Replace a file in one rename, through a temporary file of its own."""
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, data: object, **dumps) -> None:
    dumps.setdefault("indent", 2)
    write_text_atomic(path, json.dumps(data, **dumps) + "\n")
