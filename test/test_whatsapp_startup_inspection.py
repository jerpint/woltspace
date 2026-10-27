"""Offline orchestration: real notify CLI/API, synthetic registry, no model/provider."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_status_line_contains_only_fixed_diagnostics():
    from scripts import whatsapp_startup_inspection as startup
    diagnostics = startup.model.RunDiagnostics("codex")
    diagnostics.set_harness_process("present")
    diagnostics.set_harness_readiness("unsupported")
    assert startup._status_line(diagnostics) == (
        "Startup inspection process=present readiness=unsupported symptom=not_checked "
        "notify_attempt=not_seen cleanup=not_reached."
    )


def test_startup_uses_supported_session_creation_with_prompt_policy(monkeypatch):
    from scripts import whatsapp_startup_inspection as startup
    import sessions

    calls = []
    monkeypatch.setattr(sessions, "start_session", lambda **kw: calls.append(kw) or {"name": "synthetic"})
    assert startup._start_session() == {"name": "synthetic"}
    assert calls[0]["execution_policy"] == "prompt"
    assert calls[0]["harness"] == "codex"
    assert calls[0]["prompt"] == startup.PROMPT
    assert calls[0]["routing"]["chat_id"] == startup.PARTICIPANT


def test_approval_prompt_preserves_one_attempt_and_normal_policy():
    from scripts import whatsapp_startup_inspection as startup
    prompt = startup.APPROVAL_PROBE_PROMPT
    assert prompt.startswith(startup.PROBE_PROMPT)
    for text in ("BEFORE the first helper invocation", "sandbox_permissions=require_escalated",
                 "Wait for the current reviewer", "persistent prefix grant",
                 "Never retry an ambiguous send", "stop"):
        assert text in prompt
    skill = (ROOT / "container/skills/notify/SKILL.md").read_text()
    assert "model's shell does not need direct WhatsApp access" in skill
    assert "Existing feature authorization does not" in skill


def test_owner_tool_prompt_no_cli_escalation_or_route_input():
    from scripts import whatsapp_startup_inspection as startup
    assert "notify_owner MCP tool" in startup.OWNER_TOOL_PROMPT
    assert "not the notify CLI or shell" in startup.OWNER_TOOL_PROMPT
    assert "sandbox_permissions" not in startup.OWNER_TOOL_PROMPT
    assert startup.PARTICIPANT not in startup.OWNER_TOOL_PROMPT


def test_exact_mcp_approval_snapshot_wording_is_actionable(monkeypatch):
    from scripts import whatsapp_startup_inspection as startup
    question = 'Allow the woltspace_notify MCP server to run tool "notify_owner"?'
    text = ('• Calling woltspace_notify.notify_owner\n    + Show details\n' + question + '\n\n'
            '  › 1. Allow                   Run the tool and continue\n'
            '    2. Allow for this session  Run the tool and remember this choice for this\n'
            '                               session\n'
            '    3. Always allow            Run the tool and remember this choice for\n'
            '                               future tool calls\n'
            '    4. Cancel                  Cancel this tool call\n'
            '  enter to submit | esc to cancel\n')
    assert startup.model._classify_boot_symptom(text) == "tool_approval_prompt"
    assert startup.model._classify_boot_symptom(question) != "tool_approval_prompt"
    assert startup.model._classify_boot_symptom(text.replace(question, "ordinary task text")) != "tool_approval_prompt"
    assert startup.model._classify_boot_symptom(text.replace('    4. Cancel', '    4. Other')) != "tool_approval_prompt"
    monkeypatch.setattr(startup.model, "_harness_process_status", lambda *_: True)
    monkeypatch.setattr(startup.model, "_capture_boot_symptom", lambda *_, **__: startup.model._classify_boot_symptom(text))
    diag = startup.model.RunDiagnostics("codex")
    assert not startup._observe_boot("synthetic", {}, diag, owner_tool=True)
    assert diag.category == "tool_approval_prompt" and diag.notify_attempt == "not_seen"


@pytest.mark.parametrize("symptom,expected", [("trust_or_approval", True),
    ("waiting_for_input", True), ("tool_approval_prompt", False), ("directory_trust_prompt", False)])
def test_owner_tool_observer_never_answers_or_waits_for_specific_approval(monkeypatch, symptom, expected):
    from scripts import whatsapp_startup_inspection as startup
    monkeypatch.setattr(startup.model, "_harness_process_status", lambda *_: True)
    monkeypatch.setattr(startup.model, "_capture_boot_symptom", lambda *_, **__: symptom)
    diagnostics = startup.model.RunDiagnostics("codex")
    assert startup._observe_boot("synthetic", {}, diagnostics, owner_tool=True) is expected
    assert bool(diagnostics.category) is not expected


@pytest.mark.parametrize("symptom,expected", [
    ("tool_approval_prompt", True), ("directory_trust_prompt", False),
    ("onboarding_prompt", False), ("trust_or_approval", True),
    ("waiting_for_input", True), ("auth_or_config", False), ("bootstrap_missing", False),
])
def test_approval_observer_waits_without_mistaking_broad_text_for_ui(monkeypatch, symptom, expected):
    from scripts import whatsapp_startup_inspection as startup
    monkeypatch.setattr(startup.model, "_harness_process_status", lambda *_: True)
    monkeypatch.setattr(startup.model, "_capture_boot_symptom", lambda *_, **__: symptom)
    diagnostics = startup.model.RunDiagnostics("codex")
    assert startup._observe_boot("synthetic", {}, diagnostics, allow_tool_approval=True) is expected
    assert bool(diagnostics.category) is not expected
    if symptom in {"trust_or_approval", "waiting_for_input"}:
        assert diagnostics.as_record()["harness_readiness"] == "unknown"
        baseline = startup.model.RunDiagnostics("codex")
        assert not startup._observe_boot("synthetic", {}, baseline)
        assert baseline.category


@pytest.mark.parametrize("suffix", ["", "\n• Starting MCP servers (1/2): codex_apps (0s • esc to interrupt)\n› Ask Codex to do anything"])
def test_exact_echoed_approval_instructions_are_not_a_displayed_prompt(monkeypatch, suffix):
    from scripts import whatsapp_startup_inspection as startup
    text = startup.APPROVAL_PROBE_PROMPT + suffix
    symptom = startup.model._classify_boot_symptom(text)
    assert symptom == "trust_or_approval"  # Reproduce the owner-run false positive.
    monkeypatch.setattr(startup.model, "_harness_process_status", lambda *_: True)
    monkeypatch.setattr(startup.model, "_capture_boot_symptom", lambda *_, **__: symptom)
    diagnostics = startup.model.RunDiagnostics("codex")
    assert startup._observe_boot("synthetic", {}, diagnostics, allow_tool_approval=True)
    record = diagnostics.as_record()
    assert record["harness_readiness"] == "unknown" and not diagnostics.category
    assert record["checks"]["harness_boot"] != "passed"


def test_approval_attach_notice_refuses_unverified_target(monkeypatch, tmp_path):
    from scripts import whatsapp_startup_inspection as startup
    monkeypatch.setattr(startup, "_verify_record", lambda *_: False)
    output = []
    monkeypatch.setattr(startup.model, "_say", output.append)
    with pytest.raises(RuntimeError):
        startup._approval_notice("synthetic", tmp_path, {}, tmp_path, 120)
    assert output == []


@pytest.mark.parametrize("directory_trust", ["preserve", "normal"])
def test_real_prepare_scopes_directory_trust_and_keeps_prompt_policy(
    tmp_path, monkeypatch, directory_trust,
):
    from scripts import whatsapp_startup_inspection as startup
    import sessions
    import harnesses
    from session_runtime import TmuxSessionRuntime

    wolts = tmp_path / "wolts"
    wolts.mkdir()
    wolt = startup.model._create_synthetic_wolt(wolts, "codex")
    private_codex = tmp_path / "codex-home"
    private_codex.mkdir(mode=0o700)
    owner_home = tmp_path / "synthetic-owner"
    owner_home.mkdir()
    owner_config = owner_home / "config.toml"
    owner_config.write_text('synthetic_owner_setting = true\n')
    env = {"CODEX_HOME": str(private_codex), "WOLTSPACE_PRESERVE_TRUST_PROMPTS": "1"}
    startup._directory_trust_environment(env, tmp_path, directory_trust)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.setenv("CODEX_HOME", env["CODEX_HOME"])
    if directory_trust == "preserve":
        monkeypatch.setenv("WOLTSPACE_PRESERVE_TRUST_PROMPTS", "1")
    else:
        monkeypatch.delenv("WOLTSPACE_PRESERVE_TRUST_PROMPTS", raising=False)
    monkeypatch.setattr(harnesses, "codex_supports_no_daemon", lambda: False)
    def no_trust_write(*_):
        raise AssertionError("trust dialog must not be pre-accepted")
    if directory_trust == "preserve":
        monkeypatch.setattr(sessions, "ensure_codex_dir_trusted", no_trust_write)
    sessions.SessionRegistry(wolts).create(
        "synthetic-startup", wolt="wa-test", harness="codex", dir=str(wolt),
        execution_policy={"mode": "prompt", "isolation": "host"},
        adapter="whatsapp", chat_id=startup.PARTICIPANT, whatsapp_account=startup.ACCOUNT,
    )
    command = sessions.prepare_session_command("synthetic-startup", "spawn", startup.PROMPT)
    import shlex
    assert startup.PROMPT in shlex.split(command)[-1]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    config = private_codex / "config.toml"
    if directory_trust == "preserve":
        assert not config.exists()
    else:
        import tomllib
        assert tomllib.loads(config.read_text())["projects"] == {
            str(wolt.resolve()): {"trust_level": "trusted"},
        }
    assert owner_config.read_text() == 'synthetic_owner_setting = true\n'
    # This is the actual launch envelope passed to tmux, which must carry the
    # opt-in to run-session.sh/session-reg in the separate bootstrap process.
    launch = TmuxSessionRuntime._launch_command("synthetic-command")
    assert ("WOLTSPACE_PRESERVE_TRUST_PROMPTS=1" in launch) == (directory_trust == "preserve")


def test_directory_trust_rejects_owner_config_location(tmp_path):
    from scripts import whatsapp_startup_inspection as startup
    owner = tmp_path / "synthetic-owner"
    owner.mkdir()
    config = owner / "config.toml"
    config.write_text('synthetic_owner_setting = true\n')
    with pytest.raises(RuntimeError, match="fresh private"):
        startup._directory_trust_environment({"CODEX_HOME": str(owner)}, tmp_path, "normal")
    assert config.read_text() == 'synthetic_owner_setting = true\n'


@pytest.mark.parametrize(("frame", "symptom"), [
    ("Do you trust the contents of this directory?\n1. Yes, proceed\n2. No, exit",
     "directory_trust_prompt"),
    ("Welcome to Codex\nChoose a theme\n1. Dark mode\n2. Light mode\nPress Enter to continue",
     "onboarding_prompt"),
    ("Would you like to run the following command?\n$ synthetic-command\n1. Yes, proceed\n2. No, exit",
     "tool_approval_prompt"),
    ("Do you trust this workspace?", "trust_or_approval"),
    ("Do you trust this workspace?\nWould you like to run the following command?\n1. Yes, proceed\n2. No, exit",
     "trust_or_approval"),
])
def test_visible_dialog_symptoms_require_specific_header_and_choices(frame, symptom):
    from scripts import whatsapp_startup_inspection as startup
    actual = startup.model._classify_boot_symptom(frame)
    assert actual == symptom
    diagnostics = startup.model.RunDiagnostics("codex")
    assert startup.model._handle_boot_symptom(diagnostics, actual)
    record = diagnostics.as_record()
    assert record["boot_symptom"] == symptom
    assert startup.model._valid_diagnostic_record(record)
    assert frame not in str(record)


def test_startup_capture_uses_visible_frame_without_scrollback(monkeypatch):
    from scripts import whatsapp_startup_inspection as startup
    calls = []
    monkeypatch.setattr(
        startup.model, "_tmux_value",
        lambda env, *args: calls.append(args) or (
            0, "Do you trust this workspace?\n1. Yes, proceed\n2. No, exit", "",
        ),
    )
    assert startup.model._capture_boot_symptom({}, "synthetic", visible_only=True) == "directory_trust_prompt"
    assert "-S" not in calls[0]


def test_snapshot_redacts_credentials_but_retains_synthetic_ui():
    from scripts import whatsapp_startup_inspection as startup
    text = '''Synthetic UI: model connection failed; press Enter to retry.
WHATSAPP_AGENT_KEY=short-private-key
"access_token": "short-private-token", "refresh_token": "private-refresh"
Authorization: Bearer private-bearer
Bearer bare-bearer-value
eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature
sk-proj-privateexample123456789
user_code=ABCD-EFGH
-----BEGIN PRIVATE KEY-----
private-pem-content
-----END PRIVATE KEY-----
'''
    clean = startup._redact_pane(text)
    assert "Synthetic UI: model connection failed" in clean
    for value in ("short-private-key", "short-private-token", "private-refresh",
                  "private-bearer", "bare-bearer-value", "eyJhbGci", "sk-proj-",
                  "ABCD-EFGH", "private-pem-content"):
        assert value not in clean
    assert len(startup._redact_pane("x" * 20000).encode()) <= 8192


def test_snapshot_targets_only_verified_exact_pane(monkeypatch, tmp_path):
    from scripts import whatsapp_startup_inspection as startup
    record = {"status": "failed", "exit_code": 2, "harness_session_id": "synthetic-id",
              "runtime": {"pane_id": "%7", "tmux_session_name": "synthetic-session"}}
    monkeypatch.setattr(startup.model, "_record_for_test", lambda *_: record)
    monkeypatch.setattr(startup, "_verify_record", lambda *_: True)
    calls = []
    def tmux(_env, *args):
        calls.append(args)
        return (0, "%7", "") if args[0] == "list-panes" else (
            0, "Synthetic model UI\nAPI_KEY=private-test-value", "",
        )
    monkeypatch.setattr(startup.model, "_tmux_value", tmux)
    boot_log = tmp_path / ".space/logs/session-boot.log"
    boot_log.parent.mkdir(parents=True)
    boot_log.write_text("[synthetic-time] spawn session: synthetic\n"
                        "[synthetic-time] deliver-prompt synthetic: False\n"
                        "raw-synthetic-exception must not leave the parser\n")
    snapshot = startup._capture_snapshot("synthetic", tmp_path, {}, tmp_path, "normal", None)
    assert calls == [("list-panes", "-t", "synthetic-session", "-F", "#{pane_id}"),
                     ("capture-pane", "-p", "-t", "%7")]
    assert snapshot["pane_status"] == "captured"
    assert snapshot["harness_exit_code"] == 2
    assert snapshot["harness_rollout_observed"] is True
    assert snapshot["wrapper_started"] is True
    assert snapshot["deferred_prompt_hook_ran"] is True
    assert "raw-synthetic-exception" not in str(snapshot)
    assert "private-test-value" not in str(snapshot)
    monkeypatch.setattr(startup, "_verify_record", lambda *_: False)
    calls.clear()
    assert startup._capture_snapshot("synthetic", tmp_path, {}, tmp_path, "normal", None)["pane_status"] == "unavailable"
    assert calls == []


@pytest.mark.parametrize("scenario", [
    "success", "normal_success", "snapshot_success", "snapshot_timeout", "wrong_payload", "timeout", "trust_prompt", "spawn_failure", "cleanup_failure", "probe_success", "probe_api_missing", "probe_approval_success", "probe_approval_timeout", "probe_approval_echo_timeout", "owner_tool_success", "owner_tool_prompt",
])
def test_full_offline_startup_orchestration(tmp_path, scenario):
    # Fresh interpreter ensures real server/session imports bind to only the
    # synthetic environment. No owner's private directory/auth is involved.
    code = r'''
import json, os, subprocess, sys, tempfile, threading
from pathlib import Path
from scripts import whatsapp_startup_inspection as startup
model = startup.model
base = Path(sys.argv[1])
scenario = sys.argv[2]
private = base / "private"
private.mkdir(mode=0o700)
auth = base / "synthetic-auth"
auth.mkdir(mode=0o700)
model.owner_test.PRIVATE = private
model.owner_test._check_private_dir = lambda: None
model._auth_path = lambda *_: ("CODEX_HOME", auth)
def preflight(*args):
    Path(args[1]["CODEX_HOME"]).mkdir(mode=0o700)
    args[-1].passed("preflight")
model._preflight_harness = preflight
def key_forbidden():
    raise AssertionError("credential access forbidden")
model.owner_test._read_token = key_forbidden
model._verify_private_tmux = lambda *_: None
model._harness_process_status = lambda *_: True
model._harness_readiness_status = lambda *_: "unsupported"
def boot_symptom(*_, **__):
    if scenario == "owner_tool_prompt":
        return "tool_approval_prompt"
    if scenario == "probe_approval_echo_timeout":
        return model._classify_boot_symptom(startup.APPROVAL_PROBE_PROMPT +
            "\n• Starting MCP servers (1/2): codex_apps (0s • esc to interrupt)\n› Ask Codex to do anything")
    if scenario.startswith("probe_approval_"):
        return "tool_approval_prompt"
    return "trust_or_approval" if scenario == "trust_prompt" else "none_detected"
model._capture_boot_symptom = boot_symptom
model._tmux_value = lambda env, *args: (0, "%1", "") if args[0] == "list-panes" else (
    0, "Synthetic model UI is waiting\nAPI_KEY=private-snapshot-value", "")
real_cleanup = model._cleanup_resources
cleanup_requests = []
def cleanup(*args):
    cleanup_requests.append(args[-1])
    return real_cleanup(*args, stop_session_fn=lambda _: {"status": "stopped"},
                        kill_tmux_fn=lambda *_: scenario != "cleanup_failure")
model._cleanup_resources = cleanup
cli_threads = []
cli_results = []
def fake_start(prompt=None):
    assert (os.environ.get("WOLTSPACE_PRESERVE_TRUST_PROMPTS") == "1") == (scenario != "normal_success" and not scenario.startswith(("probe_", "owner_tool_")))
    if scenario.startswith("owner_tool_"):
        assert prompt == startup.OWNER_TOOL_PROMPT
        assert os.environ.get("WOLTSPACE_NOTIFY_TOOL") == "1"
        assert "WHATSAPP_AGENT_TOKEN" not in os.environ
    if scenario.startswith("probe_"):
        assert prompt == (startup.APPROVAL_PROBE_PROMPT if scenario.startswith("probe_approval_") else startup.PROBE_PROMPT)
    if scenario == "spawn_failure":
        raise RuntimeError("private-raw-start-error synthetic-secret")
    # Supported registry path and real notify routing, without tmux/model spawn.
    from sessions import SessionRegistry
    registry = SessionRegistry()
    record = registry.create(
        "wa-test-offline-startup", wolt="wa-test", harness="codex", creature="otter",
        dir=str(Path(os.environ["WOLTS_DIR"]) / "wa-test"),
        adapter="whatsapp", chat_id=startup.PARTICIPANT,
        whatsapp_account=startup.ACCOUNT,
        execution_policy={"mode": "prompt", "isolation": "host"},
    )
    registry.update(record["name"], wolt="wa-test", runtime={
        "kind": "tmux", "tmux_session_name": "synthetic", "pane_id": "%1",
    })
    if scenario.startswith("owner_tool_"):
        record = registry.update(record["name"], wolt="wa-test", notify_owner_generation="synthetic-tool-launch")
    if scenario not in {"timeout", "snapshot_timeout", "trust_prompt", "probe_approval_timeout", "probe_approval_echo_timeout", "owner_tool_prompt"}:
        def cli():
            env = dict(os.environ)
            env["WOLTSPACE_WOLT_SESSION"] = record["name"]
            from session_runtime import _session_env_value
            env["PATH"] = _session_env_value("PATH", env["PATH"])
            if scenario == "probe_api_missing":
                env.pop("WOLTSPACE_API", None)
            message = "wrong-synthetic-body" if scenario == "wrong_payload" else startup.BODY
            command = [str(Path(env["WOLTS_DIR"]) / "wa-test/startup-notify-probe")] if scenario.startswith("probe_") else [
                str(startup.ROOT / "container/bin/notify"), "--whatsapp", startup.PARTICIPANT]
            if scenario.startswith("owner_tool_"):
                import notify_owner_mcp as worker
                command = [sys.executable, "-I", str(Path(worker.__file__).resolve()),
                    "--session", record["name"], "--binding", worker.route_binding(record),
                    "--api", env["WOLTSPACE_API"]]
                message = json.dumps({"jsonrpc":"2.0", "id":1, "method":"tools/call",
                    "params":{"name":"notify_owner", "arguments":{"message":startup.BODY}}})+"\n"
            result = subprocess.run(
                command, input=message,
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=10,
            )
            if scenario.startswith("owner_tool_"):
                assert json.loads(result.stdout)["result"]["isError"] is False
            cli_results.append(result.returncode)
        thread = threading.Thread(target=cli)
        thread.start()
        cli_threads.append(thread)
    return {"name": record["name"]}
startup._start_session = fake_start
diag = model.RunDiagnostics("codex")
with tempfile.TemporaryDirectory(prefix=".wsi-offline-", dir=startup.ROOT.parents[2]) as sockets:
    model.PRIVATE_TMUX_BASE = Path(sockets)
    try:
        result = model._run_model_test("codex", diag, startup_runner=lambda *args:
            startup._run_startup_session(*args, timeout_seconds=1 if scenario in {"timeout", "snapshot_timeout", "probe_approval_timeout", "probe_approval_echo_timeout"} else 5,
                directory_trust="normal" if scenario == "normal_success" or scenario.startswith(("probe_", "owner_tool_")) else "preserve",
                capture_snapshot=scenario.startswith(("snapshot_", "probe_")),
                probe_transport=scenario.startswith("probe_"),
                request_notify_approval=scenario.startswith("probe_approval_"),
                notify_owner_tool=scenario.startswith("owner_tool_")))
    except Exception:
        result = 2
    for thread in cli_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
record = diag.as_record()
assert cleanup_requests == [True]  # Including spawn_failure before a returned ID.
assert model._valid_diagnostic_record(record)
assert "bot.whatsapp_adapter" not in sys.modules
if scenario in {"success", "normal_success", "snapshot_success", "probe_success", "probe_approval_success", "owner_tool_success"}:
    assert result == 0
    assert record["notify_attempt"] == "sent"
    assert record["checks"]["notify"] == "passed"
    assert record["checks"]["cleanup"] == "passed"
    assert cli_results == [0]
    if scenario not in {"snapshot_success", "probe_success", "probe_approval_success"}:
        assert not list(private.glob("startup-*"))
elif scenario == "cleanup_failure":
    assert result == 2 and record["category"] == "cleanup_failed"
    assert record["notify_attempt"] == "sent"
    assert record["checks"]["cleanup"] == "failed"
else:
    assert result == 2
    assert record["category"] == {
        "wrong_payload": "notify_rejected", "timeout": "run_timeout",
        "snapshot_timeout": "run_timeout",
        "trust_prompt": "trust_or_approval_prompt",
        "spawn_failure": "session_not_started",
        "probe_api_missing": "local_transport_probe_failed",
        "probe_approval_timeout": "approval_not_completed",
        "probe_approval_echo_timeout": "approval_not_completed",
        "owner_tool_prompt": "tool_approval_prompt",
    }[scenario]
    assert record["checks"]["notify"] != "passed"
    assert record["notify_attempt"] == ("rejected" if scenario == "wrong_payload" else "not_seen")
    assert record["checks"]["cleanup"] == "passed"
if scenario not in {"success", "normal_success", "snapshot_success", "probe_success", "probe_approval_success", "owner_tool_success"}:
    paths = list(private.glob("startup-*/failure.json"))
    assert len(paths) == 1
    data = json.loads(paths[0].read_text())
    assert model._valid_diagnostic_record(data)
    assert all(term not in str(data) for term in (startup.PARTICIPANT, startup.BODY, startup.PROMPT))
    assert not list(paths[0].parent.rglob("*.log"))
if scenario.startswith(("snapshot_", "probe_")):
    snapshots = list(private.glob("startup-*/startup-snapshot.json"))
    assert len(snapshots) == 1
    snapshot = json.loads(snapshots[0].read_text())
    assert snapshot["pane_status"] == "captured"
    assert "Synthetic model UI" in snapshot["pane_text"]
    assert "private-snapshot-value" not in str(snapshot)
    assert snapshot["notify_requests"] == (1 if scenario in {"snapshot_success", "probe_success", "probe_approval_success"} else 0)
    assert snapshot["notify_last_status"] == (200 if scenario in {"snapshot_success", "probe_success", "probe_approval_success"} else None)
    assert snapshots[0].stat().st_mode & 0o777 == 0o600
    assert not list(snapshots[0].parent.rglob("*.log"))
    assert {p.name for p in snapshots[0].parent.iterdir()} == (
        {"startup-snapshot.json"} if scenario in {"snapshot_success", "probe_success", "probe_approval_success"} else {"startup-snapshot.json", "failure.json"})
    if scenario in {"probe_approval_timeout", "probe_approval_echo_timeout"}:
        assert snapshot["host_health"] == "not_checked"
        assert snapshot["transport_probe"] is None
        assert record["boot_symptom"] == ("trust_or_approval" if scenario == "probe_approval_echo_timeout" else "tool_approval_prompt")
        if scenario == "probe_approval_echo_timeout":
            assert record["harness_readiness"] == "unknown"
        assert cli_results == []
    elif scenario.startswith("probe_"):
        assert snapshot["host_health"] == "ok"
        assert snapshot["transport_probe"]["destination"] == ("default_7777" if scenario == "probe_api_missing" else "expected")
        assert snapshot["transport_probe"]["net_namespace"] == "same"
        assert snapshot["transport_probe"]["health"] == "ok"
        assert snapshot["transport_probe"]["api_present"] == (scenario != "probe_api_missing")
print("offline_startup_ok")
'''
    env = dict(os.environ)
    env.pop("WHATSAPP_AGENT_TOKEN", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), scenario], cwd=ROOT, env=env,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
    )
    # Every fixture is synthetic; failures here cannot expose real private text.
    assert result.returncode == 0, result.stderr
    assert "offline_startup_ok" in result.stdout
    assert "private-raw-start-error" not in result.stdout + result.stderr
    if scenario.startswith("probe_approval_"):
        assert "Approval needed:" in result.stdout
        assert "Attach only to this temporary test session: env -u TMUX tmux -S " in result.stdout
        assert "attach-session -t =synthetic" in result.stdout
        assert "Observation budget:" in result.stdout
    if scenario in {"probe_approval_timeout", "probe_approval_echo_timeout"}:
        assert "This does not prove approval was denied. Cleaning up." in result.stdout


def test_owner_only_counter_refuses_legacy_and_duplicate_before_app():
    import asyncio
    from scripts.whatsapp_startup_inspection import _NotifyCounter
    reached, responses = [], []
    async def app(scope, receive, send):
        reached.append(scope["path"])
        await send({"type":"http.response.start", "status":200, "headers":[]})
        await send({"type":"http.response.body", "body":b""})
    async def receive():
        raise AssertionError("refused calls must not consume a body")
    async def send(item):
        if item["type"] == "http.response.start":
            responses.append(item["status"])
    async def run():
        counter = _NotifyCounter(app, owner_only=True)
        for path in ("/notify", "/notify/owner", "/notify/owner"):
            await counter({"type":"http", "path":path}, receive, send)
        assert counter.owner_count == 2 and counter.legacy_count == 1
    asyncio.run(run())
    assert reached == ["/notify/owner"]
    assert responses == [409, 200, 409]
