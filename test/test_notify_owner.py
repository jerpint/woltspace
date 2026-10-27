"""Offline owner tool tests: no models, real credentials or provider calls."""
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container/lib"))
import notify_owner_mcp as worker
import harnesses
import sessions
from server import notify, owner_notify


def rpc(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def run_worker(requests, send):
    input_stream = io.BytesIO(b"".join(json.dumps(r).encode()+b"\n" for r in requests))
    output = io.BytesIO()
    worker.serve(input_stream, output, send)
    return [json.loads(line) for line in output.getvalue().splitlines()]


def call(message="hello", **extra):
    return rpc("tools/call", {"name": "notify_owner", "arguments": {"message": message, **extra}})


def test_protocol_schema_and_literal_text():
    sent = []
    result = run_worker([
        rpc("initialize", {"protocolVersion": "2025-03-26"}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc("tools/list"), call("literal $HOME `whoami`\n"), rpc("ping"),
    ], lambda text: sent.append(text) or "sent")
    assert result[0]["result"]["protocolVersion"] == "2025-03-26"
    tools = result[1]["result"]["tools"]
    assert len(tools) == 1 and tools[0]["name"] == "notify_owner"
    assert set(tools[0]["inputSchema"]["properties"]) == {"message"}
    assert tools[0]["inputSchema"]["additionalProperties"] is False
    assert sent == ["literal $HOME `whoami`\n"]
    assert result[2]["result"]["isError"] is False


@pytest.mark.parametrize("extra", [
    {"session": "other"}, {"binding": "other"}, {"url": "http://localhost"},
    {"participant": "user:2"}, {"channel": "telegram"}, {"file": "/tmp/private"},
    {"command": "echo bad"},
])
def test_no_route_or_execution_inputs(extra):
    result = run_worker([call(**extra)], lambda _: pytest.fail("send reached"))
    assert result[0]["error"]["code"] == -32602


@pytest.mark.parametrize("message", ["", "  \n", None, 1, [], "x"*4097, "\ud800"])
def test_invalid_message(message):
    result = run_worker([call(message)], lambda _: pytest.fail("send reached"))
    assert result[0]["error"]["code"] == -32602


def test_ambiguous_result_disables_further_attempts():
    attempts = []
    result = run_worker([call(), call(), call("new text")],
                        lambda text: attempts.append(text) or "ambiguous")
    assert attempts == ["hello"]
    assert all(r["result"]["isError"] for r in result)
    assert "Do not resend" in result[0]["result"]["content"][0]["text"]


def test_cancellation_does_not_undo_admission_or_repeat_send():
    attempts = []
    result = run_worker([call(), {"jsonrpc":"2.0", "method":"notifications/cancelled",
        "params":{"requestId":1}}, call("do not repeat")],
        lambda text: attempts.append(text) or "sent")
    assert attempts == ["hello"]
    assert result[0]["result"]["isError"] is False  # admitted send can finish
    assert result[1]["result"]["isError"] is True
    assert "ambiguous" in result[1]["result"]["content"][0]["text"]


def test_new_worker_is_not_durable_deduplication():
    attempts = []
    # Restarting loses in-memory ambiguity suppression; callers must not
    # resubmit an unknown outcome just because a new worker is available.
    run_worker([call()], lambda text: attempts.append(text) or "ambiguous")
    run_worker([call()], lambda text: attempts.append(text) or "sent")
    assert attempts == ["hello", "hello"]


def test_frame_limit_invalid_json_and_eof():
    output = io.BytesIO()
    worker.serve(io.BytesIO(b"x"*(worker.MAX_FRAME_BYTES+1)), output,
                 lambda _: pytest.fail("send reached"))
    assert len(output.getvalue().splitlines()) == 1
    assert json.loads(output.getvalue())["error"]["code"] == -32600
    output = io.BytesIO()
    worker.serve(io.BytesIO(b"{\n"), output, lambda _: pytest.fail("send reached"))
    assert json.loads(output.getvalue())["error"]["code"] == -32700


@pytest.mark.parametrize("endpoint", [
    "https://api.whatsapp.com", "http://example.com", "http://localhost:1/path",
    "http://name:secret@localhost", "http://localhost?key=secret", "file:///tmp/x",
])
def test_endpoint_fixed_loopback_only(endpoint):
    with pytest.raises(ValueError):
        worker.local_endpoint(endpoint)


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    (wolts / "wa-test").mkdir(parents=True)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(notify, "WOLTS_DIR", wolts)
    monkeypatch.setattr(notify, "append_chat_history", lambda *a: None)
    env = {"WOLTSPACE_NOTIFY_TOOL": "1", "WHATSAPP_AGENT_TOKEN": "synthetic-key"}
    monkeypatch.setattr(notify, "dotenv_env", lambda key: env.get(key, ""))
    monkeypatch.setenv("WOLTSPACE_NOTIFY_TOOL", "1")
    monkeypatch.setattr(harnesses, "codex_supports_no_daemon", lambda: True)
    registry = sessions.SessionRegistry(wolts)
    record = registry.create("wa-test-session", wolt="wa-test", creature="otter",
        harness="codex", dir=str(wolts/"wa-test"), execution_policy="prompt",
        adapter="whatsapp", chat_id="user:123",
        whatsapp_account=hashlib.sha256(env["WHATSAPP_AGENT_TOKEN"].encode()).hexdigest()[:20])
    record = registry.update(record["name"], wolt="wa-test", notify_owner_generation="synthetic-generation")
    sent = AsyncMock()
    monkeypatch.setattr(notify, "whatsapp_send", sent)
    return registry, record, env, sent


def body(record, message="hello"):
    return {"session": record["name"], "binding": worker.route_binding(record), "message": message}


@pytest.mark.asyncio
async def test_real_registry_to_real_sender_boundary(lodge):
    registry, record, env, sent = lodge
    assert await owner_notify.deliver(body(record, "literal\n")) == "sent"
    sent.assert_awaited_once_with("synthetic-key", "user:123", "🦦 wa-test: literal\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"status": "stopped"}, {"status": "completed"}, {"adapter": "telegram"},
    {"chat_id": "user:999"}, {"whatsapp_account": "rotated"},
    {"notify_owner_generation": "relaunch"}, {"wolt": "other"}, {"harness": "claude"},
])
async def test_stale_binding_never_sends_or_falls_back(lodge, change):
    registry, record, env, sent = lodge
    # Mutate the authoritative store; storage may be JSON or SQLite.
    fields = dict(change)
    if "wolt" in fields:
        other = registry.wolts_dir / fields.pop("wolt")
        other.mkdir()
        # Change the canonical owner, not a legacy alias normalized on read.
        fields["target"] = {"wolt_id": other.name, "canonical_workdir": str(other)}
    assert registry.update(record["name"], wolt=record["wolt"], **fields) is not None
    stored = registry.get(record["name"], check_alive=False)
    assert stored is not None
    assert all(stored[key] == value for key, value in change.items())
    assert await owner_notify.deliver(body(record)) == "stale_binding"
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_key_rotation_feature_disable_and_text_bounds(lodge):
    registry, record, env, sent = lodge
    env["WHATSAPP_AGENT_TOKEN"] = "rotated-key"
    assert await owner_notify.deliver(body(record)) == "stale_binding"
    env["WOLTSPACE_NOTIFY_TOOL"] = "0"
    assert await owner_notify.deliver(body(record)) == "unavailable"
    env["WOLTSPACE_NOTIFY_TOOL"] = "1"
    env["WHATSAPP_AGENT_TOKEN"] = "synthetic-key"
    assert await owner_notify.deliver(body(record, "x"*4096)) == "invalid_message"  # prefix counts
    assert await owner_notify.deliver(dict(body(record), adapter="telegram")) == "invalid_message"
    sent.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_is_sanitized_not_retried(lodge):
    registry, record, env, sent = lodge
    sent.side_effect = TimeoutError("synthetic-secret participant message")
    assert await owner_notify.deliver(body(record)) == "ambiguous"
    assert sent.await_count == 1
    assert not owner_notify._pending


@pytest.mark.asyncio
async def test_parallel_busy_and_release(lodge):
    _, record, _, sent = lodge
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(*args):
        started.set()
        await release.wait()
    sent.side_effect = delayed
    task = asyncio.create_task(owner_notify.deliver(body(record)))
    await started.wait()
    assert await owner_notify.deliver(body(record)) == "busy"
    release.set()
    assert await task == "sent"
    assert not owner_notify._pending


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"status": "stopped"}, {"notify_owner_generation": "resumed"},
                                    {"chat_id": "user:999"}])
