"""Native inbox hints: attempt locators, registry-first delivery state, the senders, the launch
plans and supervisor, and the engine-side helpers (Claude listener, Pi extension). Fakes only:
no engine and no model runs here.

Usage: uv run --extra test pytest test/test_native_adapters.py -v
"""

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(ROOT / "container" / "lib" / "native"))

import launch  # noqa: E402
import native_adapters as na  # noqa: E402
from harnesses import build_command  # noqa: E402

HINT = "New inbox mail: run `woltspace inbox fetch` (nonce n1)."


@pytest.fixture
def lodge(tmp_path):
    """A short wolts dir (codex puts a socket in the attempt dir) and a live record per harness."""
    wolts = Path(os.environ.get("TMPDIR", "/tmp")) / f"na{os.getpid()}{time.monotonic_ns() % 10**6}"
    wolts.mkdir()

    def live(harness, attempt="a1", ready=None, **fields):
        native = na.prepare(wolts, "pal-x", harness, attempt)
        if ready is not None:
            na.write_ready(native["addr"], {"kind": native["kind"], "attempt": attempt, **ready})
        return {"status": "running", "attempt": attempt, "native": native, **fields}

    yield wolts, live
    shutil.rmtree(wolts, ignore_errors=True)


def _gone(pid):
    """Exited: no /proc entry, or a zombie (PID 1 here may never reap it). It can vanish while we look."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
    except OSError:  # FileNotFoundError or ProcessLookupError: already gone
        return True


def _ends(pid, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not _gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return _gone(pid)


def fake_bin(tmp_path, name, body):
    path = tmp_path / "bin" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def test_prepare_makes_one_private_dir_per_attempt_and_none_without_support(lodge):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "pi", "a1")
    assert native == {"kind": "pi-extension", "addr": native["addr"], "attempt": "a1"}
    assert Path(native["addr"]).stat().st_mode & 0o777 == 0o700
    assert na.prepare(wolts, "pal-x", "pi", "a2")["addr"] != native["addr"]
    with pytest.raises(FileExistsError):  # an attempt's directory is never reused
        na.prepare(wolts, "pal-x", "pi", "a1")
    assert na.prepare(wolts, "pal-x", "unknown-engine", "a3") is None
    assert na.prepare(wolts, "pal-x", "pi", "") is None
    assert [na.capability(h) for h in ("claude", "codex", "opencode", "pi", "hermes", "other")] == ["native"] * 5 + ["none"]


def test_codex_alone_needs_a_short_enough_dir_for_its_socket(tmp_path):
    deep = tmp_path / ("d" * 90)
    assert na.prepare(deep, "pal-x", "codex", "a1") is None
    assert na.prepare(deep, "pal-x", "claude", "a1") is not None


def test_claude_settings_arm_an_async_rewake_listener_at_start_and_every_turn_end(lodge):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "claude", "a1")
    hooks = json.loads((Path(native["addr"]) / "settings.json").read_text())["hooks"]
    assert sorted(hooks) == ["PostToolUse", "SessionStart", "Stop", "StopFailure", "UserPromptSubmit"]
    for event in ("SessionStart", "Stop"):
        [hook] = hooks[event][0]["hooks"]
        assert hook["asyncRewake"] is True and hook["timeout"] == 86400
        assert hook["command"].endswith(f"claude_rewake.py {native['addr']} a1")
    [busy] = hooks["UserPromptSubmit"][0]["hooks"]  # synchronous: the turn waits for it
    assert hooks["StopFailure"] == hooks["Stop"]  # an API error's end arms the same listener a Stop does
    assert "asyncRewake" not in busy and busy["command"].endswith(f"claude_rewake.py {native['addr']} a1 --busy")
    [tool] = hooks["PostToolUse"][0]["hooks"]  # synchronous: its output joins the running turn
    assert "asyncRewake" not in tool and tool["command"].endswith(f"claude_rewake.py {native['addr']} a1 --tool")


@pytest.mark.parametrize("change, expected", [
    (lambda r: r.pop("native"), ("unavailable", "no_native")),
    (lambda r: r.update(launching={"attempt": "a2"}), ("pending", "launching")),
    (lambda r: r.update(status="stopping"), ("unavailable", "not_running")),
    (lambda r: r.update(attempt="a2"), ("unavailable", "stale_attempt")),
    (lambda r: na.retire(r["native"]), ("unavailable", "retired")),
    (lambda r: (Path(r["native"]["addr"]) / "ready.json").unlink(), ("pending", "not_ready")),
    (lambda r: na.write_ready(r["native"]["addr"], {"kind": "pi-extension", "attempt": "a0"}),
     ("unavailable", "stale_ready")),
    (lambda r: r.update(harness_session_id="other"), ("unavailable", "other_conversation")),
])
def test_registry_facts_then_the_ready_file_decide_before_any_send(lodge, change, expected):
    _, live = lodge
    record = live("pi", ready={"session_id": "s1"}, harness_session_id="s1")
    directory = Path(record["native"]["addr"])
    change(record)
    assert na.deliver(record, HINT) == expected
    assert not (directory / "hint").exists()  # nothing was handed over


def test_an_expired_claude_listener_gets_no_hint_placed(lodge):
    _, live = lodge
    record = live("claude", ready={"armed_at": time.time() - 86401})
    assert na.deliver(record, HINT) == ("pending", "listener_expired")  # a wait, never "hinted"
    assert not (Path(record["native"]["addr"]) / "hint").exists()


def test_a_file_hint_is_submitted_once_claimed_and_pending_otherwise(lodge):
    _, live = lodge
    record = live("pi", ready={})
    hint = Path(record["native"]["addr"]) / "hint"
    assert na.deliver(record, "first") == ("pending", "placed")
    assert na.deliver(record, HINT) == ("pending", "placed")  # one file, the newest text
    assert hint.read_text() == HINT and len(list(hint.parent.glob("hint*"))) == 1

    def claim():
        while not hint.exists():
            time.sleep(0.01)
        hint.rename(hint.with_name("taken"))
    hint.unlink()
    threading.Thread(target=claim, daemon=True).start()
    assert na.deliver(record, HINT) == ("submitted", "claimed")


@pytest.mark.parametrize("body, expected", [
    ('echo "$@" > "$0.args"; exit 0', ("submitted", "queued")),
    ("exit 3", ("failed", "rc3")),
    ("sleep 10", ("unknown", "timeout")),
])
def test_codex_hints_go_through_queue_on_the_sessions_own_socket(lodge, tmp_path, monkeypatch, body, expected):
    _, live = lodge
    fake = fake_bin(tmp_path, "codex", body + "\n")
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    record = live("codex", ready={"sock": "/x/app.sock", "thread": "t1"})
    na.write_state(record["native"]["addr"], False)  # its watcher has seen it idle
    started = time.monotonic()
    assert na.deliver(record, HINT) == expected
    assert time.monotonic() - started < na.BUDGET + 0.5
    if expected[0] == "submitted":
        assert Path(f"{fake}.args").read_text().split(" ", 6) == [
            "queue", "--remote", "unix:///x/app.sock", "--thread", "t1", "--message", HINT + "\n"]


@pytest.mark.parametrize("code, expected", [(200, ("submitted", "http200")), (500, ("failed", "http500"))])
def test_opencode_hints_use_the_prompt_api_with_the_sessions_own_password(lodge, code, expected):
    _, live = lodge
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"], json.loads(self.rfile.read(
                int(self.headers["Content-Length"])))))
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        record = live("opencode", ready={"port": server.server_address[1], "password": "pw", "session_id": "ses_1"})
        assert na.deliver(record, HINT) == expected
    finally:
        server.shutdown()
    assert seen == [("/session/ses_1/prompt_async", "Basic " + base64.b64encode(b"opencode:pw").decode(),
                     {"parts": [{"type": "text", "text": HINT}]})]


def test_an_unreachable_backend_or_a_broken_record_is_a_failure_not_an_exception(lodge):
    _, live = lodge
    record = live("opencode", ready={"port": launch.free_port(), "password": "pw", "session_id": "ses_1"})
    assert na.deliver(record, HINT) == ("failed", "unreachable")
    assert na.deliver({"status": "running", "attempt": "a1", "native": {"kind": "pi-extension", "attempt": "a1",
                                                                         "addr": None}}, HINT)[0] in ("failed", "unavailable")


def test_retire_drops_ready_data_and_refuses_a_late_child_write(lodge):
    _, live = lodge
    record = live("pi", ready={})
    na.retire(record["native"])
    assert not (Path(record["native"]["addr"]) / "ready.json").exists()
    assert na.write_ready(record["native"]["addr"], {"kind": "pi-extension", "attempt": "a1"}) is False


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "pi", "hermes"])
def test_builders_are_unchanged_without_a_locator_and_carry_the_attempt_with_one(lodge, harness):
    wolts, _ = lodge
    args = dict(session_id="11111111-2222-3333-4444-555555555555", session_name="pal-x", model="m/x", prompt="hi")
    plain = build_command(harness, "spawn", **args)
    assert build_command(harness, "spawn", native=None, **args) == plain
    native = na.prepare(wolts, "pal-x", harness, "a1")
    words = __import__("shlex").split(build_command(harness, "spawn", native=native, **args))
    assert words[:4] == ["env", f"WOLTSPACE_NATIVE_DIR={native['addr']}", "WOLTSPACE_NATIVE_ATTEMPT=a1",
                         f"WOLTSPACE_NATIVE_KIND={native['kind']}"]
    extra = {"claude": ["--settings", f"{native['addr']}/settings.json"],
             "pi": ["-e", str(na.ASSETS / "pi_hint.js")]}.get(harness, [])
    plain_words = __import__("shlex").split(plain)
    assert words[4:] == plain_words[:1] + extra + plain_words[1:]
    assert build_command(harness, "login", native=native) == build_command(harness, "login")


def test_codex_plan_runs_the_tui_remote_on_its_own_server_with_the_same_model_and_policy(tmp_path):
    bypass = "--dangerously-bypass-approvals-and-sandbox"
    plan = launch.plan_codex(["--no-daemon", bypass, "-m", "gpt-x", "hello"], tmp_path)
    sock = f"unix://{tmp_path}/app.sock"
    assert plan["backend"] == ["codex", "app-server", "-c", 'model="gpt-x"', "-c", 'approval_policy="never"',
                               "-c", 'sandbox_mode="danger-full-access"', "--listen", sock]
    assert plan["tui"] == ["codex", "--remote", sock, bypass, "-m", "gpt-x", "hello"]
    resumed = launch.plan_codex(["resume", "t1", "--no-daemon", bypass, "-m", "gpt-x"], tmp_path)
    assert resumed["tui"] == ["codex", "resume", "t1", "--remote", sock, "-m", "gpt-x"]  # remote resume refuses the flag
    assert resumed["resume_id"] == "t1" and plan["resume_id"] is None
    assert "-c" not in launch.plan_codex(["--no-daemon"], tmp_path)["backend"]


def test_opencode_plan_moves_model_and_auto_to_the_server_and_keeps_its_password_private(tmp_path):
    plan = launch.plan_opencode(["--auto", "--session", "ses_1", "--model", "p/m"], tmp_path, "/work")
    assert json.loads(plan["backend_env"]["OPENCODE_CONFIG_CONTENT"]) == {"model": "p/m", "permission": {"*": "allow"}}
    assert plan["backend_env"]["OPENCODE_SERVER_PASSWORD"] == plan["password"] == plan["tui_env"]["OPENCODE_SERVER_PASSWORD"]
    assert plan["password"] not in " ".join(plan["backend"] + plan["tui"])
    assert plan["tui"] == ["opencode", "attach", f"http://127.0.0.1:{plan['port']}", "--dir", "/work"]
    assert plan["resume_id"] == "ses_1"
    assert "OPENCODE_CONFIG_CONTENT" not in launch.plan_opencode([], tmp_path, "/work")["backend_env"]


def test_hermes_plan_gives_the_gateway_the_flags_hermes_chat_would_give_its_own(tmp_path):
    plan = launch.plan_hermes(["chat", "--resume", "r1", "--yolo", "--provider", "or", "-m", "a/b"], tmp_path, "a1", "/py")
    assert plan["backend"] == ["/py", str(na.ASSETS / "hermes_gateway.py"), str(plan["port"]), str(tmp_path), "a1"]
    assert plan["backend_env"] == {"HERMES_MODEL": "a/b", "HERMES_INFERENCE_MODEL": "a/b", "HERMES_TUI_PROVIDER": "or",
                                   "HERMES_INFERENCE_PROVIDER": "or", "HERMES_YOLO_MODE": "1"}
    assert plan["tui"] == ["hermes", "chat", "--tui", "--resume", "r1", "--yolo", "--provider", "or", "-m", "a/b"]
    assert plan["tui_env"] == {"HERMES_TUI_GATEWAY_URL": f"ws://127.0.0.1:{plan['port']}/ws"}


def test_the_codex_identity_is_the_one_loaded_user_thread_or_the_exact_resume(monkeypatch):
    threads = {"main": {"threadSource": "user", "cwd": "/w"},
               "title": {"threadSource": "thread_title", "ephemeral": True, "cwd": "/w"},
               "sub": {"threadSource": "user", "parentThreadId": "main", "cwd": "/w"},
               "elsewhere": {"threadSource": "user", "cwd": "/other"}}

    def rpc(_sock, calls):
        if calls[0][0] == "thread/loaded/list":
            return [{"data": list(threads)}]
        return [{"thread": {"id": params["threadId"], **threads[params["threadId"]]}} for _, params in calls]
    monkeypatch.setattr(launch, "_codex_rpc", rpc)
    assert launch.codex_thread({"sock": "s", "resume_id": None}, "/w") == "main"
    assert launch.codex_thread({"sock": "s", "resume_id": "main"}, "/w") == "main"
    assert launch.codex_thread({"sock": "s", "resume_id": "gone"}, "/w") is None
    threads["second"] = {"threadSource": "user", "cwd": "/w"}
    assert launch.codex_thread({"sock": "s", "resume_id": None}, "/w") is None  # ambiguous: never guess


def test_the_supervisor_retires_the_attempt_before_stopping_its_own_backend(lodge, tmp_path, monkeypatch):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "codex", "a1")
    fake_bin(tmp_path, "codex", f"""if [ "$1" = app-server ]; then
  echo $$ > {tmp_path}/backend.pid; for a; do last=$a; done; : > "${{last#unix://}}"; exec sleep 60
