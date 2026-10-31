"""Native inbox hints: one private directory per launch attempt, and bounded delivery.

A session's launch prepares an immutable locator {kind, addr, attempt} before
the engine starts. `addr` is a fresh owner-only directory; the engine side (a
hook, an extension or the wrapper's supervisor) later writes ready.json there
once it knows the engine's own identity. The registry never holds secrets or
readiness: it holds the locator, published by the lifecycle owner.

deliver() answers from the registry first, then from the attempt's ready
file, and only then talks to the engine. Every answer is a transport fact
(submitted / pending / unavailable / failed / unknown): none of them ACKs or
erases mail, and unknown is not a retry signal.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

KINDS = {
    "claude": "claude-rewake",
    "codex": "codex-app-server",
    "opencode": "opencode-http",
    "pi": "pi-extension",
    "hermes": "hermes-gateway",
}
ASSETS = Path(__file__).resolve().parent / "native"
READY, HINT, RETIRED, LISTENER, STATE = "ready.json", "hint", "retired", "listener.json", "state.json"
BUDGET = 3.0          # deliver() never takes longer than this
CLAIM_WAIT = 1.0      # how long a file hint waits to be taken before it counts as pending
HOOK_TIMEOUT = 86400  # Claude's asyncRewake listener lifetime, in seconds
CLAUDE_BUSY_TTL = 300  # an Esc interrupt fires no hook: a Claude busy this old no longer counts
_SOCKET_MAX = 100     # AF_UNIX paths stop at 107 bytes on Linux, 103 on macOS


def capability(harness: str | None) -> str:
    """'native' when this harness has a native hint path, else 'none'."""
    return "native" if harness in KINDS else "none"


def attempt_dir(wolts_dir: str | Path, session: str, attempt: str) -> Path:
    """Short and unique per (session, attempt): codex puts a unix socket in it."""
    digest = hashlib.sha256(f"{session}\0{attempt}".encode()).hexdigest()[:20]
    return Path(wolts_dir) / ".space" / "native" / digest


def prepare(wolts_dir: str | Path, session: str, harness: str | None, attempt: str) -> dict | None:
    """Create this attempt's private directory and return its locator (None: no native path).

    Called once per launch attempt, before exec. Never reuses a directory: a
    new attempt is a new locator, so a stale child can only write its old one.
    """
    kind = KINDS.get(harness or "")
    if not kind or not attempt:
        return None
    directory = attempt_dir(wolts_dir, session, attempt)
    if harness == "codex" and len(str(directory / "app.sock")) > _SOCKET_MAX:
        return None
    directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    if harness == "claude":
        command = [sys.executable, str(ASSETS / "claude_rewake.py"), str(directory), attempt]
        hook = {"type": "command", "asyncRewake": True, "timeout": HOOK_TIMEOUT, "command": shlex.join(command)}
        busy = {"type": "command", "timeout": 10, "command": shlex.join([*command, "--busy"])}
        tool = {"type": "command", "timeout": 10, "command": shlex.join([*command, "--tool"])}
        _write_private(directory / "settings.json", {"hooks": {
            "SessionStart": [{"hooks": [hook]}], "Stop": [{"hooks": [hook]}],
            "UserPromptSubmit": [{"hooks": [busy]}], "PostToolUse": [{"hooks": [tool]}],
            "StopFailure": [{"hooks": [hook]}]}})  # an API error ends a turn with no Stop: arm as a Stop does
    return {"kind": kind, "addr": str(directory), "attempt": attempt}


def launch_env(native: dict | None) -> dict:
    """The variables a launch carries so its wrapper and helpers find their attempt."""
    if not native:
        return {}
    return {"WOLTSPACE_NATIVE_DIR": native["addr"], "WOLTSPACE_NATIVE_ATTEMPT": native["attempt"],
            "WOLTSPACE_NATIVE_KIND": native["kind"]}


def write_ready(directory: str | Path, data: dict, announce: str = "ready") -> bool:
    """Atomically publish an attempt's ready data (owner-only) and tell the lodge. Refused once retired."""
    directory = Path(directory)
    if (directory / RETIRED).exists():
        return False
    _write_private(directory / READY, data)
    announce_ready(data.get("attempt", ""), announce)
    return True


def write_state(directory: str | Path, busy: bool, pid: int | None = None, ttl: float | None = None) -> bool:
    """The engine's own busy/idle; with `pid`, it only counts while that keeper lives, with `ttl` only that long."""
    directory = Path(directory)
    if (directory / RETIRED).exists():
        return False
    _write_private(directory / STATE, {"busy": busy, "since": time.time(), "pid": pid, "ttl": ttl})
    return True


