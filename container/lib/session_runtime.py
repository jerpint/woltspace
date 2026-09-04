"""Registry-backed process control for named Woltspace sessions.

Woltspace session records are the durable authority.  Tmux is an implementation
detail beneath them: new sessions persist an exact tmux session and pane handle,
and later operations use that handle rather than discovering agent processes or
guessing among panes.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Protocol

from runtime_context import RuntimeContext


_TMUX_TIMEOUT = 10
_SAFE_BUFFER = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class RuntimeHandle:
    """Exact process-runtime address stored beneath a Woltspace session ID."""

    woltspace_session_id: str
    tmux_session_name: str
    pane_id: str = ""
    kind: str = "tmux"

    def to_record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, session: dict) -> "RuntimeHandle":
        runtime = session.get("runtime") or {}
        session_id = session.get("name", "")
        return cls(
            woltspace_session_id=runtime.get("woltspace_session_id") or session_id,
            tmux_session_name=runtime.get("tmux_session_name") or session_id,
            pane_id=runtime.get("pane_id") or "",
            kind=runtime.get("kind") or "tmux",
        )


@dataclass(frozen=True)
class TmuxPane:
    session_name: str
    pane_id: str
    pane_pid: str
    active: bool


class SessionRuntime(Protocol):
    """Process-control boundary beneath Woltspace's named session registry."""

    def spawn(self, session_id: str, cwd: str, command: str) -> RuntimeHandle: ...
    def is_alive(self, handle: RuntimeHandle) -> bool: ...
    def paste(self, handle: RuntimeHandle, text: str, settle: float = 0.0) -> None: ...
    def capture(self, handle: RuntimeHandle, start: str = "-30") -> str: ...
    def stop(self, handle: RuntimeHandle) -> bool: ...
    def list_session_names(self, include_main: bool = False) -> set[str]: ...


class AmbiguousTmuxSession(RuntimeError):
    """A legacy named session has multiple panes but no persisted pane handle."""


