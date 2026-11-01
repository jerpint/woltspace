"""
One lifecycle lock per agent, and the launch marker it protects.

Every start, resume relaunch, stop, archive, delete and wake of an agent's
sessions runs under agent_lock(wolts_dir, wolt): an exclusive flock on
.space/run/lifecycle/<wolt>.lock. It is reentrant per thread (archive calls
stop), its fd is close-on-exec (a spawned child never holds it), and a wait
past `wait` seconds raises LifecycleBusy rather than running unlocked. Hold no
SQL across an external effect, and release before any readiness wait.

Before each external launch the session record carries `launching` (the
attempt id). Only publish_runtime clears it, in the same write as the
confirmed runtime. A record still carrying it is an unresolved launch: nothing
here proves it ended (not a missing tmux name, elapsed time, a lock release or
a rearm), so it is reported UNKNOWN and nothing settles or relaunches it.
"""

from __future__ import annotations

import fcntl
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

_HELD = threading.local()


class LifecycleBusy(Exception):
    """Another lifecycle operation holds this agent's lock."""
    code = "lifecycle_busy"


class LifecycleUnknown(Exception):
    """An earlier launch of this session is unresolved: it may still start."""
    code = "launch_unknown"


def new_attempt() -> str:
    return uuid.uuid4().hex[:12]


@contextmanager
def agent_lock(wolts_dir, wolt: str, *, wait: float = 20.0):
    """Hold the agent's lifecycle lock. Sessions without a wolt take none."""
    if not wolt or not (Path(wolts_dir) / ".space").is_dir():
        yield  # no wolt, or a data root with no lodge yet: nothing to serialize
        return
    path = Path(wolts_dir) / ".space" / "run" / "lifecycle" / f"{wolt}.lock"
    held = _HELD.__dict__.setdefault("counts", {})
    key = str(path)
    if held.get(key):
        held[key] += 1
        try:
            yield
        finally:
            held[key] -= 1
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise LifecycleBusy(wolt) from None
                time.sleep(0.05)
        held[key] = 1
        try:
            yield
        finally:
            held[key] = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