def engine_state(directory: str | Path) -> str:
    """busy | idle | unknown (no state yet, or its keeper is gone): unknown is never idle."""
    try:
        state_ = json.loads((Path(directory) / STATE).read_text())
    except (OSError, ValueError):
        return "unknown"
    pid, ttl = state_.get("pid"), state_.get("ttl")
    if pid is not None and not _alive(pid) or ttl and time.time() - state_.get("since", 0) > ttl:
        return "unknown"
    return "busy" if state_.get("busy") is True else "idle"


def is_busy(directory: str | Path) -> bool:
    return engine_state(directory) == "busy"


def clear_state(directory: str | Path) -> None:
    (Path(directory) / STATE).unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text().split()[2] != "Z"
    except (OSError, ValueError, IndexError):
        return False if Path("/proc/self").exists() else True  # no procfs (macOS): trust the record


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A request that carries an inbox token never follows a redirect: the 3xx is an HTTPError."""
    def redirect_request(self, *args, **kwargs):
        return None


def announce_ready(attempt: str, kind: str = "ready") -> None:
    """Tell the lodge, once ready data is published, that this attempt takes input now, so
    mail that waited (a rest, a resume) gets its hint. Authenticated as the session's own
    inbox reader. Bounded and custody-neutral: a failure is ignored, never a launch failure.
    kind "turn_end": a turn just ended, so mail that waited while busy can get its hint."""
    token, session = os.environ.get("WOLTSPACE_INBOX_TOKEN"), os.environ.get("WOLTSPACE_WOLT_SESSION")
    if not (token and session and attempt):
        return
    api = os.environ.get("WOLTSPACE_API", "http://localhost:7777")
    request = urllib.request.Request(
        f"{api}/inbox/ready", method="POST",
        data=json.dumps({"session": session, "attempt": attempt, **({"kind": kind} if kind != "ready" else {})}).encode(),
        headers={"Content-Type": "application/json", "X-Woltspace-Inbox-Token": token})
    try:  # the lodge's own API: never through a proxy
        urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=2).close()
    except Exception:
        pass


def retire(native: dict | None) -> None:
    """Invalidate an attempt before its helpers are torn down; later ready writes are refused."""
    if not native:
        return
    directory = Path(native["addr"])
    try:
        (directory / RETIRED).touch(mode=0o600)
        (directory / READY).unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def state(record: dict) -> tuple[str, str, dict | None]:
    """(ready | pending | unavailable, code, ready data) from registry facts, then the ready file."""
    native = record.get("native")
    if not isinstance(native, dict) or native.get("kind") not in KINDS.values():
        return "unavailable", "no_native", None
    if record.get("launching"):  # an unresolved or in-progress launch: never routed to yet
        return "pending", "launching", None
    if record.get("status") != "running":
        return "unavailable", "not_running", None
    if not native.get("attempt") or native.get("attempt") != record.get("attempt"):
        return "unavailable", "stale_attempt", None
    directory = Path(native.get("addr", ""))
    if (directory / RETIRED).exists() or not directory.is_dir():
        return "unavailable", "retired", None
    try:
        ready = json.loads((directory / READY).read_text())
    except FileNotFoundError:
        return "pending", "not_ready", None
    except (OSError, ValueError):
        return "unavailable", "bad_ready", None
    if not isinstance(ready, dict) or ready.get("attempt") != native["attempt"] or ready.get("kind") != native["kind"]:
        return "unavailable", "stale_ready", None
    known = record.get("harness_session_id")
    if known and ready.get("session_id") and ready["session_id"] != known:
        return "unavailable", "other_conversation", None
    if native["kind"] == KINDS["claude"] and time.time() > ready.get("armed_at", 0) + HOOK_TIMEOUT:
        return "pending", "listener_expired", ready
    if native["kind"] == KINDS["claude"] and ready.get("pid") and not _alive(ready["pid"]) and not is_busy(directory):
        # it claimed a hint and no turn end armed another; while that turn is busy it's just busy (deliver says so)
        return "pending", "listener_gone", None
    return "ready", "ready", ready


def deliver(record: dict, hint: str) -> tuple[str, str]:
    """Hand one fixed, body-free hint to a live session's own engine, within BUDGET seconds.

    Returns (submitted | pending | unavailable | failed | unknown, code). Never
    raises, never ACKs, never pastes and never starts anything.
    """
    deadline = time.monotonic() + BUDGET
    try:
        status, code, ready = state(record)
        if status != "ready":  # unavailable, launching, not ready or a listener gone: nothing placed
            return status, code
        native = record["native"]
        engine = engine_state(native["addr"]) if native["kind"] in (KINDS["claude"], KINDS["codex"]) else "idle"
        if engine == "busy":  # inside the running turn where the engine allows it; never typed into its input
            if native["kind"] == KINDS["claude"]:
                return _drop(Path(native["addr"]), hint, deadline)  # its next tool step's hook shows it
            if native["kind"] == KINDS["codex"]:
                return _steer(ready, hint, deadline)
            return "pending", "busy"  # nothing placed: the turn_end announce brings the wake back
        if engine == "unknown" and native["kind"] == KINDS["codex"]:  # not yet (or no longer) observed
            return "pending", "not_ready"
        if native["kind"] == KINDS["codex"]:
            return _codex(ready, hint, deadline)
        if native["kind"] == KINDS["opencode"]:
            return _opencode(ready, hint, deadline)
        return _drop(Path(native["addr"]), hint, deadline)
    except Exception as exc:  # a transport fact, never an exception for the wake thread
        return "failed", type(exc).__name__


def _drop(directory: Path, hint: str, deadline: float) -> tuple[str, str]:
    """File hint: one atomic file the engine side claims by renaming it away. Repeats overwrite it."""
    tmp = directory / f"{HINT}.{os.getpid()}.tmp"
    _write_private(tmp, hint, raw=True)
    os.replace(tmp, directory / HINT)
    until = min(deadline, time.monotonic() + CLAIM_WAIT)
    while time.monotonic() < until:
        if not (directory / HINT).exists():
            return "submitted", "claimed"
        time.sleep(0.05)
    return "pending", "placed"


def _codex(ready: dict, hint: str, deadline: float) -> tuple[str, str]:
    """codex queue against the session's own app-server, never the shared daemon."""
    try:
        done = subprocess.run(
            ["codex", "queue", "--remote", f"unix://{ready['sock']}", "--thread", ready["thread"],
             "--message", hint],
            capture_output=True, timeout=max(0.1, deadline - time.monotonic()),
        )
    except subprocess.TimeoutExpired:
        return "unknown", "timeout"
    return ("submitted", "queued") if done.returncode == 0 else ("failed", f"rc{done.returncode}")