fi
[ -e {native['addr']}/retired ] && echo early > {tmp_path}/tui.saw
exit 7
""")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("WOLTSPACE_NATIVE_DIR", native["addr"])
    monkeypatch.setenv("WOLTSPACE_NATIVE_ATTEMPT", "a1")
    previous = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        assert launch.main(["codex", "--no-daemon", "hi"]) == 7
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    pid = int((tmp_path / "backend.pid").read_text())
    assert (Path(native["addr"]) / "retired").exists() and not (tmp_path / "tui.saw").exists()
    assert _ends(pid)


def _listener(directory, attempt="a1", session="s1"):
    return subprocess.Popen([sys.executable, str(na.ASSETS / "claude_rewake.py"), str(directory), attempt],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_the_claude_listener_publishes_itself_claims_one_hint_and_wakes_with_exit_2(lodge):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "claude", "a1")
    directory = Path(native["addr"])
    proc = _listener(directory)
    proc.stdin.write(json.dumps({"session_id": "s1"}))
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not (directory / "ready.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    ready = json.loads((directory / "ready.json").read_text())
    assert ready["kind"] == "claude-rewake" and ready["attempt"] == "a1" and ready["session_id"] == "s1"
    record = {"status": "running", "attempt": "a1", "native": native, "harness_session_id": "s1"}
    assert na.deliver(record, HINT) == ("submitted", "claimed")
    assert proc.wait(5) == 2 and proc.stderr.read().strip() == HINT
    assert not list(directory.glob("*hint*")) and not list(directory.glob(".claimed*"))


def test_a_newer_listener_or_a_retired_attempt_ends_a_waiting_one_quietly(lodge):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "claude", "a1")["addr"])
    older = _listener(directory)
    older.stdin.write("{}")
    older.stdin.close()
    time.sleep(0.5)
    newer = _listener(directory)
    newer.stdin.write("{}")
    newer.stdin.close()
    assert older.wait(5) == 0
    (directory / "retired").touch()
    assert newer.wait(5) == 0


@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed")
def test_the_pi_extension_sends_one_claimed_hint_as_a_turn_starting_custom_message(lodge, tmp_path):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "pi", "a1")["addr"])
    driver = tmp_path / "drive.mjs"
    driver.write_text(f"""import ext from {json.dumps(str(na.ASSETS / 'pi_hint.js'))};