async def test_accepted_rate_wait_is_in_flight_future_calls_revoked(lodge, monkeypatch, change):
    registry, record, _, sent = lodge
    accepted, release = asyncio.Event(), asyncio.Event()
    submissions = []
    async def delayed(adapter, session, message, chat_id, **kwargs):
        accepted.set()  # simulates sender's rate wait before actual submission
        await release.wait()
        submissions.append((chat_id, kwargs["routing"]["notify_owner_generation"]))
    monkeypatch.setattr(notify, "_send", delayed)
    task = asyncio.create_task(owner_notify.deliver(body(record)))
    await accepted.wait()
    registry.update(record["name"], wolt="wa-test", **change)
    assert await owner_notify.deliver(body(record)) == "stale_binding"
    release.set()
    assert await task == "sent"
    assert submissions == [("user:123", "synthetic-generation")]
    sent.assert_not_awaited()


def test_http_endpoint_frame_schema_and_body_blind_response(lodge):
    from starlette.testclient import TestClient
    from server.app import app
    _, record, _, sent = lodge
    client = TestClient(app, base_url="http://localhost")  # no lifespan/start of connectors
    assert client.post("/notify/owner", json=body(record)).json() == {"status": "sent"}
    assert client.post("/notify/owner", json=dict(body(record), url="http://elsewhere")).json() == {"status": "invalid_message"}
    assert client.post("/notify/owner", content=b"{").status_code == 400
    assert client.post("/notify/owner", content=b"x"*32769).status_code == 413
    assert sent.await_count == 1


