import importlib.util
import asyncio
import os
import runpy
import stat
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "whatsapp_model_session_test_script",
    ROOT / "scripts" / "whatsapp_model_session_test.py",
)
model_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model_test)


def _completed_event():
    event = threading.Event()
    event.set()
    return event


def test_auth_path_uses_existing_configured_harness_directory(tmp_path):
    config = tmp_path / "authorized-config"
    config.mkdir(mode=0o700)
    variable, path = model_test._auth_path(
        "claude", {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": str(config)}
    )
    assert variable == "CLAUDE_CONFIG_DIR"
    assert path == config.resolve()


def test_auth_path_supports_default_existing_codex_directory_without_reading_files(
    tmp_path, monkeypatch,
):
    auth_dir = tmp_path / ".codex"
    auth_dir.mkdir(mode=0o700)
    def no_auth_file_read(_path, *args, **kwargs):
        raise AssertionError("auth file contents must not be read")
    monkeypatch.setattr(Path, "read_text", no_auth_file_read)
    variable, path = model_test._auth_path("codex", {"HOME": str(tmp_path)})
    assert variable == "CODEX_HOME"
    assert path == auth_dir.resolve()


def test_auth_path_fails_when_existing_login_directory_is_unavailable(tmp_path):
    with pytest.raises(RuntimeError, match="no existing authorized login"):
        model_test._auth_path("claude", {"HOME": str(tmp_path)})


def test_boot_symptom_classifier_returns_only_allowlisted_categories():
    assert model_test._classify_boot_symptom(
        "Not logged in. Run codex login. participant=opaque prompt=private"
    ) == "auth_or_config"
    assert model_test._classify_boot_symptom(
        "notify: command not found; secret=synthetic"
    ) == "bootstrap_missing"
    assert model_test._classify_boot_symptom(
        "Do you trust this workspace? participant=opaque"
    ) == "trust_or_approval"
    assert model_test._classify_boot_symptom(
        "Permission request: approve this command? secret=synthetic"
    ) == "trust_or_approval"
    assert model_test._classify_boot_symptom(
        "Onboarding requires trust before continuing; participant=opaque"
    ) == "trust_or_approval"
    assert model_test._classify_boot_symptom(
        "Press Enter to continue; participant=opaque"
    ) == "waiting_for_input"
    assert model_test._classify_boot_symptom("ordinary model output") == "none_detected"


@pytest.mark.parametrize(
    ("text", "category", "symptom"),
    [
        ("Not logged in; private token=synthetic", "auth_or_config", "auth_or_config"),
        ("notify: command not found; prompt=private", "bootstrap_missing", "bootstrap_missing"),
        ("Trust this workspace? participant=opaque", "trust_or_approval_prompt", "trust_or_approval"),
        ("Onboarding: approve this command? secret=synthetic", "trust_or_approval_prompt", "trust_or_approval"),
        ("Press Enter to continue; prompt=private", "waiting_for_input", "waiting_for_input"),
    ],
)
def test_actionable_boot_symptoms_record_only_fixed_categories(text, category, symptom):
    diagnostics = model_test.RunDiagnostics("codex")
    assert model_test._handle_boot_symptom(
        diagnostics, model_test._classify_boot_symptom(text),
    )
    record = diagnostics.as_record()
    assert record["category"] == category
    assert record["boot_symptom"] == symptom
    assert record["harness_readiness"] == "prompt_detected"
    assert model_test._valid_diagnostic_record(record)
    assert all(value not in str(record) for value in (
        "synthetic", "private", "opaque", "participant", "prompt=",
    ))


def test_codex_process_presence_is_not_readiness(monkeypatch):
    assert model_test._harness_readiness_status("synthetic-session", "codex") == "unsupported"
    diagnostics = model_test.RunDiagnostics("codex")
    diagnostics.set_harness_process("present")
    diagnostics.set_harness_readiness("unsupported")
    diagnostics.set_boot_symptom("none_detected")
    record = diagnostics.as_record()
    assert record["harness_process"] == "present"
    assert record["harness_readiness"] == "unsupported"
    assert record["boot_symptom"] == "none_detected"
    assert record["checks"]["harness_boot"] == "pending"


def test_harness_process_probe_excludes_launcher_shims(monkeypatch):
    import sessions
    calls = []
    monkeypatch.setattr(
        sessions, "session_has_agent_process",
        lambda *args, **kwargs: calls.append(kwargs) or True,
    )
    assert model_test._harness_process_status("synthetic", "codex") is True
    assert calls == [{"harness": "codex", "include_launching": False}]


def test_failure_record_has_only_sanitized_schema_and_private_mode(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    diagnostics = model_test.RunDiagnostics("codex")
    diagnostics.enter("harness_boot")
    diagnostics.fail("auth_or_config")
    path = model_test._write_failure_record(root, diagnostics)
    text = path.read_text()
    data = __import__("json").loads(text)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert data == {
        "schema": 4,
        "harness": "codex",
        "stage": "harness_boot",
        "category": "auth_or_config",
        "checks": {
            "preflight": "pending", "server_health": "pending",
            "inbound_selection": "pending", "selection_reply": "pending",
            "challenge_dispatch": "pending", "session_spawn": "pending",
            "harness_boot": "failed", "session_ack": "pending",
            "notify": "pending", "cleanup": "not_reached",
        },
        "notify_attempt": "not_seen",
        "notify_rejection": "none",
        "notify_payload_shape": "not_seen",
        "harness_process": "not_checked",
        "harness_readiness": "not_checked",
        "boot_symptom": "not_checked",
        "control_reply": model_test._empty_control_reply(),
        "cleanup_components": dict.fromkeys(model_test._CLEANUP_COMPONENTS, "not_checked"),
    }
    assert all(term not in text for term in (
        "synthetic-whatsapp-secret", "participant=", "private prompt",
        "raw exception", "opaque",
    ))
    assert model_test._valid_diagnostic_record(data)
    previous = dict(data, schema=3)
    del previous["control_reply"], previous["cleanup_components"]
    assert model_test._valid_diagnostic_record(previous)
    legacy = dict(previous, schema=2)
    del legacy["notify_rejection"], legacy["notify_payload_shape"]
    assert model_test._valid_diagnostic_record(legacy)
    data["notify_attempt"] = "raw prompt text"
    assert not model_test._valid_diagnostic_record(data)
    data["notify_attempt"] = []
    assert not model_test._valid_diagnostic_record(data)


def test_codex_preflight_checks_real_bootstrap_files_and_offline_cli(
    tmp_path, monkeypatch,
):
    auth = tmp_path / "codex-auth"
    auth.mkdir(mode=0o700)
    (auth / "auth.json").write_text("synthetic-only")
    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    wolts = run / "wolts"
    wolts.mkdir(mode=0o700)
    wolt = model_test._create_synthetic_wolt(wolts, "codex")
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(run / "home"),
           "CODEX_HOME": str(run / "codex-home"), "WOLTSPACE_ISOLATION": "host"}
    Path(env["HOME"]).mkdir(mode=0o700)
    calls = []
    real_which = model_test.shutil.which
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\nexit 0\n")
    fake_codex.chmod(0o700)
    monkeypatch.setattr(
        model_test.shutil, "which",
        lambda name, path=None: str(fake_codex) if name == "codex" else real_which(name, path=path),
    )
    monkeypatch.setattr(
        model_test.subprocess, "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or SimpleNamespace(returncode=0),
    )
    diagnostics = model_test.RunDiagnostics("codex")
    model_test._preflight_harness("codex", env, auth, wolt, diagnostics)
    assert diagnostics.checks["preflight"] == "passed"
    assert [call[0][1:] for call in calls] == [["--version"], ["--help"]]
    assert all("CODEX_HOME" not in call[1]["env"] for call in calls)
    assert (wolt / ".agents" / "skills" / "woltspace-start-chat" / "SKILL.md").is_file()
    assert (wolt / ".agents" / "skills" / "woltspace-notify" / "SKILL.md").is_file()
    mode = wolt / ".agents/skills/woltspace-start-chat/modes/whatsapp.md"
    assert mode.read_bytes() == (ROOT / "container/skills/start-chat/modes/whatsapp.md").read_bytes()
    assert "load the `woltspace-notify` skill" in mode.read_text()
    delivered_notify = wolt / ".agents/skills/woltspace-notify/SKILL.md"
    assert delivered_notify.read_text().split("---", 2)[-1] == (ROOT / "container/skills/notify/SKILL.md").read_text().split("---", 2)[-1]
    assert 'sandbox_permissions="require_escalated"' in delivered_notify.read_text()
    assert Path(env["CODEX_HOME"]).is_relative_to(run)
    assert (Path(env["CODEX_HOME"]) / "auth.json").is_symlink()
    assert (Path(env["CODEX_HOME"]) / "auth.json").resolve() == (auth / "auth.json").resolve()


def test_codex_preflight_executable_failure_is_allowlisted(tmp_path, monkeypatch):
    auth = tmp_path / "codex-auth"
    auth.mkdir()
    (auth / "auth.json").write_text("synthetic-only")
    run = tmp_path / "run"
    run.mkdir()
    wolts = run / "wolts"
    wolts.mkdir()
    wolt = model_test._create_synthetic_wolt(wolts, "codex")
    real_which = model_test.shutil.which
    monkeypatch.setattr(
        model_test.shutil, "which",
        lambda name, path=None: None if name == "codex" else real_which(name, path=path),
    )
    diagnostics = model_test.RunDiagnostics("codex")
    with pytest.raises(RuntimeError, match="executable is unavailable"):
        model_test._preflight_harness(
            "codex", {"PATH": os.defpath, "HOME": str(run / "home"),
                      "CODEX_HOME": str(run / "codex-home"), "WOLTSPACE_ISOLATION": "host"},
            auth, wolt, diagnostics,
        )
    assert diagnostics.as_record()["category"] == "executable_missing"


def test_real_session_prepare_uses_seeded_codex_skills_and_test_auth_home(tmp_path, monkeypatch):
    import sessions
    import harnesses
    assert Path(sessions.__file__).resolve() == ROOT / "container/lib/sessions.py"
    assert Path(harnesses.__file__).resolve() == ROOT / "container/lib/harnesses.py"

    if not model_test.shutil.which("codex"):
        pytest.skip("Codex CLI is not installed")

    run = tmp_path / "run"
    run.mkdir(mode=0o700)
    wolts = run / "wolts"
    wolts.mkdir(mode=0o700)
    wolt = model_test._create_synthetic_wolt(wolts, "codex")
    auth = run / "codex-home"
    auth_source = run / "authorized-home"
    auth_source.mkdir(mode=0o700)
    (auth_source / "auth.json").write_text("synthetic-only")
    model_test._preflight_harness(
        "codex",
        {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(run / "home"),
         "CODEX_HOME": str(auth), "WOLTSPACE_ISOLATION": "host"},
        auth_source, wolt, model_test.RunDiagnostics("codex"),
    )
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setenv("WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("WOLTSPACE_DIR", str(ROOT))
    monkeypatch.setenv("CODEX_HOME", str(auth))
    monkeypatch.setattr(harnesses, "codex_supports_no_daemon", lambda: True)
    registry = sessions.SessionRegistry(wolts)
    record = registry.create(
        "wa-test-offline", wolt="wa-test", harness="codex", dir=str(wolt),
        prompt="synthetic boot message", adapter="whatsapp", chat_id="user:4242",
        whatsapp_account="synthetic-account",
    )
    command = sessions.prepare_session_command(
        "wa-test-offline", "spawn", record["prompt"],
    )
    assert str(ROOT / "container" / "bin" / "wcodex") in command
    assert "@woltspace-start-chat" in command
    assert "notify --whatsapp user:4242" in command
    assert (auth / "config.toml").is_file()
    assert (wolt / ".agents" / "skills").is_symlink()


def test_offline_executable_preflight_failure_precedes_key_read_and_preserves_only_record(
    tmp_path, monkeypatch,
):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    auth = tmp_path / "auth"
    auth.mkdir(mode=0o700)
    monkeypatch.setattr(model_test.owner_test, "PRIVATE", private)
    monkeypatch.setattr(model_test.owner_test, "_check_private_dir", lambda: None)
    monkeypatch.setattr(
        model_test, "_auth_path", lambda *_: ("CODEX_HOME", auth),
    )
    with tempfile.TemporaryDirectory(prefix="wpf-", dir="/tmp") as socket_base:
        monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", Path(socket_base))
        key_reads = []
        monkeypatch.setattr(
            model_test.owner_test, "_read_token", lambda: key_reads.append(True),
        )

        def fail_preflight(_harness, _env, _auth_path, _wolt, diagnostics):
            diagnostics.fail("executable_missing")
            raise RuntimeError("raw private prompt and synthetic exception text")

        monkeypatch.setattr(model_test, "_preflight_harness", fail_preflight)
        diagnostics = model_test.RunDiagnostics("codex")
        with pytest.raises(RuntimeError):
            model_test._run_model_test("codex", diagnostics)
    assert key_reads == []
    records = list(private.glob("model-*/failure.json"))
    assert len(records) == 1
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
    assert sorted(p.name for p in records[0].parent.iterdir()) == ["failure.json"]
    text = records[0].read_text()
    assert "raw private prompt" not in text
    assert "synthetic exception" not in text
    assert __import__("json").loads(text)["category"] == "executable_missing"


def test_main_outputs_only_sanitized_stage_and_category(monkeypatch, capfd):
    import builtins

    monkeypatch.setattr(model_test.sys, "argv", [
        "whatsapp_model_session_test.py", "--run", "--harness", "codex",
    ])
    monkeypatch.setattr(model_test.owner_test, "_require_tty", lambda: None)
    monkeypatch.setattr(builtins, "input", lambda *_: "MODEL")
    monkeypatch.setattr(
        model_test, "_run_model_test",
        lambda _harness, _diagnostics: (_ for _ in ()).throw(
            RuntimeError("synthetic-key participant=opaque private prompt raw exception")
        ),
    )
    assert model_test.main() == 2
    output = capfd.readouterr().out
    assert output.strip() == "Run stopped: stage=preflight category=preflight_failed."
    assert all(value not in output for value in (
        "synthetic-key", "opaque", "private prompt", "raw exception",
    ))


def test_private_environment_is_minimal_and_keeps_auth_out_of_home(tmp_path):
    run_root = tmp_path / "run"
    run_root.mkdir(mode=0o700)
    wolts_dir = run_root / "wolts"
    wolts_dir.mkdir(mode=0o700)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text("#!/bin/sh\nexit 0\n")
    tmux.chmod(0o700)
    auth = tmp_path / "claude-auth"
    auth.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix="wt-", dir="/tmp") as socket_base:
        tmux_tmp = model_test._prepare_private_tmux_dir(run_root, Path(socket_base))
    inherited = {
        "PATH": str(bindir),
        "HOME": str(tmp_path),
        "WHATSAPP_AGENT_TOKEN": "synthetic-whatsapp-secret",
        "ANTHROPIC_API_KEY": "synthetic-provider-secret",
        "TELEGRAM_BOT_TOKEN": "synthetic-chat-secret",
        "TMUX": "/owner/socket,1,0",
        "CLAUDE_CONFIG_DIR": str(auth),
    }
    env = model_test._private_environment(
        run_root, wolts_dir, "claude", "CLAUDE_CONFIG_DIR", auth,
        "127.0.0.1", 41321, inherited, tmux_tmp,
    )
    assert env["CLAUDE_CONFIG_DIR"] == str(auth.resolve())
    assert env["HOME"] == str(run_root / "home")
    assert env["TMUX_TMPDIR"] == str(tmux_tmp)
    assert env["WOLTSPACE_WOLTS_DIR"] == str(wolts_dir)
    assert env["WOLTSPACE_API"] == "http://127.0.0.1:41321"
    assert env["PORT"] != "7777"
    assert "TMUX" not in env and "TMUX_PANE" not in env
    assert "WHATSAPP_AGENT_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert stat.S_IMODE((run_root / "home").stat().st_mode) == 0o700


def test_synthetic_wolt_scaffolding_is_confined_to_private_test_root(tmp_path):
    wolts_dir = tmp_path / "wolts"
    wolts_dir.mkdir(mode=0o700)
    wolt = model_test._create_synthetic_wolt(wolts_dir, "codex")
    assert wolt == wolts_dir / "wa-test"
    manifest = (wolt / "wolt" / "wolt.json").read_text()
    assert '"harness": "codex"' in manifest
    config = (wolts_dir / "woltspace.json").read_text()
    assert '"enabled": false' in config
    assert all(path.resolve().is_relative_to(tmp_path.resolve())
               for path in wolt.rglob("*"))


def test_expected_nonce_matches_the_real_notify_cli_prefix(monkeypatch):
    notify_cli = runpy.run_path(str(ROOT / "container" / "bin" / "notify"))
    monkeypatch.setitem(
        notify_cli["_format_full_message"].__globals__,
        "_creature_emoji", lambda *_: "🦦",
    )
    nonce = "wa-model-check-a1b2c3"
    actual_cli_payload = notify_cli["_format_full_message"](
        "test-session", "wa-test", nonce, ROOT / "container" / "bin" / "session-reg",
    )
    assert actual_cli_payload == model_test._expected_notify_payload(nonce)
    assert actual_cli_payload == f"🦦 wa-test: {nonce}"


def test_notify_gate_allows_only_one_exact_payload_after_route_verification():
    attempts = []
    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    def verify(session, participant, supplied_routing):
        assert (session, participant, supplied_routing) == ("s1", "user:42", routing)
        return "s1"

    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: wa-model-check", verify, _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        await wrapped("whatsapp", "s1", "🦦 wa-test: wa-model-check", "user:42",
                      routing=routing)
        with pytest.raises(RuntimeError, match="already reserved"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: wa-model-check", "user:42",
                          routing=routing)

    asyncio.run(run())
    assert len(attempts) == 1
    assert gate.status == "sent"
    assert gate.verified_session == "s1"


