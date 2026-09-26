"""Attach browser WebSockets to tmux without a Node sidecar.

The browser terminal only needs a local pseudo-terminal around ``tmux attach``.
Keeping that tiny bridge in the Python control plane means native Woltspace no
longer needs Node, npm, ``node-pty`` or the separately distributed terminal
cockpit merely to render its browser session pane.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import fcntl
import os
import pty
import re
import shutil
import struct
import termios
from pathlib import Path


SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
MIN_COLS = 20
MIN_ROWS = 5


class PtyBridgeError(RuntimeError):
    """The requested terminal cannot be attached safely."""


class PtyTextDecoder:
    """Preserve UTF-8 characters that cross arbitrary PTY read boundaries."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def feed(self, data: bytes, *, final: bool = False) -> str:
        return self._decoder.decode(data, final=final)


async def _tmux(*args: str, cwd: Path) -> int:
    tmux = shutil.which("tmux")
    if not tmux:
        raise PtyBridgeError("tmux is not installed")
    child_env = dict(os.environ)
    child_env.pop("TMUX", None)
    process = await asyncio.create_subprocess_exec(
        tmux,
        *args,
        cwd=cwd,
        env=child_env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait()


async def _ensure_session(name: str, cwd: Path) -> None:
    if SESSION_NAME.fullmatch(name) is None:
        raise PtyBridgeError("invalid session name")
    if await _tmux("has-session", "-t", name, cwd=cwd) == 0:
        return
    if name != "main":
        raise PtyBridgeError(f"tmux session does not exist: {name}")
    created = await _tmux("new-session", "-d", "-s", name, "-c", str(cwd), cwd=cwd)
    if created != 0 and await _tmux("has-session", "-t", name, cwd=cwd) != 0:
        raise PtyBridgeError("could not create the main tmux session")


class TmuxAttachment:
    """One disposable PTY client attached to a durable tmux session."""

    def __init__(self, master_fd: int, process: asyncio.subprocess.Process):
        self.master_fd = master_fd
        self.process = process
        self._loop = asyncio.get_running_loop()
        self._readable = asyncio.Event()
        self._closed = False
        self._loop.add_reader(self.master_fd, self._readable.set)

    async def read(self, size: int = 65536) -> bytes:
        while not self._closed:
            await self._readable.wait()
            self._readable.clear()
            try:
                return os.read(self.master_fd, size)
            except BlockingIOError:
                continue
            except OSError:
                return b""
        return b""

    async def write(self, data: str) -> None:
        remaining = memoryview(data.encode())
        while remaining and not self._closed:
            try:
                remaining = remaining[os.write(self.master_fd, remaining):]
            except BlockingIOError:
                writable = self._loop.create_future()
                def mark_writable() -> None:
                    if not writable.done():
                        writable.set_result(None)

                self._loop.add_writer(self.master_fd, mark_writable)
                try:
                    await writable
                finally:
                    self._loop.remove_writer(self.master_fd)

    def resize(self, cols: int, rows: int) -> None:
        if self._closed or cols < MIN_COLS or rows < MIN_ROWS:
            return
        dimensions = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, dimensions)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._loop.remove_reader(self.master_fd)
        with contextlib.suppress(OSError):
            os.close(self.master_fd)
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=1)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()


async def attach_tmux(name: str, cwd: Path) -> TmuxAttachment:
    """Open a PTY onto ``name`` without creating non-main sessions."""

    cwd = cwd.resolve()
    await _ensure_session(name, cwd)
    tmux = shutil.which("tmux")
    if not tmux:  # `_ensure_session` already names this; keep type narrowing honest.
        raise PtyBridgeError("tmux is not installed")

    master_fd, slave_fd = pty.openpty()
    os.set_blocking(master_fd, False)
    child_env = {**os.environ, "TERM": "xterm-256color"}
    # The control plane can itself be started from a tmux-hosted wolt. A child
    # `tmux attach` must not inherit that client's socket marker or tmux refuses
    # the nested attach before the browser receives its first frame.
    child_env.pop("TMUX", None)
    try:
        process = await asyncio.create_subprocess_exec(
            tmux,
            "attach-session",
            "-t",
            name,
            cwd=cwd,
            env=child_env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
        )
    except Exception:
        os.close(master_fd)
        raise
    finally:
        os.close(slave_fd)
    return TmuxAttachment(master_fd, process)