def test_prepare_stamps_and_revokes_generation(lodge, monkeypatch):
    registry, record, _, _ = lodge
    checked = []
    monkeypatch.setattr(worker, "verify_codex_definition", lambda *args: checked.append(args))
    monkeypatch.setattr(sessions, "_assemble_spawn_prompt", lambda *args: "synthetic prompt")
    first = sessions.prepare_session_command(record["name"], "spawn")
    current = registry.get(record["name"], check_alive=False)
    assert current["notify_owner_generation"] != record["notify_owner_generation"]
    assert worker.route_binding(current) in first
    assert "--dangerously" not in first and "approval_mode" in first
    assert "synthetic-key" not in first and "user:123" not in first
    registry.update(record["name"], wolt="wa-test", harness_session_id="synthetic-rollout")
    second = sessions.prepare_session_command(record["name"], "resume")
    assert len(checked) == 2
    latest = registry.get(record["name"], check_alive=False)
    assert latest["notify_owner_generation"] != current["notify_owner_generation"]
    assert worker.route_binding(latest) in second


def test_failed_resume_does_not_revoke_existing_worker(lodge, monkeypatch):
    registry, record, _, _ = lodge
    monkeypatch.setattr(worker, "verify_codex_definition", lambda *args: None)
    monkeypatch.setattr(sessions, "_tmux_alive", lambda *args: False)
    with pytest.raises(ValueError, match="no harness_session_id"):
        sessions.prepare_session_command(record["name"], "resume")
    assert registry.get(record["name"], check_alive=False)["notify_owner_generation"] == record["notify_owner_generation"]


def test_missing_persisted_binding_refuses_harness_launch(lodge, monkeypatch):
    _, record, _, _ = lodge
    monkeypatch.setattr(worker, "verify_codex_definition", lambda *args: None)
    monkeypatch.setattr(sessions, "_assemble_spawn_prompt", lambda *args: "synthetic")
    monkeypatch.setattr(sessions.SessionRegistry, "update", lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="binding was not persisted"):
        sessions.prepare_session_command(record["name"], "spawn")