def test_notify_gate_records_attempt_transitions_without_sensitive_values():
    class RecordingDiagnostics(model_test.RunDiagnostics):
        def __init__(self):
            super().__init__("codex")
            self.transitions = []

        def set_notify_attempt(self, state):
            self.transitions.append(state)
            super().set_notify_attempt(state)

    diagnostics = RecordingDiagnostics()
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: expected-nonce", lambda *_: "session-safe", _completed_event(),
        diagnostics=diagnostics,
    )
    attempts = []

    async def sender():
        attempts.append(diagnostics.as_record()["notify_attempt"])
        return {"ok": True}

    asyncio.run(gate.send(
        "private-session", "opaque-participant", "🦦 wa-test: expected-nonce",
        {"chat_id": "opaque-participant"}, sender,
    ))
    assert attempts == ["send_in_flight"]
    assert diagnostics.as_record()["notify_attempt"] == "sent"
    assert diagnostics.transitions == [
        "dispatch_wait", "reserved", "route_verified", "send_in_flight", "sent",
    ]
    assert all(value not in str(diagnostics.as_record()) for value in (
        "expected-nonce", "opaque-participant", "private-session",
    ))


def test_notify_gate_distinguishes_not_seen_from_blocked_send():
    diagnostics = model_test.RunDiagnostics("codex")
    assert diagnostics.as_record()["notify_attempt"] == "not_seen"
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: expected-nonce", lambda *_: "session-safe", _completed_event(),
        diagnostics=diagnostics,
    )
    started = asyncio.Event()

    async def blocked_sender():
        started.set()
        await asyncio.Event().wait()

    async def run():
        task = asyncio.create_task(gate.send(
            "private-session", "opaque-participant", "🦦 wa-test: expected-nonce",
            {"chat_id": "opaque-participant"}, blocked_sender,
        ))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert diagnostics.as_record()["notify_attempt"] == "send_in_flight"
        assert not model_test._confirmed_notify(gate, "session-safe", diagnostics)
        assert diagnostics.as_record()["checks"]["notify"] != "passed"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert diagnostics.as_record()["notify_attempt"] == "ambiguous"