import fs from "node:fs";
const on = {{}}, sent = [];
ext({{ on: (name, fn) => (on[name] = fn), sendMessage: (m, o) => sent.push([m, o]) }});
on.session_start();
fs.writeFileSync({json.dumps(str(directory / 'hint'))}, {json.dumps(HINT)});
setTimeout(() => {{ on.session_shutdown(); console.log(JSON.stringify(sent)); }}, 700);
""")
    out = subprocess.run(["node", str(driver)], capture_output=True, text=True, timeout=20,
                         env={**os.environ, "WOLTSPACE_NATIVE_DIR": str(directory), "WOLTSPACE_NATIVE_ATTEMPT": "a1"})
    assert json.loads(out.stdout) == [[{"customType": "inbox-hint", "content": HINT, "display": True},
                                       {"triggerTurn": True}]]
    assert json.loads((directory / "ready.json").read_text())["attempt"] == "a1"
    assert not (directory / "hint").exists()


@pytest.mark.parametrize("wrapper, engine", [("wcodex", "codex"), ("wopencode", "opencode"), ("whermes", "hermes")])
def test_wrappers_hand_a_native_launch_to_the_supervisor_and_keep_the_plain_one(tmp_path, wrapper, engine):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shutil.copy(ROOT / "container" / "bin" / wrapper, bin_dir / wrapper)
    (tmp_path / "lib" / "native").mkdir(parents=True)
    fake_bin(tmp_path, "woltspace-python", 'echo "supervisor $*"\n')
    fake_bin(tmp_path, engine, f'echo "{engine} $*"\n')
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "WOLTSPACE_WOLT_HOME": str(tmp_path / "none")}
    run = lambda **extra: subprocess.run(["bash", str(bin_dir / wrapper), "a", "b c"], capture_output=True,
                                         text=True, env={**env, **extra}, cwd=tmp_path).stdout.strip()
    for isolation in ("external", "host"):
        assert run(WOLTSPACE_ISOLATION=isolation) == f"{engine} a b c"
        assert run(WOLTSPACE_ISOLATION=isolation, WOLTSPACE_NATIVE_DIR="/d") == \
            f"supervisor {bin_dir}/../lib/native/launch.py {engine} a b c"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux-only")
def test_a_killed_supervisor_takes_its_own_backend_with_it(lodge, tmp_path):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "codex", "a1")
    fake_bin(tmp_path, "codex", f"""if [ "$1" = app-server ]; then
  echo $$ > {tmp_path}/backend.pid; for a; do last=$a; done; : > "${{last#unix://}}"; exec sleep 60