@pytest.mark.parametrize("flag,adapter", [("0", "whatsapp"), ("", "whatsapp"), ("1", "telegram"), ("1", "")])
def test_optional_and_other_sessions_keep_original_command(lodge, monkeypatch, flag, adapter):
    registry, record, _, _ = lodge
    monkeypatch.setenv("WOLTSPACE_NOTIFY_TOOL", flag)
    registry.update(record["name"], wolt="wa-test", adapter=adapter)
    monkeypatch.setattr(worker, "verify_codex_definition", lambda *a: pytest.fail("tool touched"))
    monkeypatch.setattr(sessions, "_assemble_spawn_prompt", lambda *args: "synthetic prompt")
    command = sessions.prepare_session_command(record["name"], "spawn")
    assert "mcp_servers" not in command
    assert "--dangerously" not in command


def test_worker_path_must_be_outside_writable_roots(lodge):
    _, record, _, _ = lodge
    with pytest.raises(ValueError, match="writable"):
        worker.codex_overrides(record, "http://localhost:7777", [Path(worker.__file__).parent])


def test_only_exact_owner_tool_has_explicit_permission(lodge):
    import tomllib
    _, record, _, _ = lodge
    args = worker.codex_overrides(record, "http://localhost:7777", [])
    definition = tomllib.loads(args[1])["mcp_servers"][worker.SERVER_NAME]
    assert definition["enabled_tools"] == ["notify_owner"]
    assert definition["tools"] == {"notify_owner": {"approval_mode": "approve"}}
    assert "default_tools_approval_mode" not in definition
    assert "approvals_reviewer" not in args[1] and "approval_policy" not in args[1]
    tool = run_worker([rpc("tools/list")], lambda _: pytest.fail("send reached"))[0]["result"]["tools"][0]
    assert tool["annotations"]["readOnlyHint"] is False


def test_preflight_scrubs_secret_and_rejects_old_codex(tmp_path, monkeypatch):
    import tomllib
    overrides = worker.codex_overrides({"name": "synthetic"}, "http://localhost:7777", [tmp_path])
    definition = tomllib.loads(overrides[1])["mcp_servers"][worker.SERVER_NAME]
    observed = []
    version = ["codex-cli 0.163.0"]
    def run(argv, **kwargs):
        observed.append(kwargs["env"])
        if "--version" in argv:return SimpleNamespace(stdout=version[0])
        return SimpleNamespace(stdout=json.dumps({"enabled": True, "disabled_reason": None,
            "enabled_tools": [worker.TOOL_NAME], "disabled_tools": [],
            "startup_timeout_sec": 10, "tool_timeout_sec": 120,
            "transport": {"type": "stdio", **{key:definition[key] for key in
                ("command", "args", "cwd", "env", "env_vars")}}}))
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-private")
    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.verify_codex_definition(overrides, "/trusted/wcodex", tmp_path)
    assert all("WHATSAPP_AGENT_TOKEN" not in env for env in observed)
    assert "synthetic-private" not in overrides[1]
    version[0] = "codex-cli 0.162.0"
    with pytest.raises(ValueError, match="0.163"):
        worker.verify_codex_definition(overrides, "/trusted/wcodex", tmp_path)


def test_tmux_carries_tool_opt_in_without_provider_secret(monkeypatch):
    from session_runtime import TmuxSessionRuntime
    monkeypatch.setenv("WOLTSPACE_NOTIFY_TOOL", "1")
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-private")
    command = TmuxSessionRuntime._launch_command("run-session")
    assert "WOLTSPACE_NOTIFY_TOOL=1" in command
    assert "synthetic-private" not in command
    assert "WHATSAPP_AGENT_TOKEN" not in TmuxSessionRuntime._tmux_environment()


