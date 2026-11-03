"""Owner-only livereload for wolt sites, on the site's own gateway host.

One watcher per site polls the site tree's mtimes (no inotify handles, a
capped file count) and hands every subscriber the same event:

    {"type": "reload", "revision": n}

That is the whole protocol: no filenames, no other site's activity, nothing
from the browser is read. Subscribers are bounded per site and overall.

A socket closes when the site goes away, when the gateway shuts down, when the
owner's identity no longer checks out, and at the access token's expiry: the
gateway re-runs its admission check every RECHECK_SECONDS with the credentials
the socket was opened with, so an Access session that has ended cannot keep a
reload stream alive just because it was authorized once. The recheck runs on a
monotonic deadline that reload traffic does not move, and before any event is
sent once that deadline has passed, so a stream of edits cannot postpone it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Awaitable, Callable

from fastapi import WebSocket

POLL_SECONDS = 0.5
MAX_FILES = 2000
MAX_SUBSCRIBERS_PER_SITE = 8
MAX_SUBSCRIBERS_TOTAL = 64
RECHECK_SECONDS = 60.0
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv"}

# Queue sentinel: the site is gone or the gateway is stopping.
_CLOSE = None


def snapshot_tree(root: str) -> dict[str, tuple[int, int]] | None:
    """mtime and size of every entry under root (symlinks not followed), or None when root is gone."""
    if not os.path.isdir(root):
        return None
    seen: dict[str, tuple[int, int]] = {}
    pending = [root]
    while pending and len(seen) < MAX_FILES:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if len(seen) >= MAX_FILES:
                        break
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    seen[entry.path] = (st.st_mtime_ns, st.st_size)
                    if entry.is_dir(follow_symlinks=False) and entry.name not in _SKIP_DIRS:
                        pending.append(entry.path)
        except OSError:
            continue
    return seen


class SiteWatch:
    def __init__(self, root: str):
        self.root = root
        self.revision = 0
        self.subscribers: set[asyncio.Queue] = set()
        self.task: asyncio.Task | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def broadcast(self, message) -> None:
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Reloads already wait for this subscriber; one more says
                # nothing new. A close must still get through.
                if message is _CLOSE:
                    queue.get_nowait()
                    queue.put_nowait(message)

    async def run(self) -> None:
        snapshot = await asyncio.to_thread(snapshot_tree, self.root)
        while self.subscribers:
            await asyncio.sleep(POLL_SECONDS)
            current = await asyncio.to_thread(snapshot_tree, self.root)
            if current is None:
                self.broadcast(_CLOSE)
                return
            if current != snapshot:
                snapshot = current
                self.revision += 1
                self.broadcast({"type": "reload", "revision": self.revision})


class LivereloadHub:
    def __init__(self):
        self.watches: dict[str, SiteWatch] = {}

    def total(self) -> int:
        return sum(len(watch.subscribers) for watch in self.watches.values())

    def subscribe(self, root: str) -> asyncio.Queue | None:
        loop = asyncio.get_running_loop()
        watch = self.watches.get(root)
        if watch is not None and (watch.loop is not loop or (watch.task and watch.task.done())):
            # Left over from a stopped loop (tests) or a finished watcher.
            watch = None
        if ((watch is not None and len(watch.subscribers) >= MAX_SUBSCRIBERS_PER_SITE)
                or self.total() >= MAX_SUBSCRIBERS_TOTAL):
            return None
        if watch is None:
            watch = self.watches[root] = SiteWatch(root)
        queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        watch.subscribers.add(queue)
        if watch.task is None or watch.task.done():
            watch.loop = loop
            watch.task = loop.create_task(watch.run())
        return queue

    def unsubscribe(self, root: str, queue: asyncio.Queue) -> None:
        watch = self.watches.get(root)
        if watch is None:
            return
        watch.subscribers.discard(queue)
        if not watch.subscribers:
            if watch.task is not None and not watch.task.done():
                watch.task.cancel()
            self.watches.pop(root, None)

    async def shutdown(self) -> None:
        for watch in list(self.watches.values()):
            watch.broadcast(_CLOSE)
            if watch.task is not None and not watch.task.done():
                watch.task.cancel()
                await asyncio.gather(watch.task, return_exceptions=True)
        self.watches.clear()


hub = LivereloadHub()


async def serve_site_livereload(
    ws: WebSocket, root: str, *,
    still_authorized: Callable[[], Awaitable[bool]],
    expires_at: float | None = None,
) -> None:
    """Accept the owner's socket and feed it reload events until it should end."""
    queue = hub.subscribe(root)
    if queue is None:
        await ws.close(code=1013)  # try again later: this site is at its limit
        return
    await ws.accept()

    async def await_disconnect():
        # Only a disconnect ends this; anything the browser sends is ignored.
        while True:
            message = await ws.receive()
            if message.get("type") == "websocket.disconnect":
                return

    disconnect = asyncio.create_task(await_disconnect())
    event = asyncio.create_task(queue.get())
    code = 1000
    next_check = time.monotonic() + RECHECK_SECONDS

    async def revalidate() -> bool:
        # The deadline moves only after a real check, never on traffic.
        nonlocal next_check
        if expires_at is not None and time.time() >= expires_at:
            return False
        if not await still_authorized():
            return False
        next_check = time.monotonic() + RECHECK_SECONDS
        return True

    try:
        while True:
            timeout = max(0.0, next_check - time.monotonic())
            if expires_at is not None:
                timeout = max(0.0, min(timeout, expires_at - time.time()))
            done, _pending = await asyncio.wait(
                {disconnect, event}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect in done:
                return
            due = time.monotonic() >= next_check or (
                expires_at is not None and time.time() >= expires_at
            )
            if due and not await revalidate():
                code = 1008
                break
            if event in done:
                message = event.result()
                if message is _CLOSE:
                    code = 1001
                    break
                await ws.send_text(json.dumps(message, separators=(",", ":")))
                event = asyncio.create_task(queue.get())
        try:
            await ws.close(code=code)
        except Exception:
            pass
    finally:
        hub.unsubscribe(root, queue)
        for task in (disconnect, event):
            task.cancel()
        # Retrieves both results so nothing goes unread; the visitor leaving
        # or a shutdown may cancel this handler mid-cleanup, which is fine.
        with contextlib.suppress(BaseException):
            await asyncio.gather(disconnect, event, return_exceptions=True)
