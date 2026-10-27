"""File-locked rolling budgets shared by notify and the poller."""
import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path


def acquire_slot(state_dir: Path, token: str, limit: int, namespace: str,
                 *, clock=time.time, sleep=time.sleep):
    """Acquire a request reservation and return its still-held lock file."""
    digest = hashlib.sha256(token.encode()).hexdigest()[:20]
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / f"{namespace}-budget-{digest}.lock"
    data_path = state_dir / f"{namespace}-budget-{digest}.json"
    lock = lock_path.open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        while True:
            now = clock()
            try:
                times = json.loads(data_path.read_text())
                if not isinstance(times, list):
                    times = []
            except (OSError, ValueError):
                times = []
            times = [float(t) for t in times if isinstance(t, (int, float)) and now - float(t) < 60]
            if len(times) < limit:
                times.append(now)
                temp = data_path.with_suffix(".tmp")
                temp.write_text(json.dumps(times))
                os.chmod(temp, 0o600)
                os.replace(temp, data_path)
                return lock
            sleep(max(0.05, times[0] + 60 - now))
    except BaseException:
        lock.close()
        raise


def _close_acquired_lock(task: asyncio.Task) -> None:
    """Consume a detached acquisition result and release any late lock."""
    try:
        lock = task.result()
    except BaseException:
        return
    try:
        lock.close()
    except OSError:
        pass


@contextmanager
def request_slot(state_dir: Path, token: str, limit: int, namespace: str,
                 *, clock=time.time, sleep=time.sleep):
    """Hold a cross-process lock through a synchronous HTTP call."""
    lock = acquire_slot(state_dir, token, limit, namespace, clock=clock, sleep=sleep)
    try:
        yield
    finally:
        lock.close()


@asynccontextmanager
async def async_request_slot(state_dir: Path, token: str, limit: int, namespace: str):
    """Async variant; waits for the file lock off the event loop."""
    acquisition = asyncio.create_task(
        asyncio.to_thread(acquire_slot, state_dir, token, limit, namespace)
    )
    try:
        lock = await asyncio.shield(acquisition)
    except asyncio.CancelledError:
        # The worker thread cannot be cancelled. Detach cleanup from this
        # context manager so repeated cancellation cannot abandon its result.
        acquisition.add_done_callback(_close_acquired_lock)
        raise
    try:
        yield
    finally:
        lock.close()


def pace_request(state_dir: Path, token: str, limit: int, namespace: str,
                 *, clock=time.time, sleep=time.sleep) -> None:
    """Reserve a short request slot; used before long-poll network calls."""
    with request_slot(state_dir, token, limit, namespace, clock=clock, sleep=sleep):
        pass


def pace_send(state_dir: Path, token: str, **kwargs) -> None:
    """Reserve one send under WhatsApp's 12 requests / rolling 60 seconds."""
    pace_request(state_dir, token, 12, "send", **kwargs)
