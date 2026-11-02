"""A connector's end of the lodge socket: connect, hand events in, answer the lodge.

A connector imports this and its provider's SDK, nothing else of the lodge.
The lodge starts it with WOLTSPACE_CONNECTOR_SOCKET and WOLTSPACE_CONNECTOR_TOKEN
in its environment.

    client = Client("telegram", on_say=..., on_edit=..., on_ack=..., on_send=...)
    welcome = await client.connect()       # the lodge's command menu
    status = await client.event("message", route, sender, body, event_id)

An event the lodge has not answered with a receipt is kept, and sent again
after a reconnect; the lodge drops repeats by id. So a lodge restart loses
nothing that had not been receipted. Events are kept in memory only: if this
process dies, what it held is gone.
"""

from __future__ import annotations

import asyncio
import collections
import itertools
import logging
import os
from typing import Awaitable, Callable

import connector_protocol as wire

logger = logging.getLogger(__name__)

RECEIPT_TIMEOUT = 30.0
MAX_UNRECEIPTED = 500
RECONNECT_MAX_DELAY = 30.0


class Refused(RuntimeError):
    """The lodge turned this process away (a stale token: a newer launch exists)."""


class Client:
    def __init__(
        self, name: str, *,
        socket_path: str | None = None, token: str | None = None,
        on_say: Callable[[dict], Awaitable[None]] | None = None,
        on_edit: Callable[[dict], Awaitable[None]] | None = None,
        on_ack: Callable[[dict], Awaitable[None]] | None = None,
        on_send: Callable[[dict], Awaitable[dict]] | None = None,
    ):
        self.name = name
        self.socket_path = socket_path or os.environ.get("WOLTSPACE_CONNECTOR_SOCKET", "")
        self.token = token or os.environ.get("WOLTSPACE_CONNECTOR_TOKEN", "")
        self.on_say, self.on_edit, self.on_ack, self.on_send = on_say, on_edit, on_ack, on_send
        self.welcome: dict | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._receipts: dict[str, asyncio.Future] = {}
        self._pongs: dict[str, asyncio.Future] = {}
        self._unreceipted: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self._connected = asyncio.Event()
        self._ids = itertools.count(1)
        self._tasks: set[asyncio.Task] = set()
        self._closing = False
        self._read_task: asyncio.Task | None = None

    # --- connection ------------------------------------------------------------------

    async def connect(self) -> dict:
        """Connect once and say hello; the welcome, or Refused / OSError."""
        reader, writer = await asyncio.open_unix_connection(
            self.socket_path, limit=wire.MAX_FRAME_BYTES + 1)
        writer.write(wire.encode({"type": "hello", "v": wire.VERSION,
                                  "name": self.name, "token": self.token}))
        await writer.drain()
        line = await reader.readline()
        if not line:
            raise Refused("the lodge closed the connection")
        frame = wire.decode(line, frozenset({"welcome", "bye"}))
        if frame["type"] == "bye":
            writer.close()
            raise Refused(frame.get("reason") or "refused")
        self._reader, self._writer, self.welcome = reader, writer, frame
        self._connected.set()
        self._read_task = asyncio.create_task(self._read(reader, writer))
        # What the lodge never receipted goes again, in order.
        for frame in list(self._unreceipted.values()):
            await self._write(frame)
        return frame

    async def run_forever(self) -> None:
        """Stay connected: reconnect with backoff; stop for good when refused."""
        delay = 0.5
        while not self._closing:
            if self._read_task is None or self._read_task.done():
                try:
                    await self.connect()
                    delay = 0.5
                except Refused:
                    raise
                except (OSError, wire.FrameError) as exc:
                    logger.warning("%s: lodge not reachable (%s); retrying in %.1fs",
                                   self.name, exc, delay)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, RECONNECT_MAX_DELAY)
                    continue
            try:
                await self._read_task
            except Refused:
                raise
            except Exception:
                pass

    async def close(self) -> None:
        self._closing = True
        if self._writer is not None:
            self._writer.close()
        if self._read_task is not None:
            self._read_task.cancel()
        for task in list(self._tasks):
            task.cancel()

    # --- to the lodge ------------------------------------------------------------------

    async def event(self, kind: str, route: dict, sender: str, body: dict,
                    event_id: str | None = None, timeout: float = RECEIPT_TIMEOUT) -> str:
        """Hand one event to the lodge; its receipt status, or "pending" when the
        lodge did not answer in time (the event is then sent again on reconnect)."""
        event_id = event_id or f"{self.name}-{os.getpid()}-{next(self._ids)}"
        frame = {"type": "event", "id": event_id, "kind": kind, "route": route,
                 "sender": sender, "body": body}
        wire._check_event(frame)
        while len(self._unreceipted) >= MAX_UNRECEIPTED:
            dropped, _ = self._unreceipted.popitem(last=False)
            logger.error("%s: too many unreceipted events; dropped %s", self.name, dropped)
        self._unreceipted[event_id] = frame
        future = self._receipts.get(event_id) or asyncio.get_running_loop().create_future()
        self._receipts[event_id] = future
        if self._connected.is_set():
            await self._write(frame)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except asyncio.TimeoutError:
            return "pending"

    async def ping(self, timeout: float = 10.0) -> None:
        """Round trip to the lodge: every frame it wrote before the pong is handled."""
        ping_id = f"p{next(self._ids)}"
        future = asyncio.get_running_loop().create_future()
        self._pongs[ping_id] = future
        await self._write({"type": "ping", "id": ping_id})
        await asyncio.wait_for(future, timeout)

    async def _write(self, frame: dict) -> None:
        writer = self._writer
        if writer is None:
            return
        try:
            writer.write(wire.encode(frame))
            await writer.drain()
        except (ConnectionError, OSError) as exc:
            logger.warning("%s: write to the lodge failed (%s)", self.name, exc)

    # --- from the lodge -----------------------------------------------------------------

    async def _read(self, reader, writer) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                frame = wire.decode(line, wire.FROM_LODGE - {"welcome"})
                await self._on_frame(frame)
        except (wire.FrameError, ValueError) as exc:
            logger.warning("%s: bad frame from the lodge (%s)", self.name, exc)
        finally:
            self._connected.clear()
            if self._writer is writer:
                self._writer = None
            writer.close()

    async def _on_frame(self, frame: dict) -> None:
        kind = frame["type"]
        if kind == "receipt":
            self._unreceipted.pop(frame["event"], None)
            future = self._receipts.pop(frame["event"], None)
            if future is not None and not future.done():
                future.set_result(frame["status"])
        elif kind == "pong":
            future = self._pongs.pop(frame["id"], None)
            if future is not None and not future.done():
                future.set_result(True)
        elif kind == "bye":
            if frame.get("reason") in ("refused", "replaced by a newer connection"):
                self._closing = True
                raise Refused(frame["reason"])
        elif kind == "send":
            # Uploads take time: answer from a task, keep reading.
            task = asyncio.create_task(self._answer_send(frame))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        else:
            handler = {"say": self.on_say, "edit": self.on_edit, "ack": self.on_ack}[kind]
            if handler is None:
                return
            try:
                await handler(frame)
            except Exception:
                logger.exception("%s: handling a %s frame failed", self.name, kind)

    async def _answer_send(self, frame: dict) -> None:
        if self.on_send is None:
            result = {"status": "refused", "error": f"{self.name} does not send"}
        else:
            try:
                result = await self.on_send(frame)
            except Exception as exc:
                logger.exception("%s: send failed", self.name)
                result = {"status": "failed", "error": str(exc) or type(exc).__name__}
        reply = {"type": "send-result", "id": frame["id"], "status": result.get("status", "failed")}
        for key in ("error", "message_id"):
            if result.get(key) is not None:
                reply[key] = str(result[key])[:2048]
        await self._write(reply)
