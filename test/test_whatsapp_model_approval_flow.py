"""Offline full real-test orchestration: synthetic inbound/spawn/provider only."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", [
    "sent", "heredoc", "approval_pending", "ambiguous", "extra_blank",
    "trailing_space", "leading_space", "extra_text", "crlf",
    "tool_sent", "tool_ambiguous", "tool_legacy", "tool_duplicate", "tool_prompt", "tool_timeout", "tool_spawn_ambiguous",
])
def test_real_runner_approval_flow_without_credentials_or_model(tmp_path, scenario):
    code = r'''
import asyncio, json, os, re, subprocess, sys, tempfile, threading, time, types
from pathlib import Path
from scripts import whatsapp_model_session_test as m
base, scenario = Path(sys.argv[1]), sys.argv[2]
tool_mode = scenario.startswith("tool_")
if tool_mode: scenario = scenario[5:]
private = base / "private"
private.mkdir(mode=0o700)
auth = base / "synthetic-auth"
auth.mkdir(mode=0o700)
m.owner_test.PRIVATE = private
m.owner_test._check_private_dir = lambda: None
m.owner_test._read_token = lambda: "synthetic-only-key"
m._auth_path = lambda *_: ("CODEX_HOME", auth)
def preflight(*args):
    Path(args[1]["CODEX_HOME"]).mkdir(mode=0o700)
    args[-1].passed("preflight")
m._preflight_harness = preflight
m._verify_private_tmux = lambda *_: None
m._harness_process_status = lambda *_: True
m._harness_readiness_status = lambda *_: "unsupported"
m._capture_boot_symptom = lambda *_, **__: "tool_approval_prompt" if scenario == "prompt" else m._classify_boot_symptom(m.NOTIFY_APPROVAL_INSTRUCTION)
real_cleanup = m._cleanup_resources
cleanup_requests = []
def cleanup(*args):
    cleanup_requests.append(args[-1])
    return real_cleanup(*args, stop_session_fn=lambda _: {"status": "stopped"}, kill_tmux_fn=lambda *_: True)
m._cleanup_resources = cleanup
m.TIMEOUT_SECONDS = 1
cli_threads, cli_results, sends, starts, expected_forwarded = [], [], [], [], []
real_wrap = m._wrap_notify_send
async def provider_send(*args, **kwargs):
    assert args[2] == expected_forwarded[0]  # Original CLI bytes preserved.
    sends.append(1)
    if scenario == "ambiguous":
        raise TimeoutError("synthetic-private-provider-error")
    return {"ok": True}
m._wrap_notify_send = lambda original, gate: real_wrap(provider_send, gate)
adapter = types.ModuleType("bot.whatsapp_adapter")
def load(path):
    return json.loads(path.read_text()) if path.exists() else {}
adapter.load = load
adapter.atomic_json = lambda path, data: path.write_text(json.dumps(data))
adapter.api = lambda *_: (200, {"messages": [{"id": "synthetic-control"}]})
def start(prompt, **kwargs):
    starts.append(1)
    assert prompt.endswith(m.NOTIFY_OWNER_INSTRUCTION if tool_mode else m.NOTIFY_APPROVAL_INSTRUCTION)
    if tool_mode:
        assert os.environ["WOLTSPACE_NOTIFY_TOOL"] == "1"
        assert m.NOTIFY_APPROVAL_INSTRUCTION not in prompt
    assert "WHATSAPP_AGENT_TOKEN" not in os.environ
    assert kwargs["routing"]["whatsapp_account"] == adapter.ACCOUNT
    if scenario == "spawn_ambiguous":
        raise TimeoutError("synthetic-private-ambiguous-start")
    nonce = re.search(r"wa-model-check-[0-9a-f]+", prompt).group()
    suffixes = {"extra_blank": "\n\n", "trailing_space": " ", "extra_text": "\nextra", "crlf": "\r\n"}
    body = (" " if scenario == "leading_space" else "") + nonce + suffixes.get(scenario, "")
    expected_forwarded.append(m._expected_notify_payload(nonce) + ("" if scenario == "sent" or tool_mode else "\n"))
    from sessions import SessionRegistry
    registry = SessionRegistry()
    record = registry.create(
        "wa-test-offline-model", wolt="wa-test", harness="codex", creature="otter",
        dir=str(Path(os.environ["WOLTS_DIR"]) / "wa-test"),
        adapter="whatsapp", chat_id="user:4242424242", whatsapp_account=adapter.ACCOUNT,
        execution_policy={"mode": "prompt", "isolation": "host"})
    record = registry.update(record["name"], wolt="wa-test", notify_owner_generation="synthetic-generation", runtime={
        "kind": "tmux", "tmux_session_name": "synthetic-model", "pane_id": "%1"})
    if scenario not in {"approval_pending", "prompt", "timeout"}:
        def cli():
            # Arrives after initial selection budget would have expired, but
            # within the new full budget from verified attach readiness.
            time.sleep(0.6)
            env = dict(os.environ)
            env["WOLTSPACE_WOLT_SESSION"] = record["name"]
            from session_runtime import _session_env_value
            env["PATH"] = _session_env_value("PATH", env["PATH"])
            argv = [str(m.ROOT / "container/bin/notify"), "--whatsapp", "user:4242424242"]
            if tool_mode and scenario != "legacy":
                import notify_owner_mcp as worker
                command = [sys.executable, "-I", worker.__file__, "--session", record["name"],
                           "--binding", worker.route_binding(record), "--api", env["WOLTSPACE_API"]]
                call = {"jsonrpc":"2.0", "id":1, "method":"tools/call",
                        "params":{"name":"notify_owner", "arguments":{"message":nonce}}}
                stdin = json.dumps(call) + "\n"
                if scenario in {"ambiguous", "duplicate"}:
                    stdin += json.dumps(dict(call, id=2)) + "\n"
            elif scenario in {"sent", "legacy"}:
                command, stdin = argv, nonce
            else:
                # The actual supported shell path, including the literal
                # heredoc's required terminal LF, rather than newline-free input.
                from container.lib.notify_prompt import quoted_heredoc
                command, stdin = ["bash", "-c", quoted_heredoc(argv, body, prefix="WOLTSPACE_NOTIFY")], None
            result = subprocess.run(command, input=stdin, env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            cli_results.append(result.returncode)
            if tool_mode and scenario != "legacy":
                values = [json.loads(line) for line in result.stdout.splitlines()]
                assert len(values) == (2 if scenario in {"ambiguous", "duplicate"} else 1)
                assert values[0]["result"]["isError"] is (scenario == "ambiguous")
                if scenario in {"ambiguous", "duplicate"}:
                    assert values[1]["result"]["isError"] is True
        thread = threading.Thread(target=cli)
        thread.start()
        cli_threads.append(thread)
    return {"name": record["name"], "url": "http://synthetic.invalid/session"}
adapter.start_claude_session = start
def handle(item, routes, activation):
    participant, body = item["from"], item["text"]["body"]
    if body == "wolt wa-test":
        routes[participant] = {"wolt": "wa-test"}
        adapter.api("POST", "/messages", {"text": {"body": "Selected wa-test. Send a message."}})
    else:
        record = adapter.start_claude_session(body, wolt="wa-test", routing={
            "adapter": "whatsapp", "chat_id": participant, "whatsapp_account": adapter.ACCOUNT})
        routes[participant]["session"] = record["name"]
        adapter.api("POST", "/messages", {"text": {"body": "Session started: synthetic"}})
    adapter.atomic_json(adapter.ROUTES, routes)
adapter.handle = handle
challenge = []
real_say = m._say
def say(text):
    if text.startswith("Then send exactly: `"):
        challenge.append(text[len("Then send exactly: `"):-1])
    real_say(text)
m._say = say
def poll(state, routes, activation, *, cursor_path, dispatch):
    step = state.get("step", 0)
    if step < 2:
        if step == 0:
            time.sleep(0.7)  # Consume selection time before session exists.
        dispatch({"from": "user:4242424242", "text": {"body":
                  "wolt wa-test" if step == 0 else challenge[0]}}, routes, activation)
        state["step"] = step + 1
    else:
        time.sleep(0.02)
    return state
adapter.poll_once = poll
sys.modules["bot.whatsapp_adapter"] = adapter
diag = m.RunDiagnostics("codex")
with tempfile.TemporaryDirectory(prefix=".wma-offline-", dir=m.ROOT.parents[2]) as sockets:
    m.PRIVATE_TMUX_BASE = Path(sockets)
    try:
        result = m._run_model_test("codex", diag, notify_owner_tool=tool_mode)
    except Exception:
        result = 2
    for thread in cli_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
record = diag.as_record()
assert m._valid_diagnostic_record(record)
assert starts == [1]
assert cleanup_requests == [True]  # Includes throws before any returned session ID.
assert record["checks"]["cleanup"] == "passed"
assert adapter.start_claude_session is start and adapter.TOKEN == ""
if scenario in {"sent", "heredoc"}:
    assert result == 0 and sends == [1] and cli_results == [0]
    assert record["notify_attempt"] == "sent" and record["checks"]["notify"] == "passed"
    assert record["notify_rejection"] == "none"
    assert record["notify_payload_shape"] == ("exact" if scenario == "sent" else "exact_plus_terminal_lf")
    assert not list(private.glob("model-*"))
elif tool_mode:
    assert result == 2 and record["checks"]["notify"] != "passed"
    expected = {"ambiguous":"notify_ambiguous", "legacy":"notify_rejected",
                "duplicate":"notify_rejected", "prompt":"tool_approval_prompt", "timeout":"run_timeout", "spawn_ambiguous":"session_not_started"}
    assert record["category"] == expected[scenario], record
    assert sends == ([1] if scenario in {"ambiguous", "duplicate"} else [])
    files = list(private.glob("model-*/failure.json"))
    assert len(files) == 1 and m._valid_diagnostic_record(json.loads(files[0].read_text()))
    assert all(term not in files[0].read_text() for term in ("synthetic-only-key", "user:4242424242", "wa-model-check-", "synthetic-private-provider-error"))
    assert {p.name for p in files[0].parent.iterdir()} == {"failure.json"}
else:
    assert result == 2 and record["checks"]["notify"] != "passed"
    assert record["category"] == ("approval_not_completed" if scenario == "approval_pending" else "notify_ambiguous" if scenario == "ambiguous" else "notify_rejected")
    assert sends == ([1] if scenario == "ambiguous" else [])
    assert record["notify_rejection"] == ("none" if scenario in {"approval_pending", "ambiguous"} else "payload_mismatch")
    assert record["notify_payload_shape"] == ("not_seen" if scenario == "approval_pending" else "exact_plus_terminal_lf" if scenario == "ambiguous" else "other")
    if scenario == "approval_pending":
        assert record["harness_readiness"] == "unknown" and cli_results == []
    files = list(private.glob("model-*/failure.json"))
    assert len(files) == 1 and m._valid_diagnostic_record(json.loads(files[0].read_text()))
    assert all(term not in files[0].read_text() for term in ("synthetic-only-key", "user:4242424242", "wa-model-check-", "synthetic-private-provider-error"))
    assert {p.name for p in files[0].parent.iterdir()} == {"failure.json"}
assert "WHATSAPP_AGENT_TOKEN" not in os.environ
print("offline_model_approval_ok")
'''
    env = dict(os.environ)
    env.pop("WHATSAPP_AGENT_TOKEN", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), scenario],
                            cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=35)
    assert result.returncode == 0, result.stderr
    assert "offline_model_approval_ok" in result.stdout
    if scenario.startswith("tool_"):
        assert "Approval needed:" not in result.stdout
        assert "attach-session" not in result.stdout
        assert "Approve only" not in result.stdout
        assert "Model test cleanup=passed." in result.stdout
        if scenario == "tool_sent":
            assert "Owner-tool requests=1 legacy-notify=0." in result.stdout
    else:
        assert "Approval needed:" in result.stdout
        assert "attach-session -t =synthetic-model" in result.stdout
        assert "Approve only the intended notify command" in result.stdout
    assert "synthetic-private-provider-error" not in result.stdout + result.stderr
    if scenario == "approval_pending":
        assert "This does not prove approval was denied. Cleaning up." in result.stdout
