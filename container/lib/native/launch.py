"""Per-session native backend supervisor: `launch.py <codex|opencode|hermes> <engine argv...>`.

A wrapper execs this instead of the engine when its launch carries a native
attempt (WOLTSPACE_NATIVE_DIR). It starts the session's OWN backend (never a
shared daemon) in its own process session, runs the engine's TUI against it
in the foreground of the pane, publishes the engine-derived identity to the
attempt's ready.json, and when the TUI ends retires the attempt before
stopping the backend. Model and permission policy from the original argv are
carried to the backend, which is where turns actually run.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from native_adapters import ASSETS, KINDS, RETIRED, announce_ready, clear_state, retire, write_ready, write_state  # noqa: E402

CODEX_BYPASS = "--dangerously-bypass-approvals-and-sandbox"


def _value(argv: list[str], *flags: str) -> str | None:
    for i, word in enumerate(argv[:-1]):
        if word in flags:
            return argv[i + 1]
    return None


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def plan_codex(argv: list[str], directory: Path) -> dict:
    """App-server on the attempt's unix socket with the TUI's model/policy; the TUI connects --remote."""
    sock = directory / "app.sock"
    config = []
    if model := _value(argv, "-m", "--model"):
        config += ["-c", f"model={json.dumps(model)}"]
    if CODEX_BYPASS in argv:
        config += ["-c", 'approval_policy="never"', "-c", 'sandbox_mode="danger-full-access"']
    tui = [word for word in argv if word != "--no-daemon"]
    at = 2 if tui[:1] == ["resume"] else 0
    if at:  # a remote resume refuses permission flags; the server's config above carries the policy
        tui = [word for word in tui if word != CODEX_BYPASS]
    tui[at:at] = ["--remote", f"unix://{sock}"]
    return {"backend": ["codex", "app-server", *config, "--listen", f"unix://{sock}"], "backend_env": {},
            "tui": ["codex", *tui], "tui_env": {}, "sock": str(sock),
            "resume_id": argv[1] if at else None}


def plan_opencode(argv: list[str], directory: Path, cwd: str) -> dict:
    """`opencode serve` on an explicit free port with its own password; the TUI attaches to it.

    attach has no --model or --auto, so both move to the server's config: the
    model as its default and --auto as {"*": "allow"}, merged over the user's
    config so a rule that explicitly denies stays a deny, as --auto keeps it.
    """
    port, password = free_port(), secrets.token_urlsafe(24)
    config = {}
    if model := _value(argv, "-m", "--model"):
        config["model"] = model
    if "--auto" in argv:
        config["permission"] = {"*": "allow"}
    env = {"OPENCODE_SERVER_PASSWORD": password}
    if config:
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return {"backend": ["opencode", "serve", "--hostname", "127.0.0.1", "--port", str(port)], "backend_env": env,
            "tui": ["opencode", "attach", f"http://127.0.0.1:{port}", "--dir", cwd], "tui_env": env,
            "port": port, "password": password, "resume_id": _value(argv, "-s", "--session")}


def plan_hermes(argv: list[str], directory: Path, attempt: str, python: str) -> dict:
    """Our gateway on Hermes's own interpreter; `hermes chat --tui` attaches through HERMES_TUI_GATEWAY_URL.

    The flags `hermes chat` would turn into its own gateway's env are given to
    this gateway instead, since that is where the agent runs.
    """
    port = free_port()
    env = {}
    if model := _value(argv, "-m", "--model"):
        env["HERMES_MODEL"] = env["HERMES_INFERENCE_MODEL"] = model
    if provider := _value(argv, "--provider"):
        env["HERMES_TUI_PROVIDER"] = env["HERMES_INFERENCE_PROVIDER"] = provider
    if "--yolo" in argv:
        env["HERMES_YOLO_MODE"] = "1"
    tui = list(argv)
    if "--tui" not in tui:  # only Hermes's own TUI attaches to a gateway; the classic REPL runs its agent in-process
        tui.insert(1 if tui[:1] == ["chat"] else 0, "--tui")
    return {"backend": [python, str(ASSETS / "hermes_gateway.py"), str(port), str(directory), attempt],
            "backend_env": env, "tui": ["hermes", *tui],
            "tui_env": {"HERMES_TUI_GATEWAY_URL": f"ws://127.0.0.1:{port}/ws"}, "port": port}


def _codex_rpc(sock: str, calls: list[tuple[str, dict]], strict: bool = False,
               deadline: float | None = None) -> list[dict]:
    """strict: an error reply raises instead of reading as an empty result. deadline (monotonic):
    the whole exchange ends there, however many notifications arrive (TimeoutError)."""
    from websockets.sync.client import unix_connect

    def left():
        if deadline is None:
            return 5
        if (rest := deadline - time.monotonic()) <= 0:
            raise TimeoutError("codex app-server: no reply before the deadline")
        return min(5, rest)
    with unix_connect(sock, "ws://localhost/", open_timeout=left()) as conn:
        out, n = [], 0
        for method, params in [("initialize", {"clientInfo": {"name": "woltspace-native", "version": "1"}}), *calls]:
            n += 1
            conn.send(json.dumps({"id": n, "method": method, "params": params}))
            while (reply := json.loads(conn.recv(timeout=left()))).get("id") != n:
                pass
            if n == 1:
                conn.send(json.dumps({"method": "initialized"}))
            elif strict and "error" in reply:
                raise RuntimeError(f"{method}: {reply['error'].get('message', '')[:200]}")
            else:
                out.append(reply.get("result") or {})
        return out


def codex_thread(plan: dict, cwd: str) -> str | None:
    """The TUI's thread on this private app-server: the one user thread it has loaded, in this cwd.

    Only threads THIS server process loaded count (not the wolt's CODEX_HOME
    history), and only the engine's own user threads: codex also loads
    ephemeral helper threads (threadSource "thread_title") in the same cwd.
    A resume must come back as exactly the expected id.
    """
    [loaded] = _codex_rpc(plan["sock"], [("thread/loaded/list", {})])
    ids = [t if isinstance(t, str) else t.get("id") for t in loaded.get("data", [])]
    reads = _codex_rpc(plan["sock"], [("thread/read", {"threadId": i}) for i in ids])
    main = [r.get("thread", {}) for r in reads]
    main = [t["id"] for t in main if t.get("threadSource") == "user" and not t.get("ephemeral")
            and not t.get("parentThreadId") and t.get("cwd") == cwd]
    if plan["resume_id"]:
        return plan["resume_id"] if plan["resume_id"] in main else None
    return main[0] if len(main) == 1 else None


def watch_codex(sock: str, thread: str, directory: Path, attempt: str, on_known=None) -> None:
    """Busy/idle from the app-server's thread/status/changed for this thread; announce each turn's end.
    on_known runs once the first status is written; when the watch ends the state is unknown again."""
    from websockets.sync.client import unix_connect
    try:
        _watch_codex(unix_connect(sock, "ws://localhost/", open_timeout=5), thread, directory, attempt, on_known)
    finally:
        clear_state(directory)


def _watch_codex(conn, thread, directory, attempt, on_known):
    with conn:
        conn.send(json.dumps({"id": 1, "method": "initialize",
                              "params": {"clientInfo": {"name": "woltspace-native", "version": "1"}}}))
        conn.send(json.dumps({"method": "initialized"}))
        conn.send(json.dumps({"id": 2, "method": "thread/read", "params": {"threadId": thread}}))
        first, active = True, False
        for raw in conn:
            message = json.loads(raw)
            if message.get("id") == 2:
                status = ((message.get("result") or {}).get("thread") or {}).get("status") or {}
            elif message.get("method") == "thread/status/changed" and message["params"].get("threadId") == thread:
                status = message["params"].get("status") or {}
            else:
                continue
            if status.get("type") not in ("active", "idle"):
                continue  # an error or an unknown status leaves it unknown
            now = status.get("type") == "active"
            if not write_state(directory, now, os.getpid()):
                return  # retired
            if on_known:
                on_known()
                on_known = None
            elif (first or active) and not now:  # a turn ended, possibly while the watch was down
                announce_ready(attempt, "turn_end")
            first, active = False, now


def _opencode_api(plan: dict, method: str, path: str, body=None):
    import base64
    auth = base64.b64encode(f"opencode:{plan['password']}".encode()).decode()
    request = urllib.request.Request(f"http://127.0.0.1:{plan['port']}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=5) as answer:
        return json.loads(answer.read() or b"null")


def _wait(check, seconds: float):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if found := check():
                return found
        except Exception:
            pass
        time.sleep(0.2)
    return None


def _own_backend(command: list[str], env: dict, log: Path) -> subprocess.Popen:
    """The backend gets its own process session (no Ctrl-C or SIGHUP from the pane) and dies with us."""
    def child():
        if sys.platform.startswith("linux"):
            import ctypes
            ctypes.CDLL(None).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    out = open(log, "ab")
    return subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                            start_new_session=True, preexec_fn=child)


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def main(argv: list[str]) -> int:
    harness, engine_argv = argv[0], argv[1:]
    directory, attempt = Path(os.environ["WOLTSPACE_NATIVE_DIR"]), os.environ["WOLTSPACE_NATIVE_ATTEMPT"]
    native, cwd = {"kind": KINDS[harness], "addr": str(directory), "attempt": attempt}, os.getcwd()
    if harness == "codex":
        plan = plan_codex(engine_argv, directory)
    elif harness == "opencode":
        plan = plan_opencode(engine_argv, directory, cwd)
    else:
        hermes = Path(subprocess.run(["sh", "-c", "command -v hermes"], capture_output=True, text=True).stdout.strip())
        python = hermes.read_text(errors="replace").splitlines()[0].removeprefix("#!").strip().split()[0]
        plan = plan_hermes(engine_argv, directory, attempt, python)
    backend = _own_backend(plan["backend"], {**os.environ, **plan["backend_env"]}, directory / "backend.log")
    tui = None

    def finish(*_):
        retire(native)  # invalidate before teardown
        if tui is not None and tui.poll() is None:
            tui.terminate()
        _stop(backend)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: (finish(), sys.exit(128 + 15)))
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the TUI
    try:
        if harness == "codex":
            ok = _wait(lambda: Path(plan["sock"]).exists(), 15)
        elif harness == "opencode":
            ok = _wait(lambda: _opencode_api(plan, "GET", "/config") is not None, 20)
            session = plan["resume_id"] or (ok and _opencode_api(plan, "POST", "/session", {}).get("id"))
            plan["tui"] += ["--session", session] if session else []
        else:
            ok = _wait(lambda: socket.create_connection(("127.0.0.1", plan["port"]), 1).close() or True, 20)
        if not ok:
            print(f"woltspace: the {harness} backend did not start (see {directory / 'backend.log'})", file=sys.stderr)
            return 1
        tui = subprocess.Popen(plan["tui"], env={**os.environ, **plan["tui_env"]},
                               preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        if harness == "codex":
            def publish():
                thread = _wait(lambda: codex_thread(plan, cwd), 120)
                ready = {"kind": native["kind"], "attempt": attempt, "sock": plan["sock"], "thread": thread,
                         "session_id": thread}
                published = []  # ready only once a first busy/idle is known; then watch again after a drop
                while thread and not (directory / RETIRED).exists():
                    try:
                        watch_codex(plan["sock"], thread, directory, attempt,
                                    None if published else lambda: published.append(write_ready(directory, ready)))
                    except Exception:  # unknown until it is seen again
                        time.sleep(1)
            threading.Thread(target=publish, daemon=True).start()
        elif harness == "opencode" and session:
            write_ready(directory, {"kind": native["kind"], "attempt": attempt, "port": plan["port"],
                                    "password": plan["password"], "session_id": session})
        return tui.wait()
    finally:
        finish()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