def test_effective_config_precedence_and_nested_env_fail_closed(tmp_path, monkeypatch):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("installed Codex CLI required for offline parser test")
    home, work, config = (tmp_path/name for name in ("home", "workspace", "codex"))
    for path in (home, work, config):path.mkdir()
    monkeypatch.setenv("HOME", str(home));monkeypatch.setenv("CODEX_HOME", str(config))
    monkeypatch.setenv("WHATSAPP_AGENT_TOKEN", "synthetic-key-not-for-child")
    overrides = worker.codex_overrides({"name": "synthetic"}, "http://127.0.0.1:19876", [work])
    # Project config is trusted for this synthetic cwd, to actually test its layer.
    (config/"config.toml").write_text(f'[projects.{json.dumps(str(work))}]\ntrust_level="trusted"\n')
    (work/".codex").mkdir()
    project = work/".codex/config.toml"
    project.write_text('[mcp_servers.woltspace_notify]\ncommand="/synthetic/wrong"\nargs=["wrong"]\nenabled=false\nenabled_tools=["wrong"]\n[mcp_servers.woltspace_notify.tools.notify_owner]\napproval_mode="prompt"\n')
    worker.verify_codex_definition(overrides, codex, work)
    result = subprocess.run([codex, *overrides, "mcp", "get", worker.SERVER_NAME, "--json"],
                            cwd=work, capture_output=True, text=True, check=True, timeout=15)
    value = json.loads(result.stdout)
    assert value["transport"]["args"][0] == "-I"
    assert value["transport"]["args"][1] == str(Path(worker.__file__).resolve())
    assert value["enabled_tools"] == ["notify_owner"]
    # Per-tool mode is not exposed in mcp get. Check parser/override controls,
    # while reserving real approval behavior for a reviewed runtime test.
    project.write_text('[mcp_servers.woltspace_notify]\ncommand="/synthetic/wrong"\n[mcp_servers.woltspace_notify.tools.notify_owner]\napproval_mode="synthetic_invalid"\n')
    worker.verify_codex_definition(overrides, codex, work)
    invalid = [overrides[0], overrides[1].replace('approval_mode = "approve"', 'approval_mode = "synthetic_invalid"')]
    rejected = subprocess.run([codex, *invalid, "mcp", "get", worker.SERVER_NAME, "--json"],
                              cwd=work, capture_output=True, timeout=15)
    assert rejected.returncode != 0
    # A nested map is merged, not replaced. Refuse inherited additions.
    project.write_text('[mcp_servers.woltspace_notify]\ncommand="/synthetic/wrong"\nenv={SYNTHETIC_REPLACE="wrong"}\n')
    with pytest.raises(ValueError, match="unmodified effective"):
        worker.verify_codex_definition(overrides, codex, work)


def test_actual_worker_process_local_mock_http_and_eof(lodge):
    _, record, _, sent = lodge
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, request))
            status = asyncio.run(owner_notify.deliver(request))
            raw = json.dumps({"status": status}).encode()
            self.send_response(200);self.send_header("Content-Length", str(len(raw)))
            self.end_headers();self.wfile.write(raw)
        def log_message(self, *args):pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
    try:
        args = [sys.executable, "-I", str(Path(worker.__file__).resolve()),
                "--session", record["name"], "--binding", worker.route_binding(record),
                "--api", f"http://127.0.0.1:{server.server_port}"]
        child_env = {key:os.environ[key] for key in ("PATH",) if key in os.environ}
        child_env["PYTHONPATH"] = "/synthetic/ignored"
        raw = b"".join(json.dumps(r).encode()+b"\n" for r in [rpc("initialize"), call("quoted 'text'\n")])
        result = subprocess.run(args, input=raw, capture_output=True, env=child_env, timeout=10)
        assert result.returncode == 0 and not result.stderr
        values = [json.loads(line) for line in result.stdout.splitlines()]
        assert values[-1]["result"]["isError"] is False
        assert requests == [("/notify/owner", body(record, "quoted 'text'\n"))]
        sent.assert_awaited_once_with("synthetic-key", "user:123", "🦦 wa-test: quoted 'text'\n")
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
    assert not thread.is_alive()


def test_wall_deadline_even_during_rate_wait_prevents_repeat(monkeypatch):
    release, finished = threading.Event(), threading.Event()
    attempts = []
    def delayed(*args):
        attempts.append(True)
        release.wait(2)
        finished.set()
        return "sent"  # server may have sent after caller's deadline
    monkeypatch.setattr(worker, "_post_message_once", delayed)
    monkeypatch.setattr(worker, "REQUEST_DEADLINE_SECONDS", 0.02)
    start = time.monotonic()
    try:
        values = run_worker([call(), call()], lambda text: worker.post_message(
            "synthetic", "a"*64, "http://localhost:1", text))
        assert time.monotonic()-start < 1
        assert attempts == [True]
        assert all(value["result"]["isError"] for value in values)
    finally:
        release.set()
        assert finished.wait(2)


