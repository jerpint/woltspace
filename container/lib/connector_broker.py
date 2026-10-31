"""The lodge's end of the connector socket (RFC #617).

One unix socket per connector, in a 0700 directory the lodge owns. The
launcher writes a fresh token for each launch (`<name>.token`, 0600) and hands
it to the connector in its environment; `hello` must carry the current one,
so a connector left over from an earlier launch is turned away.

Each event is checked, its dedup key committed, a receipt sent, and only then
dispatched, once. Dispatch runs on its own thread and event loop: the lodge's
handlers start sessions and read the disk, and none of that may stall the web
server this broker shares a process with. Events are dispatched in order, one
connector at a time, from a bounded queue: a connector that floods is told
`failed`. Every connector shares this one dispatch loop, so a handler must not
block it: slow service calls (the dog, transcription) run in worker threads.

A receipt means "accepted for one dispatch attempt". If the lodge dies after
the receipt and before the dispatch, the event is lost, and a resend is a
duplicate: at-most-once past this point, by design.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import connector_protocol as wire
from connector_contract import Ack, Reply

logger = logging.getLogger(__name__)

HELLO_TIMEOUT = 10.0
QUEUE_LIMIT = 256
DEFAULT_SEND_TIMEOUT = 330.0  # an upload may take minutes; the sender times out first


class ConnectorUnavailable(RuntimeError):
    """The connector is not connected to the lodge right now."""


class SendFailed(RuntimeError):
    def __init__(self, status: str, error: str):
        super().__init__(error or status)
        self.status = status


def token_path(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / f"{name}.token"


def socket_path(run_dir: Path, name: str) -> Path:
    return Path(run_dir) / f"{name}.sock"


def prepare_run_dir(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    run_dir.chmod(0o700)
    return run_dir


def new_token(run_dir: Path, name: str) -> str:
    """A fresh token for one launch, readable by this OS user only."""
    prepare_run_dir(run_dir)
    token = secrets.token_hex(32)
    path = token_path(run_dir, name)
    tmp = path.with_suffix(".token.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)
    os.replace(tmp, path)
    return token


def _current_token(run_dir: Path, name: str) -> str:
    try:
        return token_path(run_dir, name).read_text().strip()
    except OSError:
        return ""


class Outbox:
    """What a handler may do in answer to one event; nothing else reaches the connector."""

    def __init__(self, broker: "Broker", name: str, event: dict):
        self._broker, self._name, self._event = broker, name, event

    @property
    def route(self) -> dict:
        return dict(self._event["route"])

    async def say(self, reply: Reply, photo: str | None = None) -> None:
        frame = {"type": "say", "event": self._event["id"], "route": self.route,
                 "reply": wire.reply_to_wire(reply)}
        if photo:
            frame["photo"] = str(photo)
        await self._broker._from_dispatch(self._name, frame)

    async def edit(self, reply: Reply) -> None:
        await self._broker._from_dispatch(self._name, {
            "type": "edit", "event": self._event["id"], "reply": wire.reply_to_wire(reply)})

    async def ack(self, ack: Ack) -> None:
        await self._broker._from_dispatch(self._name, {
            "type": "ack", "event": self._event["id"], "ack": wire.ack_to_wire(ack)})


# handle(event, outbox) -> None. Runs on the dispatch thread.
Handler = Callable[[dict, Outbox], Awaitable[None]]


@dataclass
class _Slot:
    name: str
    handler: Handler
    commands: list = field(default_factory=list)
    server: asyncio.AbstractServer | None = None
    writer: asyncio.StreamWriter | None = None
    pending: dict = field(default_factory=dict)  # send id -> Future
    queued: int = 0
    queue: asyncio.Queue | None = None  # lives on the dispatch loop


class Broker:
    """Serves each registered connector on its socket. Start and stop on one loop."""

    def __init__(self, run_dir: Path, dedup: Callable[[str, str], bool]):
        # dedup(connector, event_id) -> True the first time, False for a repeat.
        self.run_dir = Path(run_dir)
        self._dedup = dedup
        self._slots: dict[str, _Slot] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dispatch_loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    # --- lifecycle --------------------------------------------------------------

    def add(self, name: str, handler: Handler, commands: list | None = None) -> None:
        self._slots[name] = _Slot(name, handler, list(commands or []))

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        prepare_run_dir(self.run_dir)
        self._start_dispatch()
        for slot in self._slots.values():
            path = socket_path(self.run_dir, slot.name)
            path.unlink(missing_ok=True)
            slot.server = await asyncio.start_unix_server(
                lambda r, w, s=slot: self._serve(s, r, w), path=str(path),
                limit=wire.MAX_FRAME_BYTES + 1)
            path.chmod(0o600)
            fut = asyncio.run_coroutine_threadsafe(self._start_worker(slot), self._dispatch_loop)
            await asyncio.wrap_future(fut)

    async def stop(self) -> None:
        for slot in self._slots.values():
            if slot.writer is not None:
                await self._close(slot, slot.writer, "lodge stopping")
            if slot.server is not None:
                slot.server.close()
                await slot.server.wait_closed()
                socket_path(self.run_dir, slot.name).unlink(missing_ok=True)
            for future in slot.pending.values():
                if not future.done():
                    future.set_exception(ConnectorUnavailable(f"{slot.name} disconnected"))
        if self._dispatch_loop is not None:
            async def cancel_workers():
                tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(cancel_workers(), self._dispatch_loop))
            self._dispatch_loop.call_soon_threadsafe(self._dispatch_loop.stop)
            self._thread.join(timeout=5)
            self._dispatch_loop = None

    def connected(self, name: str) -> bool:
        slot = self._slots.get(name)
        return bool(slot and slot.writer is not None)

    # --- dispatch thread -----------------------------------------------------------

    def _start_dispatch(self) -> None:
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._dispatch_loop = loop
            ready.set()
            loop.run_forever()
            loop.close()

        self._thread = threading.Thread(target=run, name="connector-dispatch", daemon=True)
        self._thread.start()
        ready.wait()

    async def _start_worker(self, slot: _Slot) -> None:
        slot.queue = asyncio.Queue()
        asyncio.get_running_loop().create_task(self._worker(slot))

    async def _worker(self, slot: _Slot) -> None:
        while True:
            event = await slot.queue.get()
            try:
                await slot.handler(event, Outbox(self, slot.name, event))
            except Exception:
                logger.exception("%s: dispatch of event %s failed", slot.name, event.get("id"))
            finally:
                with self._lock:
                    slot.queued -= 1
                slot.queue.task_done()

    async def drain(self, name: str) -> None:
        """Wait until every accepted event of `name` is dispatched (for tests)."""
        slot = self._slots[name]

        async def joined():
            await slot.queue.join()
        await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(joined(), self._dispatch_loop))

    async def _from_dispatch(self, name: str, frame: dict) -> None:
        """Write a frame from the dispatch thread, on the lodge loop."""
        fut = asyncio.run_coroutine_threadsafe(self._write(name, frame), self._loop)
        await asyncio.wrap_future(fut)

    # --- connections --------------------------------------------------------------

    async def _write(self, name: str, frame: dict) -> bool:
        slot = self._slots[name]
        writer = slot.writer
        if writer is None:
            logger.warning("%s: not connected; dropped a %s frame", name, frame.get("type"))
            return False
        try:
            writer.write(wire.encode(frame))
            await writer.drain()
            return True
        except (ConnectionError, OSError) as exc:
            logger.warning("%s: write failed (%s)", name, exc)
            return False

    async def _close(self, slot: _Slot, writer: asyncio.StreamWriter, reason: str) -> None:
        try:
            writer.write(wire.encode({"type": "bye", "reason": reason}))
            await writer.drain()
        except Exception:
            pass
        writer.close()
        if slot.writer is writer:
            slot.writer = None

    async def _serve(self, slot: _Slot, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            hello = wire.decode(line, frozenset({"hello"}))
            expected = _current_token(self.run_dir, slot.name)
            if hello["name"] != slot.name or not expected or not secrets.compare_digest(
                    hello["token"], expected):
                raise wire.FrameError("hello refused: wrong name or stale token")
        except (asyncio.TimeoutError, wire.FrameError, ValueError, ConnectionError) as exc:
            logger.warning("%s: connection refused (%s)", slot.name, exc)
            await self._close(slot, writer, "refused")
            return

        if slot.writer is not None:
            # One live connection per connector: the newest launch wins.
            await self._close(slot, slot.writer, "replaced by a newer connection")
        slot.writer = writer
        writer.write(wire.encode({"type": "welcome", "v": wire.VERSION,
                                  "commands": [list(c) for c in slot.commands]}))
        await writer.drain()
        logger.info("%s: connected", slot.name)

        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise wire.FrameError("frame too large or truncated")
                frame = wire.decode(line, wire.FROM_CONNECTOR - {"hello"})
                await self._on_frame(slot, frame)
        except (wire.FrameError, ValueError) as exc:
            logger.warning("%s: bad frame, closing (%s)", slot.name, exc)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if slot.writer is writer:
                await self._close(slot, writer, "closed")
                for future in slot.pending.values():
                    if not future.done():
                        future.set_exception(SendFailed("ambiguous", f"{slot.name} disconnected"))
                slot.pending.clear()

    async def _on_frame(self, slot: _Slot, frame: dict) -> None:
        kind = frame["type"]
        if kind == "ping":
            await self._write(slot.name, {"type": "pong", "id": frame["id"]})
        elif kind == "send-result":
            future = slot.pending.pop(frame["id"], None)
            if future is not None and not future.done():
                future.set_result(frame)
        elif kind == "event":
            status = await self._admit(slot, frame)
            await self._write(slot.name, {"type": "receipt", "event": frame["id"], "status": status})
            if status == "accepted":
                self._dispatch_loop.call_soon_threadsafe(slot.queue.put_nowait, frame)

    async def _admit(self, slot: _Slot, event: dict) -> str:
        with self._lock:
            if slot.queued >= QUEUE_LIMIT:
                logger.warning("%s: queue full, event %s refused", slot.name, event["id"])
                return "failed"
            slot.queued += 1
        try:
            fresh = await asyncio.to_thread(self._dedup, slot.name, event["id"])
        except Exception:
            logger.exception("%s: could not record event %s", slot.name, event["id"])
            fresh = None
        if fresh:
            return "accepted"
        with self._lock:
            slot.queued -= 1
        return "duplicate" if fresh is False else "failed"

    # --- outbound -------------------------------------------------------------------

    async def send(self, name: str, route: dict, method: str, text: str = "", *,
                   path: str | None = None, filename: str | None = None,
                   content_type: str | None = None,
                   timeout: float = DEFAULT_SEND_TIMEOUT) -> dict:
        """Ask the connector to send; its send-result, or SendFailed / ConnectorUnavailable.

        From any event loop: the work runs on the lodge loop (the one `start` ran on).
        """
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                self.send(name, route, method, text, path=path, filename=filename,
                          content_type=content_type, timeout=timeout), self._loop))
        slot = self._slots.get(name)
        if slot is None or slot.writer is None:
            raise ConnectorUnavailable(f"{name} is not connected to the lodge")
        send_id = f"s{next(self._ids)}"
        frame = {"type": "send", "id": send_id, "route": dict(route), "method": method, "text": text}
        if method != "message":
            frame.update(path=str(path), filename=filename, content_type=content_type)
        future = asyncio.get_running_loop().create_future()
        slot.pending[send_id] = future
        if not await self._write(name, frame):
            slot.pending.pop(send_id, None)
            raise ConnectorUnavailable(f"{name} is not connected to the lodge")
        try:
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            slot.pending.pop(send_id, None)
            raise SendFailed("ambiguous", f"{name} did not answer in {timeout:.0f}s") from None
        if result["status"] != "delivered":
            raise SendFailed(result["status"], result.get("error") or result["status"])
        return result


# --- the one broker of this lodge process ------------------------------------------

_BROKER: Broker | None = None


def current() -> Broker | None:
    return _BROKER


def install(broker: Broker | None) -> None:
    global _BROKER
    _BROKER = broker
