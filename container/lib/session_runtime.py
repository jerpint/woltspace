"""Registry-backed process control for named Woltspace sessions.

Woltspace session records are the durable authority.  Tmux is an implementation
detail beneath them: new sessions persist an exact tmux session and pane handle,
and later operations use that handle rather than discovering agent processes or
guessing among panes.

Two questions are asked of a session, and they are deliberately different:

* **Is it alive?** — a session-level question.  A record is live while its tmux
  session exists at all, which is what `list()`, `reconcile()` and the stop
  paths have always meant by it.  Answering this pane-strictly would strand a
  session whose persisted pane went away: too dead to message, too alive to
  reap.
* **Where do I deliver?** — a pane-level question, and the reason handles are
  persisted at all.  `resolve_delivery_pane` answers it, and agent detection
  walks the same pane set, so a session is never found in one pane and pasted
  into another.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Protocol, runtime_checkable

from runtime_context import RuntimeContext


_TMUX_TIMEOUT = 10
_SAFE_BUFFER = re.compile(r"[^A-Za-z0-9_-]")
_SESSION_ENV_KEYS = (
    "HOME",
    "PATH",
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR",
    "OPENCODE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "WOLTS_DIR",
    "WOLTSPACE_DIR",
    "WOLTSPACE_ISOLATION",
    "WOLTSPACE_TMUX_BIN",
    "WOLTSPACE_PS_BIN",
)


@dataclass(frozen=True)
class RuntimeHandle:
    """Exact process-runtime address stored beneath a Woltspace session ID."""

    woltspace_session_id: str
    tmux_session_name: str
    pane_id: str = ""
    kind: str = "tmux"

    def to_record(self) -> dict:
        return asdict(self)

    def at_pane(self, pane_id: str) -> "RuntimeHandle":
        """Same session, addressed at a specific pane."""
        return RuntimeHandle(
            woltspace_session_id=self.woltspace_session_id,
            tmux_session_name=self.tmux_session_name,
            pane_id=pane_id,
            kind=self.kind,
        )

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


@runtime_checkable
class SessionRuntime(Protocol):
    """Process-control boundary beneath Woltspace's named session registry."""

    def spawn(self, session_id: str, cwd: str, command: str) -> RuntimeHandle: ...
    def spawn_in_session(
        self, handle: RuntimeHandle, cwd: str, command: str
    ) -> RuntimeHandle: ...
    def is_alive(self, handle: RuntimeHandle) -> bool: ...
    def handle_is_alive(self, handle: RuntimeHandle) -> bool: ...
    def paste(self, handle: RuntimeHandle, text: str, settle: float = 0.0) -> None: ...
    def capture(self, handle: RuntimeHandle, start: str | None = "-30") -> str: ...
    def stop(self, handle: RuntimeHandle) -> bool: ...
    def list_session_names(self, include_main: bool = False) -> set[str]: ...
    def has_descendant_process(
        self, handle: RuntimeHandle, process_names: Iterable[str]
    ) -> bool | None: ...
    def resolve_process_handle(
        self, handle: RuntimeHandle, process_names: Iterable[str]
    ) -> RuntimeHandle | None: ...