def test_intended_profile_protects_install_and_project_config(tmp_path, monkeypatch):
    codex = shutil.which("codex")
    if not codex:
        pytest.skip("installed Codex CLI required for offline sandbox check")
    home, config, work, install = (tmp_path/name for name in ("home", "codex", "workspace", "install"))
    for path in (home, config, work, install):path.mkdir()
    packaged = install/"notify_owner_mcp.py"
    shutil.copyfile(worker.__file__, packaged)
    (work/".codex").mkdir()
    project_config = work/".codex/config.toml"
    project_config.write_text("# synthetic config\n")
    env = {"PATH":os.environ["PATH"], "HOME":str(home), "CODEX_HOME":str(config)}
    probe = '''import json,os,sys
out={}
for key,path in zip(("worker","config"),sys.argv[1:]):
    fd=None
    try:
        fd=os.open(path,os.O_WRONLY)  # no truncation or write, even if allowed
        out[key]="writable"
    except OSError as e:
        out[key]="denied" if e.errno in (1,13,30) else "unproven"
    finally:
        if fd is not None:os.close(fd)
for key,path in zip(("worker_parent_rename","config_parent_rename"),sys.argv[1:]):
    parent=__import__('pathlib').Path(path).parent
    moved=parent.with_name(parent.name+"-proof-renamed")
    try:
        os.rename(parent,moved)
        os.rename(moved,parent)
        out[key]="allowed"
    except OSError as e:
        out[key]="denied" if e.errno in (1,13,16,30) else "unproven"
print(json.dumps(out))
'''
    result = subprocess.run([codex, "sandbox", "--permission-profile", ":workspace",
        "--include-managed-config", "-C", str(work), "--", sys.executable, "-c", probe,
        str(packaged), str(project_config)], env=env, cwd=work,
        capture_output=True, text=True, timeout=20)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"worker": "denied", "config": "denied",
        "worker_parent_rename": "denied", "config_parent_rename": "denied"}
    assert packaged.read_bytes() == Path(worker.__file__).read_bytes()
    assert project_config.read_text() == "# synthetic config\n"


def test_declared_extra_writable_root_refused_even_readonly_file(tmp_path, monkeypatch):
    install, work = tmp_path/"install", tmp_path/"work"
    install.mkdir();work.mkdir()
    target = install/"worker.py";target.write_text("# synthetic worker\n");target.chmod(0o444)
    monkeypatch.setattr(worker, "__file__", str(target))
    worker.codex_overrides({"name":"synthetic"}, "http://localhost:7777", [work])
    with pytest.raises(ValueError, match="writable"):
        # Explicit roots, not discovery of arbitrary configured roots.
        worker.codex_overrides({"name":"synthetic"}, "http://localhost:7777", [work, install])
    assert target.read_text() == "# synthetic worker\n"


def test_symlink_alias_cannot_redirect_fixed_resolved_worker(tmp_path, monkeypatch):
    install, workspace = tmp_path/"install", tmp_path/"workspace"
    install.mkdir();workspace.mkdir()
    target = install/"worker.py";target.write_text("# trusted synthetic\n")
    alias = workspace/"alias.py";alias.symlink_to(target)
    monkeypatch.setattr(worker, "__file__", str(alias))
    args = worker.codex_overrides({"name":"synthetic"}, "http://localhost:7777", [workspace])
    assert str(target.resolve()) in args[1] and str(alias) not in args[1]
    alias.unlink();alias.write_text("# changed alias\n")
    assert str(target.resolve()) in args[1]
    monkeypatch.setattr(worker, "__file__", str(alias))
    with pytest.raises(ValueError, match="writable"):
        worker.codex_overrides({"name":"synthetic"}, "http://localhost:7777", [workspace])


def test_packaging_and_boot_guidance():
    packaging = (ROOT/"pyproject.toml").read_text()
    assert '"container" = "woltspace/_bundle/container"' in packaging
    assert '"/container"' in packaging
    for path in [ROOT/"container/skills/notify/SKILL.md", ROOT/"container/skills/start-chat/modes/whatsapp.md"]:
        assert "notify_owner" in path.read_text()
    from notify_prompt import notify_reply_instruction
    assert notify_reply_instruction("--whatsapp", "user:123").startswith("If this session exposes notify_owner")
    assert "notify_owner" not in notify_reply_instruction("--telegram", "123")