class TmuxSessionRuntime:
    """Tmux implementation using exact handles created for named sessions."""

    def __init__(
        self,
        context: RuntimeContext | None = None,
        *,
        runner: Callable = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.context = context or RuntimeContext.from_env()
        self._run = runner
        self._sleep = sleeper

    @property
    def tmux(self) -> str:
        return self.context.tmux_bin

    def panes_for_session(self, session_name: str) -> list[TmuxPane]:
        """List every pane in one exact named session, across all its windows."""
        try:
            result = self._run(
                [
                    self.tmux,
                    "list-panes",
                    "-s",
                    "-t",
                    f"={session_name}",
                    "-F",
                    "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_active}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=_TMUX_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        return self._parse_panes(result.stdout if isinstance(result.stdout, str) else "")

    def all_panes(self) -> list[TmuxPane]:
        """List panes across the tmux server for reconciliation and cleanup."""
        try:
            result = self._run(
                [
                    self.tmux,
                    "list-panes",
                    "-a",
                    "-F",
                    "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_active}",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=_TMUX_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        return self._parse_panes(result.stdout if isinstance(result.stdout, str) else "")

    @staticmethod
    def _parse_panes(raw: str) -> list[TmuxPane]:
        panes = []
        for line in raw.splitlines():
            parts = line.split("\t")
            if len(parts) != 4 or not parts[0] or not parts[1]:
                continue
            panes.append(
                TmuxPane(
                    session_name=parts[0],
                    pane_id=parts[1],
                    pane_pid=parts[2],
                    active=parts[3] == "1",
                )
            )
        return panes

    def list_session_names(self, include_main: bool = False) -> set[str]:
        names = {pane.session_name for pane in self.all_panes()}
        if not include_main:
            names.discard("main")
        return names

    def resolve_handle(self, session_id: str) -> RuntimeHandle | None:
        """Resolve one legacy named session without guessing among panes."""
        panes = self.panes_for_session(session_id)
        if not panes:
            return None
        if len(panes) != 1:
            raise AmbiguousTmuxSession(
                f"session '{session_id}' has {len(panes)} panes and no persisted pane_id"
            )
        pane = panes[0]
        return RuntimeHandle(session_id, pane.session_name, pane.pane_id)

    def spawn(self, session_id: str, cwd: str, command: str) -> RuntimeHandle:
        """Create one named tmux session and return its exact initial pane."""
        result = self._run(
            [
                self.tmux,
                "new-session",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-s",
                session_id,
                "-c",
                cwd,
                command,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        pane_id = stdout.strip() if stdout.strip().startswith("%") else ""
        return RuntimeHandle(session_id, session_id, pane_id)

    def is_alive(self, handle: RuntimeHandle) -> bool:
        panes = self.panes_for_session(handle.tmux_session_name)
        if handle.pane_id:
            return any(pane.pane_id == handle.pane_id for pane in panes)
        return bool(panes)

    def _exact_handle(self, handle: RuntimeHandle) -> RuntimeHandle:
        if handle.pane_id:
            return handle
        # Compatibility for pre-runtime-handle records: retain the exact
        # named-session target so tmux produces the same failure/behavior as
        # before. New spawns always persist a pane_id and never use this path.
        return RuntimeHandle(
            woltspace_session_id=handle.woltspace_session_id,
            tmux_session_name=handle.tmux_session_name,
            pane_id=f"={handle.tmux_session_name}",
        )

    def paste(self, handle: RuntimeHandle, text: str, settle: float = 0.0) -> None:
        exact = self._exact_handle(handle)
        target = exact.pane_id
        buffer_id = _SAFE_BUFFER.sub("-", exact.woltspace_session_id)
        buffer_name = f"paste-{buffer_id}"
        self._run(
            [self.tmux, "send-keys", "-t", target, "-X", "cancel"],
            check=False,
            timeout=_TMUX_TIMEOUT,
        )
        self._run(
            [self.tmux, "set-buffer", "-b", buffer_name, text],
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        self._run(
            [self.tmux, "paste-buffer", "-b", buffer_name, "-d", "-t", target],
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        if settle > 0:
            self._sleep(settle)
        self._run(
            [self.tmux, "send-keys", "-t", target, "Enter"],
            check=True,
            timeout=_TMUX_TIMEOUT,
        )

    def capture(self, handle: RuntimeHandle, start: str = "-30") -> str:
        exact = self._exact_handle(handle)
        result = self._run(
            [self.tmux, "capture-pane", "-t", exact.pane_id, "-p", "-S", start],
            capture_output=True,
            text=True,
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        return result.stdout if isinstance(result.stdout, str) else ""

    def stop(self, handle: RuntimeHandle) -> bool:
        try:
            self._run(
                [self.tmux, "kill-session", "-t", f"={handle.tmux_session_name}"],
                capture_output=True,
                check=True,
                timeout=_TMUX_TIMEOUT,
            )
            return True
        except (subprocess.SubprocessError, OSError):
            return False

    def has_descendant_process(
        self,
        handle: RuntimeHandle,
        process_names: Iterable[str],
    ) -> bool | None:
        """Return whether an exact session pane has a matching process descendant."""
        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            return None
        if handle.pane_id:
            panes = [pane for pane in panes if pane.pane_id == handle.pane_id]
            if not panes:
                return None

        try:
            result = self._run(
                ["ps", "--no-headers", "-eo", "pid,ppid,comm"],
                capture_output=True,
                text=True,
                check=False,
                timeout=_TMUX_TIMEOUT,
            )
        except (subprocess.SubprocessError, OSError):
            return None

        children: dict[str, list[str]] = {}
        commands: dict[str, str] = {}
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        for line in stdout.splitlines():
            parts = line.split()
            if len(parts) >= 3:
                pid, parent, command = parts[0], parts[1], parts[2]
                children.setdefault(parent, []).append(pid)
                commands[pid] = command

        wanted = set(process_names)
        queue = [pane.pane_pid for pane in panes if pane.pane_pid]
        seen: set[str] = set()
        while queue:
            pid = queue.pop()
            if pid in seen:
                continue
            seen.add(pid)
            if commands.get(pid) in wanted:
                return True
            queue.extend(children.get(pid, []))
        return False
