"""Supervise channel connectors alongside the API, in one control plane.

`woltspace start` gives the connector the same lifecycle as the server: it
starts with the API, stops with it, restarts a bounded number of times if it
dies, and reports its state through `/health` and `woltspace status`.

State is written to `<platform_state>/connectors.json`. That file is a status
report — it never carries a token.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .channels import ConnectorPlan
from .layout import RuntimeLayout
from .processes import pid_alive, process_command

# Bounded restart: at most MAX_RESTARTS crashes inside RESTART_WINDOW seconds.
MAX_RESTARTS = 5
RESTART_WINDOW = 300.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 30.0
POLL_INTERVAL = 0.25


def report_path(layout: RuntimeLayout) -> Path:
    return layout.platform_state / "connectors.json"


def read_connector_report(layout: RuntimeLayout) -> dict:
    try:
        report = json.loads(report_path(layout).read_text())
        return report if isinstance(report, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# A second long-poller on the same bot token makes Telegram reject ours with
# 409 Conflict. Restarting cannot win that race — the other poller owns the
# token until it stops — so we name it instead of looping.
TOKEN_CLASH_MARKERS = (
    "terminated by other getupdates request",
    "conflict: terminated by other",
    "error_code\":409",
    "409 conflict",
)
TOKEN_CLASH_ERROR = (
    "another process is already polling this bot token "
    "(Telegram getUpdates returned 409 Conflict)"
)
TOKEN_CLASH_REMEDY = (
    "One bot token can only be polled by one process. Stop the other instance "
    "(e.g. `woltspace stop`, or stop the container) — or give this instance its "
    "own test bot token in channels.telegram.token."
)
LOG_SCAN_LIMIT = 64 * 1024


def find_token_clash(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in TOKEN_CLASH_MARKERS)


def _matches_connector(pid: int, command: list, *, ps_bin="ps", runner=subprocess.run) -> bool:
    """Only reap a pid that still looks like the connector we recorded.

    Pids are reused. Without this, a stale record could point at whatever
    unrelated process inherited the number.
    """
    if not command:
        return False
    running = process_command(pid, ps_bin=ps_bin, runner=runner)
    if not running:
        return False
    module = str(command[-1])
    return module in running or " ".join(str(part) for part in command) == running


def timeout_for_stillborn() -> float:
    """A child that must die immediately after birth gets a short leash."""
    return 5.0


def backoff_delay(restarts: int) -> float:
    return min(BACKOFF_BASE * (2 ** max(restarts - 1, 0)), BACKOFF_CAP)


@dataclass
class ConnectorState:
    plan: ConnectorPlan
    state: str = "pending"
    pid: int | None = None
    restarts: int = 0
    started_at: float = 0.0
    last_exit_code: int | None = None
    error: str = ""
    log: str = ""
    remedy_override: str = ""
    token_clash: bool = False
    log_offset: int = 0
    crash_times: list[float] = field(default_factory=list)

    def to_record(self) -> dict:
        record = self.plan.to_record()
        if self.remedy_override:
            record["remedy"] = self.remedy_override
        record.update({
            "state": self.state,
            "pid": self.pid,
            "restarts": self.restarts,
            "started_at": int(self.started_at) or None,
            "last_exit_code": self.last_exit_code,
            "error": self.error or None,
            "log": self.log or None,
        })
        return record


class ChannelSupervisor:
    """Start, watch, bounded-restart, and stop connector child processes."""

    def __init__(
        self,
        layout: RuntimeLayout,
        plans: list[ConnectorPlan],
        *,
        max_restarts: int = MAX_RESTARTS,
        restart_window: float = RESTART_WINDOW,
        poll_interval: float = POLL_INTERVAL,
        popen=subprocess.Popen,
        sleep=time.sleep,
        clock=time.time,
        ps_bin: str | None = None,
        runner=subprocess.run,
        already_reaped: bool = False,
    ):
        self.layout = layout
        self._already_reaped = already_reaped
        self.ps_bin = ps_bin or os.environ.get("WOLTSPACE_PS_BIN", "ps")
        self._run = runner
        self.max_restarts = max_restarts
        self.restart_window = restart_window
        self.poll_interval = poll_interval
        self._popen = popen
        self._sleep = sleep
        self._clock = clock
        self.states = {plan.name: ConnectorState(plan) for plan in plans}
        self._children: dict[str, subprocess.Popen] = {}
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def reap_orphans(self) -> list[int]:
        """Kill connectors a previous control plane left behind.

        A `kill -9`, an OOM, or a `docker kill` never reaches `stop()`, and the
        connector runs in its own process group, so it survives — still holding
        the bot token. The next start would then spawn a rival that loses the
        409 race and reports a remedy (`woltspace stop`) which cannot help,
        because nothing tracks the orphan. `connectors.json` does: it already
        records the pid and the command.
        """
        reaped = []
        for record in read_connector_report(self.layout).get("connectors", []):
            pid = record.get("pid")
            if not isinstance(pid, int) or pid <= 0 or not pid_alive(pid):
                continue
            if not _matches_connector(
                pid, record.get("command") or [], ps_bin=self.ps_bin, runner=self._run
            ):
                continue
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    continue
            reaped.append(pid)
        for _ in range(40):
            if not any(pid_alive(pid) for pid in reaped):
                break
            time.sleep(0.05)
        for pid in reaped:
            if pid_alive(pid):
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            try:
                # If the orphan happens to be our own child (a restart inside
                # one process), collect it rather than leave a zombie holding
                # the pid.
                os.waitpid(pid, os.WNOHANG)
            except (ChildProcessError, OSError):
                pass
        return reaped

    def start(self, *, watch: bool = True) -> None:
        """Spawn every enabled connector. `watch=False` leaves ticking to the caller."""
        self.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        if not self._already_reaped:
            orphans = self.reap_orphans()
            if orphans:
                print(f"[connectors] reaped {len(orphans)} orphaned connector(s): {orphans}")
        for name, state in self.states.items():
            if not state.plan.enabled:
                state.state = "disabled"
                continue
            self._spawn(name)
        self.publish()
        if watch and any(state.plan.enabled for state in self.states.values()):
            self._thread = threading.Thread(
                target=self._watch, name="channel-supervisor", daemon=True
            )
            self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stopping.set()
        # Snapshot and mark under one lock hold, so a _spawn racing this can
        # only ever land on the stillborn branch — never re-publish `running`.
        with self._lock:
            children = list(self._children.items())
            self._children.clear()
            for name, _child in children:
                self.states[name].state = "stopped"
                self.states[name].pid = None
        for _name, child in children:
            self._terminate(child, timeout=timeout)
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        self.publish()

    # -- internals ---------------------------------------------------------

    def _log_path(self, name: str) -> Path:
        return self.layout.logs_dir / f"connector-{name}.log"

    def _spawn(self, name: str) -> None:
        state = self.states[name]
        plan = state.plan
        log_path = self._log_path(name)
        state.log = str(log_path)
        env = dict(os.environ)
        env.update(plan.env)
        try:
            handle = log_path.open("a")
            # The adapter may log a request URL, and a request URL carries the
            # token. Keep it as private as the report beside it.
            os.chmod(log_path, 0o600)
        except OSError as exc:
            state.state = "failed"
            state.error = f"cannot open {log_path}: {exc}"
            return
        try:
            child = self._popen(
                list(plan.command),
                cwd=plan.cwd or None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            handle.close()
            state.state = "failed"
            state.error = str(exc)
            return
        finally:
            if not handle.closed:
                handle.close()
        # Registration and the state transition must be one atomic step. If
        # `running` were published after releasing the lock, a stop() that
        # interleaved would terminate this child, mark it stopped, and then be
        # overwritten back to `running` — a dead pid reported as healthy.
        with self._lock:
            if self._stopping.is_set():
                # stop() already took its snapshot; this child would never be
                # in it. Drop it here rather than leak a poller that `stop`
                # has already reported as stopped.
                stillborn = True
            else:
                self._children[name] = child
                state.pid = child.pid
                state.state = "running"
                state.error = ""
                state.started_at = time.time()
                stillborn = False
        if stillborn:
            self._terminate(child, timeout=timeout_for_stillborn())
            with self._lock:
                state.pid = None
                state.state = "stopped"

    def _terminate(self, child: subprocess.Popen, *, timeout: float) -> None:
        if child.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                child.terminate()
            except OSError:
                return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                return
            time.sleep(0.05)
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                child.kill()
            except OSError:
                pass

    def _watch(self) -> None:
        while not self._stopping.is_set():
            self.poll_once()
            self._sleep(self.poll_interval)

    def scan_log(self, name: str) -> bool:
        """Read what the connector appended since last tick; report a token clash.

        A clashing connector stays alive but deaf — python-telegram-bot logs the
        409 and keeps retrying — so a liveness check alone would call it healthy.
        """
        state = self.states[name]
        if not state.log:
            return False
        try:
            size = os.path.getsize(state.log)
            if size < state.log_offset:
                # Truncated or rotated. Without this, seek() lands past EOF and
                # clash detection goes permanently blind.
                state.log_offset = 0
            with open(state.log, "r", errors="replace") as handle:
                handle.seek(state.log_offset)
                chunk = handle.read(LOG_SCAN_LIMIT)
                state.log_offset = handle.tell()
        except OSError:
            return False
        if not chunk or not find_token_clash(chunk):
            return False
        state.state = "degraded"
        state.error = TOKEN_CLASH_ERROR
        state.remedy_override = TOKEN_CLASH_REMEDY
        state.token_clash = True
        return True

    def poll_once(self) -> None:
        """One supervision tick. Public so tests can drive it deterministically."""
        changed = False
        for name, state in self.states.items():
            if not state.plan.enabled or state.state in {"failed", "stopped", "disabled"}:
                continue
            if state.state in {"running", "degraded"} and self.scan_log(name):
                changed = True
            with self._lock:
                child = self._children.get(name)
            if child is None:
                continue
            code = child.poll()
            if code is None:
                continue
            changed = True
            state.last_exit_code = code
            state.pid = None
            with self._lock:
                self._children.pop(name, None)
            if self._stopping.is_set():
                state.state = "stopped"
                continue
            self.scan_log(name)
            if state.token_clash:
                # It did not merely die; it died because another poller owns the
                # token. Restarting would lose the same race five more times.
                state.state = "failed"
                state.error = TOKEN_CLASH_ERROR
                continue
            # Wall clock, not monotonic: the window means "crashes per five
            # minutes", and on macOS time.monotonic() does not advance while
            # the machine is asleep — a suspend would otherwise look like no
            # time passing at all.
            now = self._clock()
            state.crash_times = [
                stamp for stamp in state.crash_times if now - stamp < self.restart_window
            ]
            state.crash_times.append(now)
            if len(state.crash_times) > self.max_restarts:
                state.state = "failed"
                state.error = (
                    f"exited {len(state.crash_times)} times within "
                    f"{self.restart_window:g}s (last code {code}); not restarting"
                )
                continue
            state.restarts += 1
            delay = backoff_delay(state.restarts)
            state.state = "restarting"
            state.error = f"exited with {code}; restarting in {delay:g}s"
            self.publish()
            self._sleep(delay)
            if self._stopping.is_set():
                state.state = "stopped"
                continue
            self._spawn(name)
        if changed:
            self.publish()

    # -- reporting ---------------------------------------------------------

    def report(self) -> dict:
        return {
            "connectors": [state.to_record() for state in self.states.values()],
            "updated": int(time.time()),
        }

    def publish(self) -> None:
        path = report_path(self.layout)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.report(), indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(path)