fi
exec sleep 60
""")
    env = {**os.environ, "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}", **na.launch_env(native)}
    supervisor = subprocess.Popen([sys.executable, str(na.ASSETS / "launch.py"), "codex", "hi"], env=env,
                                  stdin=subprocess.DEVNULL)
    pid_file = tmp_path / "backend.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    supervisor.kill()
    supervisor.wait(5)
    assert _ends(pid)


class Lodge:
    """A stand-in for POST /inbox/ready: records each announce body and its token header."""

    def __init__(self):
        self.seen, parent = [], self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                parent.seen.append((self.path, self.headers["X-Woltspace-Inbox-Token"],
                                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_):
                pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.env = {"WOLTSPACE_API": f"http://127.0.0.1:{self.server.server_address[1]}",
                    "WOLTSPACE_INBOX_TOKEN": "tok", "WOLTSPACE_WOLT_SESSION": "pal-x"}


@pytest.mark.parametrize("harness, ready", [("claude", {"armed_at": time.time()}),
                                            ("codex", {"sock": "/x/app.sock", "thread": "t1"})])
def test_a_busy_engine_gets_its_hint_mid_turn_and_a_dead_watcher_never_holds_hints(lodge, harness, ready):
    _, live = lodge
    record = live(harness, ready=ready)
    directory = Path(record["native"]["addr"])
    na.write_state(directory, True)  # codex: no running turn known yet, so nothing to steer into
    if harness == "claude":  # left for its next tool step's hook
        assert na.deliver(record, HINT) == ("pending", "placed") and (directory / "hint").exists()
        (directory / "hint").unlink()
    else:
        assert na.deliver(record, HINT) == ("pending", "busy") and not (directory / "hint").exists()
    gone = subprocess.Popen(["true"])
    gone.wait()
    na.write_state(directory, True, gone.pid)  # its keeper is gone: unknown, never idle
    assert na.engine_state(directory) == "unknown" and not na.is_busy(directory)
    if harness == "codex":  # no observed state: a silent wait, never a blind `codex queue`
        assert na.deliver(record, HINT) == ("pending", "not_ready")
        na.clear_state(directory)
        assert na.deliver(record, HINT) == ("pending", "not_ready")
    na.retire(record["native"])
    assert na.write_state(directory, True) is False


def _hook(directory, event, *extra, env=None):
    return subprocess.run([sys.executable, str(na.ASSETS / "claude_rewake.py"), str(directory), "a1", *extra],
                          input=json.dumps({"session_id": "s1", "hook_event_name": event}), capture_output=True,
                          text=True, timeout=10, env={**os.environ, **(env or {})})


def test_claude_marks_a_human_turn_busy_and_never_claims_a_hint_mid_turn(lodge):
    wolts, _ = lodge
    native = na.prepare(wolts, "pal-x", "claude", "a1")
    directory = Path(native["addr"])
    proc = _listener(directory)  # armed at the previous turn's end
    proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "Stop"}))
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not (directory / "ready.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    busy = _hook(directory, "UserPromptSubmit", "--busy")  # the human submits a turn
    assert busy.returncode == 0 and busy.stdout == ""  # stdout would become prompt context
    assert na.is_busy(directory)
    (directory / "hint").write_text(HINT)  # a hint that reached the attempt anyway
    time.sleep(1)
    assert proc.poll() is None and (directory / "hint").exists()  # held while the turn runs
    na.write_state(directory, False)  # the turn ends
    assert proc.wait(5) == 2 and proc.stderr.read().strip() == HINT
    assert na.is_busy(directory)  # the woken turn is busy until its own Stop


def test_claude_announces_the_turn_end_at_stop_and_plain_ready_at_session_start(lodge):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "claude", "a1")["addr"])
    stand_in = Lodge()
    try:
        for event in ("SessionStart", "Stop"):
            proc = subprocess.Popen([sys.executable, str(na.ASSETS / "claude_rewake.py"), str(directory), "a1"],
                                    stdin=subprocess.PIPE, text=True, env={**os.environ, **stand_in.env})
            proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": event}))
            proc.stdin.close()
            deadline = time.monotonic() + 5
            while len(stand_in.seen) < (1 if event == "SessionStart" else 2) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not na.is_busy(directory)
        (directory / "retired").touch()
    finally:
        stand_in.server.shutdown()
    assert [body for _, _, body in stand_in.seen] == [{"session": "pal-x", "attempt": "a1"},
                                                     {"session": "pal-x", "attempt": "a1", "kind": "turn_end"}]
    assert {(path, token) for path, token, _ in stand_in.seen} == {("/inbox/ready", "tok")}


def test_the_codex_watcher_follows_its_own_thread_and_announces_each_turn_end(lodge, tmp_path, monkeypatch):
    from websockets.sync.server import unix_serve
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "codex", "a1")["addr"])
    stand_in = Lodge()
    for key, value in stand_in.env.items():
        monkeypatch.setenv(key, value)
    states, sock = [], str(directory / "app.sock")

    def status(thread, kind):
        return json.dumps({"method": "thread/status/changed", "params": {"threadId": thread, "status": {"type": kind}}})

    def app_server(conn):
        for raw in conn:
            if json.loads(raw).get("id") == 2:  # the TUI's first turn is still running when the watch starts
                conn.send(json.dumps({"id": 2, "result": {"thread": {"id": "t1", "status": {"type": "active"}}}}))
                break
        time.sleep(0.2)
        for frame in (status("title", "idle"), status("t1", "idle"), status("t1", "active"), status("t1", "idle")):
            conn.send(frame)
            time.sleep(0.2)
            states.append(na.engine_state(directory))
    known = []
    with unix_serve(app_server, sock) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:  # returns when the stand-in closes
            launch.watch_codex(sock, "t1", directory, "a1", on_known=lambda: known.append(na.engine_state(directory)))
        finally:
            server.shutdown()
            stand_in.server.shutdown()
    assert known == ["busy"]  # readiness is exposed only after a first, real status
    assert states == ["busy", "idle", "busy", "idle"]  # the helper thread's idle changes nothing
    assert [body.get("kind") for _, _, body in stand_in.seen] == ["turn_end", "turn_end"]  # one per active -> idle
    assert na.engine_state(directory) == "unknown"  # a lost watch is not idle


@pytest.mark.parametrize("reply, announced, after", [
    ({"result": {"thread": {"id": "t1", "status": {"type": "idle"}}}}, ["turn_end"], "idle"),  # missed while down
    ({"error": {"code": -1, "message": "no"}}, [], "unknown"),
    ({"result": {"thread": {"id": "t1"}}}, [], "unknown"),
])
def test_a_rewatch_wakes_once_on_a_known_idle_and_never_takes_a_bad_status_for_idle(lodge, monkeypatch, reply, announced,
                                                                                     after):
    from websockets.sync.server import unix_serve
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "codex", "a1")["addr"])
    stand_in, seen = Lodge(), []
    for key, value in stand_in.env.items():
        monkeypatch.setenv(key, value)

    def app_server(conn):
        for raw in conn:
            if json.loads(raw).get("id") == 2:
                conn.send(json.dumps({"id": 2, **reply}))
                time.sleep(0.3)
                seen.append(na.engine_state(directory))
                return
    with unix_serve(app_server, str(directory / "app.sock")) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            launch.watch_codex(str(directory / "app.sock"), "t1", directory, "a1")  # a reconnect: no on_known
        finally:
            server.shutdown()
            stand_in.server.shutdown()
    assert seen == [after]
    assert [body.get("kind") for _, _, body in stand_in.seen] == announced


def test_a_hint_from_before_the_turn_is_dropped_at_its_end_and_the_next_one_is_claimed(lodge):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "claude", "a1")["addr"])
    na.write_state(directory, True)  # a turn started before the listener took the hint placed just before it
    (directory / "hint").write_text("New inbox mail (stale)")
    proc = _listener(directory)
    proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "Stop"}))
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not (directory / "ready.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert proc.poll() is None and not (directory / "hint").exists()  # dropped, not claimed
    (directory / "hint").write_text(HINT)  # what the turn_end wake places if mail is still queued
    assert proc.wait(5) == 2 and proc.stderr.read().strip() == HINT


def test_a_session_start_never_overrides_the_opening_prompt_s_busy(lodge):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "claude", "a1")["addr"])
    na.write_state(directory, True)  # the opening prompt's UserPromptSubmit ran first
    proc = _listener(directory)
    proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "SessionStart"}))
    proc.stdin.close()
    deadline = time.monotonic() + 5
    while not (directory / "ready.json").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    (directory / "hint").write_text(HINT)
    time.sleep(0.6)
    assert na.is_busy(directory) and proc.poll() is None and (directory / "hint").exists()  # held until the turn ends
    (directory / "retired").touch()
    assert proc.wait(5) == 0


def test_a_turn_ended_by_an_api_error_clears_busy_and_announces_its_end(lodge):
    wolts, _ = lodge
    directory = Path(na.prepare(wolts, "pal-x", "claude", "a1")["addr"])
    stand_in = Lodge()
    try:
        na.write_state(directory, True)  # a turn that died on an API error: StopFailure, no Stop
        proc = subprocess.Popen([sys.executable, str(na.ASSETS / "claude_rewake.py"), str(directory), "a1"],
                                stdin=subprocess.PIPE, text=True, env={**os.environ, **stand_in.env})
        proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "StopFailure"}))
        proc.stdin.close()
        deadline = time.monotonic() + 5
        while not stand_in.seen and time.monotonic() < deadline:
            time.sleep(0.05)
        assert na.engine_state(directory) == "idle"
        assert [body.get("kind") for _, _, body in stand_in.seen] == ["turn_end"]
        (directory / "retired").touch()
        assert proc.wait(5) == 0
    finally:
        stand_in.server.shutdown()


def test_stop_failure_after_a_hint_turn_rearms_a_claiming_listener(lodge):  # reviewer's probe, StopFailure as Claude runs it
    _, live = lodge
    record = live("claude", ready={"armed_at": time.time()})
    directory = Path(record["native"]["addr"])
    stand_in = Lodge()
    try:
        proc = _listener(directory)
        proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "Stop"}))
        proc.stdin.close()
        deadline = time.monotonic() + 5
        while not (directory / "listener.json").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert na.deliver(record, HINT) == ("submitted", "claimed")
        assert proc.wait(5) == 2  # the hint-triggered turn has no remaining listener
        failed = subprocess.Popen([sys.executable, str(na.ASSETS / "claude_rewake.py"), str(directory), "a1"],
                                  stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  env={**os.environ, **stand_in.env})
        failed.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "StopFailure"}))
        failed.stdin.close()
        deadline = time.monotonic() + 5
        while na.is_busy(directory) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert na.deliver(record, HINT) == ("submitted", "claimed")
        assert failed.wait(5) == 2
    finally:
        na.retire(record["native"])
        stand_in.server.shutdown()


def test_an_interrupted_claude_turn_frees_the_session_after_its_busy_ages_out(lodge, monkeypatch):
    wolts, live = lodge
    record = live("claude", ready={"armed_at": time.time()})
    directory = Path(record["native"]["addr"])
    stand_in = Lodge()
    for key, value in stand_in.env.items():
        monkeypatch.setenv(key, value)
    try:
        proc = _listener(directory)  # armed at the last completed turn's Stop
        proc.stdin.write(json.dumps({"session_id": "s1", "hook_event_name": "Stop"}))
        proc.stdin.close()
        deadline = time.monotonic() + 5
        while len(stand_in.seen) < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        na.write_state(directory, True, ttl=1.5)  # the next turn, then Esc: no Stop, no StopFailure, no tool step
        assert na.deliver(record, HINT) == ("pending", "placed")  # left for a tool step that never comes
        assert proc.wait(8) == 2 and proc.stderr.read().strip() == HINT  # the aged busy's end: the listener takes it
        assert [body.get("kind") for _, _, body in stand_in.seen] == ["turn_end", "turn_end"]  # the aged busy's end
    finally:
        stand_in.server.shutdown()


def test_with_no_live_claude_listener_nothing_is_placed(lodge):
    _, live = lodge
    gone = subprocess.Popen(["true"])
    gone.wait()
    record = live("claude", ready={"armed_at": time.time(), "pid": gone.pid})  # it claimed and exited
    assert na.deliver(record, HINT) == ("pending", "listener_gone")
    assert not (Path(record["native"]["addr"]) / "hint").exists()


def test_a_busy_hint_turn_with_an_exited_listener_gets_the_next_hint_at_a_tool_step(lodge):
    _, live = lodge
    gone = subprocess.Popen(["true"])
    gone.wait()
    record = live("claude", ready={"armed_at": time.time(), "pid": gone.pid})
    directory = Path(record["native"]["addr"])
    na.write_state(directory, True, ttl=na.CLAUDE_BUSY_TTL)
    assert na.deliver(record, HINT) == ("pending", "placed")
    tool = _hook(directory, "PostToolUse", "--tool")
    assert tool.returncode == 0 and not (directory / "hint").exists()
    assert json.loads(tool.stdout) == {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": HINT}}
    again = _hook(directory, "PostToolUse", "--tool")  # nothing placed: a tool step adds nothing
    assert again.returncode == 0 and again.stdout == ""


def test_a_busy_codex_turn_is_steered_with_the_hint_and_a_refused_steer_waits_for_the_turn_end(lodge):
    from websockets.sync.server import unix_serve
    _, live = lodge
    record = live("codex", ready={"thread": "t1"})
    directory = Path(record["native"]["addr"])
    sock = str(directory / "app.sock")
    na.write_ready(directory, {"kind": record["native"]["kind"], "attempt": "a1", "sock": sock, "thread": "t1"})
    na.write_state(directory, True)
    steers, refuse, status = [], [], ["inProgress"]

    def app_server(conn):
        for raw in conn:
            message = json.loads(raw)
            if message.get("method") == "thread/turns/list":
                assert message["params"] == {"threadId": "t1", "limit": 1, "itemsView": "notLoaded"}
                conn.send(json.dumps({"id": message["id"], "result": {"data": [{"id": "turn-7", "status": status[0]}]}}))
            elif message.get("method") == "turn/steer":
                steers.append(message["params"])
                conn.send(json.dumps({"id": message["id"], **({"error": {"code": -1, "message": "not active"}}
                                                              if refuse else {"result": {"turnId": "turn-7"}})}))
            elif "id" in message:
                conn.send(json.dumps({"id": message["id"], "result": {}}))
    with unix_serve(app_server, sock) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            assert na.deliver(record, HINT) == ("submitted", "steered")
            refuse.append(True)  # the turn ends between the read and the steer
            assert na.deliver(record, HINT) == ("pending", "busy")
            status[0] = "completed"  # already over when read: nothing is steered
            assert na.deliver(record, HINT) == ("pending", "busy")
        finally:
            server.shutdown()
    assert steers[0] == {"threadId": "t1", "expectedTurnId": "turn-7", "input": [{"type": "text", "text": HINT}]}
    assert len(steers) == 2