def _steer(ready: dict, hint: str, deadline: float) -> tuple[str, str]:
    """turn/steer into the thread's running turn, on the session's own app-server. Its newest turn
    is read first (no items): only one still in progress is steered. None, or a steer the turn
    refuses (it just ended): pending/busy, so the turn's end hints instead. Both calls end at
    deliver's deadline: before the steer was sent nothing is placed (pending/busy); after, it may
    have been (unknown/timeout, as _codex)."""
    from native.launch import _codex_rpc
    try:
        [turns] = _codex_rpc(ready["sock"], [("thread/turns/list", {"threadId": ready["thread"], "limit": 1,
                                                                    "itemsView": "notLoaded"})],
                             strict=True, deadline=deadline)
        newest = (turns.get("data") or [{}])[0]
        if newest.get("status") != "inProgress" or not newest.get("id"):
            return "pending", "busy"
    except Exception:
        return "pending", "busy"
    try:
        _codex_rpc(ready["sock"], [("turn/steer", {"threadId": ready["thread"], "expectedTurnId": newest["id"],
                                                   "input": [{"type": "text", "text": hint}]})],
                   strict=True, deadline=deadline)
    except TimeoutError:
        return "unknown", "timeout"
    except Exception:
        return "pending", "busy"
    return "submitted", "steered"


def _opencode(ready: dict, hint: str, deadline: float) -> tuple[str, str]:
    """OpenCode's prompt API on the session's own password-protected loopback server."""
    auth = base64.b64encode(f"opencode:{ready['password']}".encode()).decode()
    request = urllib.request.Request(
        f"http://127.0.0.1:{int(ready['port'])}/session/{ready['session_id']}/prompt_async",
        data=json.dumps({"parts": [{"type": "text", "text": hint}]}).encode(), method="POST",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=max(0.1, deadline - time.monotonic())) as answer:
            return "submitted", f"http{answer.status}"
    except urllib.error.HTTPError as exc:
        return "failed", f"http{exc.code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            return "unknown", "timeout"
        return "failed", "unreachable"
    except TimeoutError:
        return "unknown", "timeout"


def _write_private(path: Path, data, raw: bool = False) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(data if raw else json.dumps(data))
    os.replace(tmp, path)