@pytest.mark.parametrize("mode", ["rejected", "timeout"])
def test_notify_failure_record_hides_synthetic_participant_prompt_and_exception(
    mode, tmp_path,
):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    diagnostics = model_test.RunDiagnostics("codex")
    diagnostics.enter("notify")
    event = _completed_event()
    sender_calls = []

    async def sender():
        sender_calls.append(True)
        raise TimeoutError("raw exception prompt=synthetic participant=opaque secret=synthetic-key")

    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: expected-nonce", lambda *_: "session-private", event,
        diagnostics=diagnostics,
    )
    message = "wrong-payload" if mode == "rejected" else "🦦 wa-test: expected-nonce"

    async def run():
        with pytest.raises(RuntimeError if mode == "rejected" else TimeoutError):
            await gate.send("private-session", "opaque-participant", message,
                            {"chat_id": "opaque-participant"}, sender)

    asyncio.run(run())
    category = "notify_rejected" if mode == "rejected" else "notify_ambiguous"
    diagnostics.fail(category)
    raw_log = root / "runner.log"
    raw_log.write_text("private prompt, participant, and raw exception")
    model_test._remove_raw_log(root)
    path = model_test._write_failure_record(root, diagnostics)
    assert path.read_text().find("opaque-participant") == -1
    assert all(value not in path.read_text() for value in (
        "private-session", "expected-nonce", "synthetic-key", "raw exception",
    ))
    assert not raw_log.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sender_calls == ([] if mode == "rejected" else [True])
    assert __import__("json").loads(path.read_text())["notify_attempt"] == (
        "rejected" if mode == "rejected" else "ambiguous"
    )
    assert model_test._diagnostic_line(diagnostics) == (
        f"Run stopped: stage=notify category={category}."
        f" notify_rejection={'payload_mismatch' if mode == 'rejected' else 'none'}"
        f" notify_payload_shape={'other' if mode == 'rejected' else 'exact'}."
    )