class TmuxSessionRuntime:
    """Tmux implementation using exact handles created for named sessions."""

    def __init__(
        self,
        context: RuntimeContext | None = None,
        *,
        runner: Callable | None = None,
        sleeper: Callable[[float], None] | None = None,
    ):
        self.context = context or RuntimeContext.from_env()
        self._runner = runner
        self._sleeper = sleeper

    # Late-bound on purpose: with no explicit runner injected, every call looks
    # subprocess.run up fresh, so a test that patches the subprocess module is
    # still honored by a runtime built before the patch.
    @property
    def _run(self) -> Callable:
        return self._runner if self._runner is not None else subprocess.run

    @property
    def _sleep(self) -> Callable[[float], None]:
        return self._sleeper if self._sleeper is not None else time.sleep

    @property
    def tmux(self) -> str:
        return self.context.tmux_bin

    # -- pane inventory ----------------------------------------------------

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

    # -- liveness ----------------------------------------------------------

    def is_alive(self, handle: RuntimeHandle) -> bool:
        """Whether the session exists at all — the one liveness definition.

        Deliberately session-level, not pane-level.  `list()`, `reconcile()`
        and the stop paths have always meant "tmux still has this session",
        and a stale persisted pane must not make a running session look dead:
        the stop paths would then skip the kill and leave it running forever,
        while the vulture would refuse to reap it.  Pane identity governs
        delivery (`resolve_delivery_pane`), not existence.
        """
        return bool(self.panes_for_session(handle.tmux_session_name))

    def handle_is_alive(self, handle: RuntimeHandle) -> bool:
        """Whether this handle's exact persisted execution surface exists."""
        if not handle.pane_id:
            return False
        return any(
            pane.pane_id == handle.pane_id
            for pane in self.panes_for_session(handle.tmux_session_name)
        )

    # -- pane resolution ---------------------------------------------------

    def resolve_delivery_pane(
        self,
        handle: RuntimeHandle,
        process_names: Iterable[str] | None = None,
    ) -> RuntimeHandle:
        """Resolve the exact pane to talk to for this session.

        Records written before runtime handles carry no pane_id — that is
        every session already on disk — and a persisted pane can also go away
        beneath a live session.  Both fall back to inspecting the session's
        panes rather than addressing it by bare name, because tmux resolves a
        bare name to the *active* pane of the *current* window: with the human
        looking at a second window, a paste meant for the agent lands in
        whatever they last clicked on.

        A handle that already names a pane is used as-is.  Callers get one
        either from the record written at spawn or from `resolve_agent_handle`,
        which just walked the panes to find the agent; re-verifying it here
        would cost a second tmux call and could only lose information.  If such
        a pane has since died, tmux says so loudly on the paste — far better
        than silently retargeting the message at some other pane.

        With no pane to go on: the only pane when there is just one, else the
        pane whose process tree actually carries the agent — the same walk
        `has_descendant_process` uses, so detection and delivery can never
        disagree.  Failing that, the bare session name, and tmux picks as it
        always did.
        """
        if handle.pane_id:
            return handle

        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            # Nothing to resolve. Address the bare name so a dead session
            # fails exactly as it did pre-refactor.
            return self._bare(handle)
        if len(panes) == 1:
            return handle.at_pane(panes[0].pane_id)
        if process_names:
            matched = self._panes_running(panes, process_names)
            if matched:
                return handle.at_pane(matched[0].pane_id)
        return self._bare(handle)

    @staticmethod
    def _bare(handle: RuntimeHandle) -> RuntimeHandle:
        """Address the session by bare name — tmux's own active-pane pick.

        The '=' exact-match prefix is deliberately absent: tmux honors it for a
        target-session (has-session, kill-session) but rejects it for a
        target-pane (paste-buffer, capture-pane, send-keys) with
        "can't find pane: =<name>".
        """
        return handle.at_pane(handle.tmux_session_name)

    # -- process control ---------------------------------------------------

    @staticmethod
    def _launch_command(command: str) -> str:
        """Carry path/auth locations across a pre-existing tmux server.

        Tmux panes inherit the server environment, which may predate the
        current Woltspace install or data root. Prefix only non-secret path and
        runtime variables; harness tokens are intentionally never embedded in
        a visible tmux start command.
        """
        values = [
            shlex.quote(f"{key}={os.environ[key]}")
            for key in _SESSION_ENV_KEYS
            if key in os.environ
        ]
        return f"env {' '.join(values)} {command}" if values else command

    def spawn(self, session_id: str, cwd: str, command: str) -> RuntimeHandle:
        """Create one named tmux session and return its exact initial pane.

        `new-session -d` returns as soon as tmux has forked, so the timeout is
        a safety net against a wedged tmux server rather than a normal outcome.
        If it does fire the caller sees an exception while tmux may already
        have the session — the registry record is then "running" with no
        handle, which reconcile()/the vulture resolve on their next pass.
        """
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
                self._launch_command(command),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        pane_id = stdout.strip() if stdout.strip().startswith("%") else ""
        return RuntimeHandle(session_id, session_id, pane_id)

    def spawn_in_session(
        self,
        handle: RuntimeHandle,
        cwd: str,
        command: str,
    ) -> RuntimeHandle:
        """Create a dedicated window in an existing session.

        Recovery must never paste a restart command into an arbitrary surviving
        user pane. A detached new window preserves the existing layout while
        giving the agent a fresh execution surface with an exact returned ID.
        """
        result = self._run(
            [
                self.tmux,
                "new-window",
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-t",
                f"={handle.tmux_session_name}",
                "-c",
                cwd,
                self._launch_command(command),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=_TMUX_TIMEOUT,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        pane_id = stdout.strip() if stdout.strip().startswith("%") else ""
        return handle.at_pane(pane_id)

    def paste(
        self,
        handle: RuntimeHandle,
        text: str,
        settle: float = 0.0,
        *,
        process_names: Iterable[str] | None = None,
    ) -> None:
        exact = self.resolve_delivery_pane(handle, process_names)
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

    def capture(self, handle: RuntimeHandle, start: str | None = "-30") -> str:
        """Capture a pane's contents.

        start is the -S history offset; pass None for the visible pane only.
        That distinction matters: callers watching for a marker to *clear*
        (deliver_boot_prompt's repaint gate) must not see scrollback, or the
        marker never disappears and the gate never opens.
        """
        exact = self.resolve_delivery_pane(handle)
        command = [self.tmux, "capture-pane", "-t", exact.pane_id, "-p"]
        if start is not None:
            command += ["-S", start]
        result = self._run(
            command,
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

    # -- process inspection ------------------------------------------------

    def _process_table(self) -> tuple[dict[str, list[str]], dict[str, str]] | None:
        """One ps snapshot as (pid → child pids, pid → comm)."""
        try:
            result = self._run(
                # The field-name '=' form suppresses headers on both GNU ps
                # (Linux/container) and BSD ps (macOS/native).
                [self.context.ps_bin, "-axo", "pid=,ppid=,comm="],
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
        return children, commands

    def _panes_running(
        self,
        panes: list[TmuxPane],
        process_names: Iterable[str],
        table: tuple[dict[str, list[str]], dict[str, str]] | None = None,
    ) -> list[TmuxPane]:
        """Subset of panes whose process tree contains one of process_names."""
        table = table or self._process_table()
        if table is None:
            return []
        children, commands = table
        wanted = set(process_names)

        matched = []
        for pane in panes:
            if not pane.pane_pid:
                continue
            queue = [pane.pane_pid]
            seen: set[str] = set()
            while queue:
                pid = queue.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                if commands.get(pid) in wanted:
                    matched.append(pane)
                    break
                queue.extend(children.get(pid, []))
        return matched

    def _matching_panes_for_handle(
        self,
        handle: RuntimeHandle,
        process_names: Iterable[str],
    ) -> list[TmuxPane] | None:
        """Panes of this session whose process tree carries a wanted process.

        None means "can't tell" — the session is gone, or ps was unreadable.
        [] means the session exists and no pane carries one.

        Scoped to the persisted pane while that pane lives.  Once it has
        vanished the whole session is searched instead: a missing pane is not
        evidence the agent left the session, and searching wide keeps this in
        step with `resolve_delivery_pane`.
        """
        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            return None
        if handle.pane_id:
            scoped = [pane for pane in panes if pane.pane_id == handle.pane_id]
            if scoped:
                panes = scoped
        table = self._process_table()
        if table is None:
            return None
        return self._panes_running(panes, process_names, table)

    def resolve_process_handle(
        self,
        handle: RuntimeHandle,
        process_names: Iterable[str],
    ) -> RuntimeHandle | None:
        """Return a handle addressed where a wanted process was found.

        The contract returns the runtime-neutral handle callers need rather
        than exposing tmux's pane inventory outside this driver.  None covers
        both "not found" and "undetermined"; callers that must distinguish
        uncertainty use `has_descendant_process` instead.
        """
        matched = self._matching_panes_for_handle(handle, process_names)
        if not matched:
            return None
        return handle.at_pane(matched[0].pane_id)

    def has_descendant_process(
        self,
        handle: RuntimeHandle,
        process_names: Iterable[str],
    ) -> bool | None:
        """Whether any pane of this session carries a wanted process.

        None means undetermined (session gone, or ps unreadable) — callers
        treat that as "don't act", never as "dead".
        """
        matched = self._matching_panes_for_handle(handle, process_names)
        if matched is None:
            return None
        return bool(matched)


# ---------------------------------------------------------------------------
# Shared factory
# ---------------------------------------------------------------------------
#
# One seam for the whole application.  Every caller goes through this rather
# than constructing a runtime inline, so a test that substitutes the runtime
# substitutes it everywhere — otherwise a faked paste sits next to a real tmux
# process walk in the same call.

_installed: SessionRuntime | None = None


def get_runtime() -> SessionRuntime:
    """The process-control boundary. Substitute it with set_runtime in tests.

    Not cached: building one is free (it holds no connection and looks its
    runner up per call), and a fresh instance keeps the late-bound subprocess
    lookup honest for callers that patch the module underneath it.
    """
    return _installed if _installed is not None else TmuxSessionRuntime()


def set_runtime(runtime: SessionRuntime | None) -> None:
    """Install a runtime for every call site (None restores the tmux default)."""
    global _installed
    _installed = runtime