def test_cleanup_failure_keeps_only_allowlisted_status_in_record(tmp_path):
    root = tmp_path / "run"
    root.mkdir(mode=0o700)
    diagnostics = model_test.RunDiagnostics("codex")
    diagnostics.enter("cleanup")
    diagnostics.fail("cleanup_failed")
    (root / "runner.log").write_text("prompt and synthetic key must not persist")
    session_log = root / "wolts" / ".space" / "logs" / "session-boot.log"
    session_log.parent.mkdir(parents=True)
    session_log.write_text("private prompt and synthetic auth text")
    model_test._remove_raw_log(root)
    path = model_test._write_failure_record(root, diagnostics)
    assert not model_test._finalize_run_root(root, False)
    assert (root / ".cleanup-required").exists()
    assert path.exists() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (root / "runner.log").exists()
    assert not session_log.exists()
    assert "synthetic key" not in path.read_text()


def test_notify_gate_rejects_wrong_payload_or_unverified_session_before_io():
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        with pytest.raises(RuntimeError, match="exact nonce"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce plus", "user:42",
                          routing={})

    asyncio.run(run())
    assert attempts == []
    assert gate.status == "rejected"


def test_notify_gate_requires_authoritative_verification_before_io():
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    wrapped = model_test._wrap_notify_send(
        lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
    )

    async def run():
        with pytest.raises(RuntimeError, match="route was not verified"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce", "user:42",
                          routing={"adapter": "whatsapp"})

    asyncio.run(run())
    assert attempts == []
    assert gate.status == "rejected"


@pytest.mark.parametrize("suffix", ["", "\n"])
def test_ambiguous_notify_send_is_terminal_and_never_retried(suffix):
    attempts = []
    routing = {"adapter": "whatsapp", "chat_id": "user:42",
               "whatsapp_account": "test-account", "wolt": "wa-test"}
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "s1", _completed_event(),
    )

    async def fail_ambiguous(*_args, **_kwargs):
        attempts.append("sent")
        raise TimeoutError("simulated timeout")

    wrapped = model_test._wrap_notify_send(fail_ambiguous, gate)

    async def run():
        with pytest.raises(TimeoutError):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce" + suffix, "user:42",
                          routing=routing)
        with pytest.raises(RuntimeError, match="already reserved"):
            await wrapped("whatsapp", "s1", "🦦 wa-test: nonce" + suffix, "user:42",
                          routing=routing)

    asyncio.run(run())
    assert attempts == ["sent"]
    assert gate.status == "ambiguous"


@pytest.mark.parametrize("reason", ["dispatch_timeout", "payload_mismatch", "route_unverified", "route_exception"])
def test_rejection_reason_and_shape_are_fixed_and_survive_private_record(tmp_path, reason):
    diagnostics = model_test.RunDiagnostics("codex")
    diagnostics.enter("notify")
    event = _completed_event() if reason != "dispatch_timeout" else threading.Event()
    def verify(*_):
        if reason == "route_exception":
            raise RuntimeError("synthetic-raw-exception-with-private-content")
        return "" if reason == "route_unverified" else "synthetic-session"
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: synthetic-private-nonce", verify, event, wait_seconds=0,
        diagnostics=diagnostics,
    )
    sends = []
    async def sender():
        sends.append(1)
    message = "synthetic-private-wrong-payload" if reason == "payload_mismatch" else "🦦 wa-test: synthetic-private-nonce\n"
    async def run():
        with pytest.raises(RuntimeError):
            await gate.send("synthetic-session", "synthetic-participant", message, {}, sender)
        with pytest.raises(RuntimeError, match="already reserved"):
            await gate.send("synthetic-session", "synthetic-participant", message, {}, sender)
    asyncio.run(run())
    assert sends == []
    record = diagnostics.as_record()
    assert record["notify_rejection"] == ("route_unverified" if reason == "route_exception" else reason)
    assert record["notify_payload_shape"] == ("other" if reason == "payload_mismatch" else "exact_plus_terminal_lf")
    diagnostics.fail("notify_rejected")
    path = model_test._write_failure_record(tmp_path, diagnostics)
    text = path.read_text()
    assert all(raw not in text for raw in (message, "synthetic-session", "synthetic-participant", "synthetic-raw-exception"))
    assert model_test._valid_diagnostic_record(__import__('json').loads(text))


@pytest.mark.parametrize("field", ["notify_rejection", "notify_payload_shape"])
@pytest.mark.parametrize("value", ["raw-private-text", [], {}, 1, None])
def test_new_diagnostic_fields_reject_non_enum_values(field, value):
    diagnostics = model_test.RunDiagnostics("codex")
    record = diagnostics.as_record()
    assert record["schema"] == 4 and model_test._valid_diagnostic_record(record)
    record[field] = value
    assert not model_test._valid_diagnostic_record(record)
    diagnostics.set_notify_details(rejection=value, payload_shape=value)
    assert model_test._valid_diagnostic_record(diagnostics.as_record())


def test_diagnostics_reject_extra_private_fields_and_legacy_schema_expansion():
    record = model_test.RunDiagnostics("codex").as_record()
    assert not model_test._valid_diagnostic_record(dict(record, payload="synthetic-private"))
    assert not model_test._valid_diagnostic_record(dict(record, schema=2))


@pytest.mark.parametrize("explicit", [
    None,
    {"adapter": "whatsapp", "participant": "user:42"},
])
def test_real_send_notification_passes_registry_routing_to_gate(
    tmp_path, monkeypatch, explicit,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    expected = "🦦 wa-test: nonce"
    attempts = []

    def verify(session, participant, supplied_routing):
        assert (session, participant) == ("s1", "user:42")
        assert supplied_routing is routing
        return "s1"

    async def fake_send(adapter, session, message, chat_id, thread_ts=None,
                        supplied_routing=None, **kwargs):
        attempts.append((adapter, session, message, chat_id, thread_ts,
                         supplied_routing, kwargs))
        return {"adapter": adapter, "participant": chat_id}

    gate = model_test.OneNonceSendGate(
        expected, verify, _completed_event(),
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(notify, "_send", model_test._wrap_notify_send(fake_send, gate))

    result = asyncio.run(notify.send_notification("s1", expected, explicit))

    assert result == {"adapter": "whatsapp", "participant": "user:42"}
    assert len(attempts) == 1
    assert attempts[0][5] is routing
    assert gate.status == "sent"


def test_real_send_notification_waits_for_dispatch_route_persistence(
    tmp_path, monkeypatch,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    expected = "🦦 wa-test: nonce"
    dispatch_done = threading.Event()
    route_persisted = {"value": False}
    attempts = []

    def verify(session, participant, supplied_routing):
        if (route_persisted["value"] and session == "s1"
                and participant == "user:42" and supplied_routing is routing):
            return "s1"
        return ""

    async def fake_send(*args, **kwargs):
        attempts.append((args, kwargs))
        return {"adapter": "whatsapp", "participant": "user:42"}

    gate = model_test.OneNonceSendGate(
        expected, verify, dispatch_done, wait_seconds=1,
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(notify, "_send", model_test._wrap_notify_send(fake_send, gate))

    async def run():
        pending = asyncio.create_task(notify.send_notification("s1", expected))
        await asyncio.sleep(0.02)
        assert not pending.done()
        assert gate.status == "dispatch_wait"
        assert attempts == []
        route_persisted["value"] = True
        dispatch_done.set()
        return await pending

    assert asyncio.run(run())["participant"] == "user:42"
    assert len(attempts) == 1
    assert gate.status == "sent"
    assert gate.verified_session == "s1"


def test_real_send_notification_fails_closed_after_dispatch_without_route(
    tmp_path, monkeypatch,
):
    import server.notify as notify

    routing = {
        "adapter": "whatsapp", "chat_id": "user:42",
        "whatsapp_account": "test-account", "wolt": "wa-test",
    }
    attempts = []
    gate = model_test.OneNonceSendGate(
        "🦦 wa-test: nonce", lambda *_: "", _completed_event(),
    )
    monkeypatch.setattr(notify, "OUTBOX_DIR", tmp_path / "outbox")
    monkeypatch.setattr(notify, "read_session_registry", lambda _session: routing)
    monkeypatch.setattr(
        notify, "_send",
        model_test._wrap_notify_send(
            lambda *args, **kwargs: _async_record(attempts, *args, **kwargs), gate,
        ),
    )

    with pytest.raises(RuntimeError, match="route was not verified"):
        asyncio.run(notify.send_notification("s1", "🦦 wa-test: nonce"))
    assert attempts == []
    assert gate.status == "rejected"


async def _async_record(records, *args, **kwargs):
    records.append((args, kwargs))
    return {"ok": True}


def test_cleanup_attempts_tmux_even_when_session_and_server_cleanup_fail(tmp_path):
    calls = []
    class Server:
        should_exit = False
    class Thread:
        def join(self, timeout):
            calls.append("join")
        def is_alive(self):
            return True
    class Listener:
        def close(self):
            calls.append("close")

    ok = model_test._cleanup_resources(
        Server(), Thread(), Listener(), "s1", {}, tmp_path,
        stop_session_fn=lambda _name: (_ for _ in ()).throw(RuntimeError("failed")),
        kill_tmux_fn=lambda _env, _root: calls.append("tmux") or True,
    )
    assert not ok
    assert calls == ["join", "close", "tmux"]


@pytest.fixture
def short_cleanup_base():
    with tempfile.TemporaryDirectory(prefix=".wt-", dir=ROOT.parents[2]) as name:
        yield Path(name)


def test_private_tmux_cleanup_only_accepts_confirmed_absence(short_cleanup_base, monkeypatch):
    import socket
    tmp_path = short_cleanup_base
    os.chmod(tmp_path, 0o700)
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", tmp_path)
    run = tmp_path / "model-test"
    root = model_test._prepare_private_tmux_dir(run, tmp_path)
    env = {"WOLTSPACE_TMUX_BIN": "tmux", "TMUX_TMPDIR": str(root)}
    monkeypatch.setattr(model_test, "_tmux_value", lambda *_: (_ for _ in ()).throw(AssertionError("no command needed")))
    assert not model_test._kill_private_tmux(env, root)  # Unknown is not never-started.
    assert model_test._kill_private_tmux(env, root, never_started=True)
    path = root / f"tmux-{os.getuid()}" / "default"
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    try:
        monkeypatch.setattr(model_test, "_tmux_value", lambda *_: (1, "", "no server running"))
        assert not model_test._kill_private_tmux(env, root)
        assert path.exists()  # No positive stop evidence, leave it untouched.
        sock.close()
        path.unlink()
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        monkeypatch.setattr(model_test, "_tmux_value", lambda *_: (1, "", "permission denied"))
        assert not model_test._kill_private_tmux(env, root)
        assert path.exists()
    finally:
        sock.close()
        path.unlink(missing_ok=True)


def test_private_tmux_path_preflight_checks_ownership_and_linux_limit(tmp_path):
    run_root = tmp_path / "model-12345678"
    run_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".wt-", dir=ROOT.parents[2]) as base_name:
        base = Path(base_name)
        socket_dir = model_test._prepare_private_tmux_dir(run_root, base)
        assert socket_dir.parent == base
        assert stat.S_IMODE(socket_dir.stat().st_mode) == 0o700
        candidate = socket_dir / f"tmux-{os.getuid()}" / "default"
        assert len(os.fsencode(candidate)) < 108

    long_base = Path("/tmp") / ("x" * 90)
    try:
        with pytest.raises(RuntimeError, match="socket path exceeds"):
            model_test._prepare_private_tmux_dir(run_root, long_base)
        assert not (long_base / run_root.name).exists()
    finally:
        if long_base.exists():
            long_base.rmdir()


def test_path_length_preflight_happens_before_private_key_read(tmp_path, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    monkeypatch.setattr(model_test.owner_test, "PRIVATE", private)
    monkeypatch.setattr(model_test.owner_test, "_check_private_dir", lambda: None)
    monkeypatch.setattr(
        model_test, "_auth_path", lambda *_: ("CLAUDE_CONFIG_DIR", tmp_path),
    )
    too_long = Path("/tmp") / ("y" * 90)
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", too_long)
    key_reads = []
    monkeypatch.setattr(
        model_test.owner_test, "_read_token", lambda: key_reads.append(True),
    )
    try:
        with pytest.raises(RuntimeError, match="socket path exceeds"):
            model_test._run_model_test("claude")
        assert key_reads == []
        records = list(private.glob("model-*/failure.json"))
        assert len(records) == 1
        assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
        assert __import__("json").loads(records[0].read_text())["stage"] == "preflight"
        assert sorted(p.name for p in records[0].parent.iterdir()) == ["failure.json"]
    finally:
        if too_long.exists():
            too_long.rmdir()
        for path in private.glob("model-*"):
            __import__("shutil").rmtree(path)


def test_real_tmux_private_socket_start_and_stop_smoke(tmp_path, monkeypatch):
    import shutil
    import tempfile

    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        pytest.skip("tmux is not installed")
    run_root = tmp_path / "model-smoke"
    run_root.mkdir(mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".wt-", dir=ROOT.parents[2]) as base_name:
        monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", Path(base_name))
        tmux_root = model_test._prepare_private_tmux_dir(run_root, Path(base_name))
        env = {
            "WOLTSPACE_TMUX_BIN": tmux_bin,
            "TMUX_TMPDIR": str(tmux_root),
            "PATH": os.environ.get("PATH", os.defpath),
            "HOME": str(tmp_path),
        }
        try:
            status, _, error = model_test._tmux_value(
                env, "new-session", "-d", "-s", "isolated-smoke", "sleep 30",
            )
            assert status == 0, error
            status, socket_path, error = model_test._tmux_value(
                env, "display-message", "-p", "#{socket_path}",
            )
            assert status == 0, error
            assert Path(socket_path).resolve().is_relative_to(tmux_root.resolve())
            assert model_test._tmux_value(
                env, "show-environment", "-g", "WHATSAPP_AGENT_TOKEN",
            )[0] != 0
            assert model_test._kill_private_tmux(env, tmux_root)
            assert not Path(socket_path).exists()
        finally:
            model_test._kill_private_tmux(env, tmux_root)
            shutil.rmtree(tmux_root, ignore_errors=True)


def test_cleanup_finalizer_retains_root_on_failure_and_removes_on_success(tmp_path, monkeypatch):
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    assert not model_test._finalize_run_root(retained, False)
    assert retained.is_dir()
    assert (retained / ".cleanup-required").is_file()

    disposable = tmp_path / "disposable"
    disposable.mkdir(mode=0o700)
    socket_base = Path(tempfile.mkdtemp(prefix="wt-", dir="/tmp"))
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", socket_base)
    tmux_root = model_test._prepare_private_tmux_dir(disposable, socket_base)
    assert model_test._finalize_run_root(disposable, True, tmux_root)
    assert not disposable.exists()
    assert not tmux_root.exists()


def test_main_passes_explicit_owner_tool_option_without_launch(monkeypatch):
    import sys
    calls = []
    monkeypatch.setattr(sys, "argv", ["runner", "--run", "--harness", "codex", "--notify-owner-tool"])
    monkeypatch.setattr(model_test.owner_test, "_require_tty", lambda: None)
    monkeypatch.setattr("builtins.input", lambda _: "MODEL")
    def run(harness, diagnostics, **options):
        calls.append((harness, options))
        return 0
    monkeypatch.setattr(model_test, "_run_model_test", run)
    assert model_test.main() == 0
    assert calls == [("codex", {"notify_owner_tool": True})]


@pytest.mark.parametrize("unsafe", ["parent_mode", "parent_symlink", "socket_symlink", "socket_regular", "wrong_root", "wrong_env", "foreign_owner", "permission"])
def test_never_created_socket_cleanup_fails_closed(short_cleanup_base, monkeypatch, unsafe):
    base = short_cleanup_base
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", base)
    root = model_test._prepare_private_tmux_dir(base / "model-never", base)
    parent = root / f"tmux-{os.getuid()}"
    socket = parent / "default"
    env = {"TMUX_TMPDIR": str(root)}
    if unsafe == "parent_mode":
        parent.chmod(0o755)
    elif unsafe == "parent_symlink":
        parent.rmdir(); parent.symlink_to(base, target_is_directory=True)
    elif unsafe == "socket_symlink":
        socket.symlink_to(base / "absent")
    elif unsafe == "socket_regular":
        socket.write_text("synthetic")
    elif unsafe == "wrong_root":
        monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", base / "other")
    elif unsafe == "wrong_env":
        env["TMUX_TMPDIR"] = "unverified"
    elif unsafe == "foreign_owner":
        monkeypatch.setattr(model_test.os, "getuid", lambda: -1)
    elif unsafe == "permission":
        original = Path.lstat
        def denied(path, *args, **kwargs):
            if path == socket: raise PermissionError("synthetic-private")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "lstat", denied)
    monkeypatch.setattr(model_test, "_tmux_value", lambda *_: (_ for _ in ()).throw(AssertionError("must not probe foreign/unverified state")))
    assert not model_test._kill_private_tmux(env, root)


def test_cleanup_component_failures_persist_fixed_outcomes(tmp_path):
    diag = model_test.RunDiagnostics("codex")
    def fail(*_): raise RuntimeError("synthetic-private-key participant nonce")
    server = SimpleNamespace(should_exit=False)
    thread = SimpleNamespace(join=fail, is_alive=lambda: True)
    listener = SimpleNamespace(close=fail)
    assert not model_test._cleanup_resources(server, thread, listener, "synthetic", {}, tmp_path, diag,
        stop_session_fn=fail, kill_tmux_fn=lambda *_: False)
    assert diag.cleanup_components == {
        "api_server":"failed", "listener":"failed", "session":"failed", "tmux":"failed", "finalize":"not_checked"}
    diag.enter("cleanup"); diag.fail("cleanup_failed")
    stored = model_test._write_failure_record(tmp_path, diag).read_text()
    assert "synthetic-private" not in stored
    assert model_test._valid_diagnostic_record(__import__('json').loads(stored))


@pytest.mark.parametrize("field,value", [
    ("operation","private"), ("http_status",True), ("http_status",600),
    ("provider_code","100"), ("provider_code",999), ("exception","private"),
    ("submission","delivered"), ("call_attempted",1),
])
def test_control_diagnostic_schema_rejects_untrusted_fields(field, value):
    record = model_test.RunDiagnostics("codex").as_record()
    record["control_reply"][field] = value
    assert not model_test._valid_diagnostic_record(record)


def test_cleanup_component_schema_rejects_extra_or_raw_outcomes():
    record = model_test.RunDiagnostics("codex").as_record()
    record["cleanup_components"]["tmux"] = "private-exception"
    assert not model_test._valid_diagnostic_record(record)
    record = model_test.RunDiagnostics("codex").as_record()
    record["cleanup_components"]["private"] = "passed"
    assert not model_test._valid_diagnostic_record(record)


def test_private_tmux_verification_keeps_exact_root_and_token_checks(tmp_path, monkeypatch):
    root = tmp_path / "model-verify"
    calls = []
    def value(env, *args):
        calls.append(args)
        if args[0] == "display-message":
            return 0, str(root / "tmux-1000/default"), ""
        return 1, "", ""
    monkeypatch.setattr(model_test, "_tmux_value", value)
    model_test._verify_private_tmux({}, root, "synthetic")
    assert len(calls) == 3
    monkeypatch.setattr(model_test, "_tmux_value", lambda *_: (0, str(tmp_path / "other/default"), ""))
    with pytest.raises(RuntimeError, match="outside"):
        model_test._verify_private_tmux({}, root, "synthetic")


def test_control_diagnostics_output_is_fixed_and_submission_not_inferred():
    diag = model_test.RunDiagnostics("codex")
    def fail(*_): raise TimeoutError("synthetic-private-key participant nonce")
    tracked = model_test._track_adapter_replies(fail, diag)
    with pytest.raises(TimeoutError):
        tracked("POST", "/messages", {"text":{"body":"Selected wa-test. private"}})
    diag.set_cleanup("tmux", "failed")
    output = model_test._diagnostic_line(diag)
    assert "control_operation=selection call_attempted=true submission=unknown http_status=None provider_code=None exception=timeout" in output
    assert "cleanup_failed_components=tmux" in output
    assert all(value not in output for value in ("synthetic-private", "participant", "nonce", "Selected"))


def test_control_diagnostic_schema_requires_consistent_call_response_facts():
    record = model_test.RunDiagnostics("codex").as_record()
    record["control_reply"]["operation"] = "selection"
    assert not model_test._valid_diagnostic_record(record)
    record["control_reply"].update(call_attempted=True, submission="unknown")
    assert model_test._valid_diagnostic_record(record)
    record["control_reply"]["http_status"] = 400
    assert not model_test._valid_diagnostic_record(record)
    record["control_reply"]["submission"] = "http_response_received"
    assert model_test._valid_diagnostic_record(record)


def test_source_conftest_prevents_inherited_install_collection_contamination(tmp_path):
    import subprocess, sys
    conflicting = tmp_path / "synthetic-install"
    lib = conflicting / "container/lib"
    lib.mkdir(parents=True)
    (lib / "sessions.py").write_text("raise AssertionError('wrong synthetic install imported')\n")
    code = r"""
import inspect, os, runpy, sys
from pathlib import Path
root, conflicting = map(Path, sys.argv[1:])
assert Path(os.environ["WOLTSPACE_DIR"]) == conflicting
# Pytest loads conftest before collecting modules, independently of test order.
runpy.run_path(str(root / "test/conftest.py"))
from server.notification_senders import whatsapp_sender
import sessions, harnesses
from server import config
assert config.WOLTSPACE_DIR == root
assert Path(sessions.__file__).resolve() == root / "container/lib/sessions.py"
assert Path(harnesses.__file__).resolve() == root / "container/lib/harnesses.py"
assert "whatsapp_account" in inspect.signature(sessions.SessionRegistry.create).parameters
# Explicit local config-override tests remain supported after collection.
import pytest
with pytest.MonkeyPatch.context() as patch:
    patch.setenv("WOLTSPACE_DIR", str(conflicting))
    assert runpy.run_path(str(root / "server/config.py"))["WOLTSPACE_DIR"] == conflicting
print("source_imports_verified")
"""
    env = {"PATH":os.environ.get("PATH", os.defpath), "HOME":str(tmp_path),
           "WOLTSPACE_DIR":str(conflicting), "WOLTSPACE_WOLTS_DIR":str(tmp_path / "wolts"),
           "PYTHONPATH":os.pathsep.join(map(str, model_test._PATHS)), "PYTHONDONTWRITEBYTECODE":"1"}
    result = subprocess.run([sys.executable, "-c", code, str(ROOT), str(conflicting)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "source_imports_verified" in result.stdout


@pytest.mark.parametrize("requested", [None, True, False])
def test_absent_socket_cleanup_and_finalizer_require_never_requested(
    tmp_path, short_cleanup_base, monkeypatch, requested,
):
    base = short_cleanup_base
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", base)
    root = model_test._prepare_private_tmux_dir(base / "model-absent", base)
    run = tmp_path / root.name
    run.mkdir(mode=0o700)
    diag = model_test.RunDiagnostics("codex")
    calls = []
    stopped = model_test._cleanup_resources(None, None, None,
        "synthetic" if requested is not False else "", {"TMUX_TMPDIR":str(root)}, root,
        diag, requested, stop_session_fn=lambda _: calls.append("registry") or {"status":"stopped"})
    assert stopped is (requested is False)
    assert diag.cleanup_components["tmux"] == ("not_needed" if requested is False else "failed")
    # A registry status alone does not override uncertain server cleanup.
    assert calls == ([] if requested is False else ["registry"])
    diag.enter("cleanup"); diag.fail("cleanup_failed")
    model_test._write_failure_record(run, diag)
    finalized = model_test._finalize_run_root(run, stopped, root, preserve_failure=True)
    assert finalized is (requested is False)
    assert (run / ".cleanup-required").exists() is (requested is not False)
    assert root.exists() is (requested is not False)


def test_private_server_stop_precedes_registry_stamp(tmp_path):
    calls = []
    assert model_test._cleanup_resources(None, None, None, "synthetic", {}, tmp_path,
        session_start_requested=True,
        kill_tmux_fn=lambda *_: calls.append("verified-private-server-stop") or True,
        stop_session_fn=lambda _: calls.append("registry-stop") or {"status":"stopped"})
    assert calls == ["verified-private-server-stop", "registry-stop"]


def test_live_sleep_only_tmux_with_unlinked_socket_is_not_stopped(short_cleanup_base, monkeypatch):
    import select, shutil, signal
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.skip("pidfd identity cleanup required for this Linux-only probe")
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        pytest.skip("tmux not installed")
    base = short_cleanup_base
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", base)
    root = model_test._prepare_private_tmux_dir(base / "model-unlink", base)
    env = {"WOLTSPACE_TMUX_BIN":tmux_bin, "TMUX_TMPDIR":str(root),
           "HOME":str(root), "PATH":os.environ.get("PATH",os.defpath)}
    descriptors = []
    try:
        assert model_test._tmux_value(env, "new-session", "-d", "-s", "isolated-unlink", "exec sleep 30")[0] == 0
        for variable, executable in (("#{pid}", "tmux"), ("#{pane_pid}", "sleep")):
            status, value, _ = model_test._tmux_value(env, "display-message", "-p", variable)
            assert status == 0 and value.isdecimal()
            pid = int(value)
            descriptor = os.pidfd_open(pid)
            descriptors.append(descriptor)
            assert Path(f"/proc/{pid}/exe").resolve().name == executable
            assert model_test._tmux_value(env, "display-message", "-p", variable)[1] == value
            assert not select.select([descriptor], [], [], 0)[0]
        socket = root / f"tmux-{os.getuid()}" / "default"
        assert stat.S_ISSOCK(socket.lstat().st_mode)
        socket.unlink()  # Only this exact test-created socket.
        assert not model_test._kill_private_tmux(env, root)
        assert not select.select([descriptors[0]], [], [], 0)[0]  # Server still alive.
    finally:
        # Held pidfds cannot retarget a reused PID. Never inspect/kill other processes.
        for descriptor in reversed(descriptors):
            try:
                signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                if not select.select([descriptor], [], [], 2)[0]:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
                assert select.select([descriptor], [], [], 2)[0]
            except ProcessLookupError:
                pass
            finally:
                os.close(descriptor)
        if not descriptors:
            model_test._kill_private_tmux(env, root)


def test_late_finalization_failure_cannot_return_success(tmp_path, short_cleanup_base, monkeypatch):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    auth = tmp_path / "synthetic-auth"
    auth.mkdir(mode=0o700)
    monkeypatch.setattr(model_test.owner_test, "PRIVATE", private)
    monkeypatch.setattr(model_test.owner_test, "_check_private_dir", lambda: None)
    monkeypatch.setattr(model_test.owner_test, "_read_token", lambda: "synthetic-only")
    monkeypatch.setattr(model_test, "PRIVATE_TMUX_BASE", short_cleanup_base)
    monkeypatch.setattr(model_test, "_auth_path", lambda *_: ("CODEX_HOME", auth))
    monkeypatch.setattr(model_test, "_preflight_harness", lambda *_: None)
    def no_process_runner(*args, **kwargs):
        args[-1].set_check("cleanup", "passed")
        return 0
    monkeypatch.setattr(model_test, "_run_model_session", no_process_runner)
    monkeypatch.setattr(model_test, "_finalize_run_root", lambda *_args, **_kwargs: False)
    diag = model_test.RunDiagnostics("codex")
    assert model_test._run_model_test("codex", diag) == 2
    assert diag.category == "cleanup_failed"
    files = list(private.glob("model-*/failure.json"))
    assert len(files) == 1
    record = __import__('json').loads(files[0].read_text())
    assert record["cleanup_components"]["finalize"] == "failed"
    assert model_test._valid_diagnostic_record(record)
    assert "synthetic-only" not in files[0].read_text()


# Provider-control diagnostic coverage belongs to this runner layer.
from test_whatsapp_agent import _load_adapter
import io
import json

@__import__("pytest").mark.parametrize("fault,status,code,category,submission", [
    ("non2xx", 400, 100, "none", "http_response_received"),
    ("invalid_key", 401, 100, "terminal_configuration", "http_response_received"),
    ("rate_exhaustion", 429, None, "rate_limited", "http_response_received"),
    ("timeout", None, None, "timeout", "unknown"),
    ("connection", None, None, "connection", "unknown"),
    ("other", None, None, "other", "unknown"),
    ("unlisted_code", 500, None, "other", "http_response_received"),
])
@__import__("pytest").mark.parametrize("operation", ["selection", "session_ack"])
def test_control_reply_tracking_retains_only_fixed_safe_fields(
    monkeypatch, tmp_path, fault, status, code, category, submission, operation,
):
    import contextlib
    import urllib.error
    from scripts import whatsapp_model_session_test as runner
    adapter = _load_adapter(monkeypatch, tmp_path)
    calls, sleeps = [], []
    raw_private = "synthetic-key user:private nonce-private https://private.invalid Authorization: Bearer private"
    def transport(*args, **kwargs):
        calls.append(1)
        if fault == "timeout":
            raise TimeoutError(raw_private)
        if fault == "connection":
            raise urllib.error.URLError(ConnectionRefusedError(raw_private))
        if fault == "other":
            raise RuntimeError(raw_private)
        body = {"error": {"code": 100 if fault == "invalid_key" else "private" if fault == "unlisted_code" else 999,
                          "message": raw_private, "access_token": raw_private}}
        raise urllib.error.HTTPError("https://private.invalid", status, raw_private,
                                     {"Retry-After": "1", "Authorization": raw_private},
                                     io.BytesIO(json.dumps(body).encode()))
    monkeypatch.setattr(adapter.urllib.request, "urlopen", transport)
    monkeypatch.setattr(adapter, "request_slot", lambda *args: contextlib.nullcontext())
    monkeypatch.setattr(adapter.time, "sleep", sleeps.append)
    api = adapter.api
    if fault == "non2xx":
        api = lambda *_: (400, {"error": {"code":100, "message":raw_private}})
    diagnostics = runner.RunDiagnostics("codex")
    tracked = runner._track_adapter_replies(api, diagnostics)
    body = "Selected wa-test. fixed" if operation == "selection" else "Session started: fixed"
    try:
        tracked("POST", "/messages", {"to":"user:private", "text":{"body":body}})
    except Exception:
        pass
    expected = {"operation":operation, "call_attempted":True, "submission":submission,
                "http_status":status, "provider_code":code, "exception":category}
    record = diagnostics.as_record()
    assert record["control_reply"] == expected
    assert runner._valid_diagnostic_record(record)
    assert record["checks"]["selection_reply" if operation == "selection" else "session_ack"] == "failed"
    assert len(calls) == (0 if fault == "non2xx" else 3 if fault == "rate_exhaustion" else 1)
    assert sleeps == ([1, 1] if fault == "rate_exhaustion" else [])
    path = runner._write_failure_record(tmp_path, diagnostics)
    stored = path.read_text()
    assert all(value not in stored for value in (raw_private, "private.invalid", "Bearer", "user:private", body))
    assert runner._valid_diagnostic_record(json.loads(stored))
