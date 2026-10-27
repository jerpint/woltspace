#!/usr/bin/env python3
"""Bounded real Codex startup → local mocked notify inspection. Review before launch."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import whatsapp_model_session_test as model
from scripts import startup_transport_probe as transport

PARTICIPANT = "user:4242424242"
SYNTHETIC_TOKEN = "synthetic-local-notify-no-provider"
ACCOUNT = hashlib.sha256(SYNTHETIC_TOKEN.encode()).hexdigest()[:20]
BODY = "startup-inspection-ok"
PROMPT = (
    "This is an isolated startup inspection. Use the notify CLI exactly once "
    "to send the text startup-inspection-ok via --whatsapp user:4242424242. "
    "Pass the text through standard input. The endpoint is a local mock and "
    "does not contact WhatsApp. Do nothing else."
)
PROBE_PROMPT = (
    "This is an isolated local transport inspection. Run ./startup-notify-probe exactly once "
    "using your shell tool. This executable performs the checks and owns the notify call; "
    "it supplies startup-inspection-ok on standard input. Use only that executable for this "
    "task and do nothing else. It contacts only the private local test server."
)
APPROVAL_PROBE_PROMPT = (
    PROBE_PROMPT + " Prior inspection established that the command sandbox blocks the local API. "
    "BEFORE the first helper invocation, request normal one-time shell-tool approval using "
    "sandbox_permissions=require_escalated and a justification explaining access to the isolated "
    "local Woltspace API. Wait for the current reviewer; do not invoke the helper without approval. "
    "Do not request a persistent prefix grant, change network/sandbox/reviewer settings, or answer "
    "any dialog yourself. Existing feature authorization does not replace runtime approval. "
    "If approval is denied or unavailable, report that blocker and stop. Never retry an ambiguous send."
)
OWNER_TOOL_PROMPT = (
    "This is an isolated local notification inspection. Call the notify_owner MCP tool "
    "exactly once with message startup-inspection-ok. Do not add a name prefix. "
    "The lodge endpoint is mocked and does not contact WhatsApp. Use that tool only, "
    "not the notify CLI or shell. Do not repeat an ambiguous outcome or change permissions."
)
MAX_SECONDS = 180
SNAPSHOT_NAME = "startup-snapshot.json"


def _redact_pane(text):
    """Redact in memory before retaining bounded synthetic UI text."""
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    text = re.sub(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----",
                  "[REDACTED PRIVATE KEY]", text)
    # Whole-line removal also covers multiple JSON fields and quoted values.
    text = re.sub(
        r"(?im)^.*\b(?:[A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|KEY|USER_CODE|CREDENTIAL)[A-Z0-9_]*|"
        r"authorization|api[ _-]?key|private[ _-]?key|device[ _-]?code|login[ _-]?code|"
        r"verification[ _-]?code)\b\s*[\"']?\s*(?:=|:|\bis\b).*$",
        "[REDACTED CREDENTIAL LINE]", text,
    )
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [REDACTED]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED JWT]", text)
    text = re.sub(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{8,}\b", "[REDACTED KEY]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@:]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(r"[A-Za-z0-9._~+/-]{32,}={0,2}", "[REDACTED OPAQUE VALUE]", text)
    text = "".join(c for c in text if c.isprintable() or c in "\n\t")
    return text.encode("utf-8")[:8192].decode("utf-8", errors="ignore")


from scripts.whatsapp_notify_counter import _NotifyCounter


def _capture_snapshot(session, wolts_dir, env, tmux_root, directory_trust, counter,
                      *, prompt=PROMPT, probe=None, host_health="not_checked"):
    """Capture only the registry's verified exact pane before cleanup."""
    snapshot = {
        "schema": 2, "directory_trust": directory_trust, "synthetic_prompt": prompt,
        "pane_status": "unavailable", "pane_text": "",
        "boot_input_delivery": "cli_argument_consumption_unconfirmed",
        "wrapper_started": False, "deferred_prompt_hook_ran": False,
        "harness_registry_status": "unknown", "harness_exit_code": None,
        "harness_rollout_observed": False,
        "notify_requests": 0, "notify_last_status": None,
        "transport_probe": probe, "host_health": host_health,
    }
    if counter is not None:
        with counter.lock:
            snapshot["notify_requests"] = min(counter.count, 10000)
            snapshot["notify_last_status"] = counter.status
    try:
        record = model._record_for_test(session, wolts_dir) if session else None
        if not record:
            return snapshot
        status = record.get("status")
        if status in {"running", "completed", "failed", "stopped", "resting"}:
            snapshot["harness_registry_status"] = status
        code = record.get("exit_code")
        if type(code) is int and -255 <= code <= 255:
            snapshot["harness_exit_code"] = code
        snapshot["harness_rollout_observed"] = bool(record.get("harness_session_id"))
        # Only this private synthetic root's log is inspected, and only fixed
        # markers for the known session leave memory. No log text is retained.
        boot_log = wolts_dir / ".space" / "logs" / "session-boot.log"
        try:
            if (boot_log.is_file() and not boot_log.is_symlink()
                    and boot_log.resolve().is_relative_to(wolts_dir.resolve())):
                with boot_log.open(encoding="utf-8", errors="replace") as source:
                    markers = source.read(65536)
                snapshot["wrapper_started"] = f"] spawn session: {session}\n" in markers
                snapshot["deferred_prompt_hook_ran"] = f"] deliver-prompt {session}:" in markers
        except OSError:
            pass
        if not _verify_record(session, wolts_dir, env, tmux_root):
            return snapshot
        runtime = record["runtime"]
        pane, target = runtime.get("pane_id", ""), runtime.get("tmux_session_name", "")
        if not re.fullmatch(r"%[0-9]+", pane) or not target:
            return snapshot
        status, panes, _ = model._tmux_value(env, "list-panes", "-t", target, "-F", "#{pane_id}")
        if status or pane not in panes.splitlines():
            return snapshot
        status, text, _ = model._tmux_value(env, "capture-pane", "-p", "-t", pane)
        if not status:
            snapshot.update(pane_status="captured", pane_text=_redact_pane(text))
    except Exception:
        pass  # Never retain or emit exception objects, owner logs or environments.
    return snapshot


def _write_snapshot(run_root, snapshot):
    fd = os.open(run_root / SNAPSHOT_NAME,
                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(snapshot, out, ensure_ascii=False)
        out.write("\n")


def _status_line(diagnostics):
    record = diagnostics.as_record()
    if not model._valid_diagnostic_record(record):
        return "Startup inspection status=invalid_diagnostics."
    return (
        f"Startup inspection process={record['harness_process']} "
        f"readiness={record['harness_readiness']} symptom={record['boot_symptom']} "
        f"notify_attempt={record['notify_attempt']} cleanup={record['checks']['cleanup']}."
    )


def _start_session(prompt=PROMPT):
    from sessions import start_session
    # Supported platform creation, including registry, bootstrap and runtime.
    # Prompt is explicit even though the fresh private grant store defaults to it.
    return start_session(
        wolt=model.TEST_WOLT, harness="codex", prompt=prompt,
        execution_policy="prompt",
        routing={"adapter": "whatsapp", "chat_id": PARTICIPANT,
                 "whatsapp_account": ACCOUNT},
    )


def _prepare_transport_probe(wolt_dir, endpoint):
    config = wolt_dir / ".state" / "transport-probe-config.json"
    transport.write_json(config, {
        "endpoint": endpoint, "session": "", "notify": str(ROOT / "container/bin/notify"),
        "net_namespace": transport.net_namespace(),
    })
    executable = wolt_dir / "startup-notify-probe"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from scripts.startup_transport_probe import run\n"
        "raise SystemExit(run(Path(__file__).parent / '.state/transport-probe-config.json'))\n",
    )
    executable.chmod(0o700)
    return config


def _read_probe(config):
    path = config.with_name("transport-probe-result.json")
    try:
        import stat
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            return None
        data = json.loads(path.read_text())
        return data if transport.valid_result(data) else None
    except (OSError, ValueError, TypeError):
        return None


def _verify_record(session: str, wolts_dir: Path, env: dict,
                   tmux_root: Path) -> bool:
    record = model._record_for_test(session, wolts_dir)
    if not record:
        return False
    if not (
        record.get("adapter") == "whatsapp"
        and record.get("chat_id") == PARTICIPANT
        and record.get("whatsapp_account") == ACCOUNT
        and record.get("harness") == "codex"
        and record.get("wolt") == model.TEST_WOLT
        and isinstance(record.get("runtime"), dict) and record["runtime"]
        and (record.get("execution_policy") or {}).get("mode") == "prompt"
    ):
        return False
    model._verify_private_tmux(env, tmux_root, session)
    return True


def _observe_boot(session: str, env: dict, diagnostics, *, allow_tool_approval=False,
                  owner_tool=False) -> bool:
    process = model._harness_process_status(session, "codex")
    diagnostics.set_harness_process(
        "present" if process is True else "absent" if process is False else "unknown",
    )
    diagnostics.enter("harness_boot")
    symptom = model._capture_boot_symptom(env, session, visible_only=True)
    if owner_tool and symptom in {"trust_or_approval", "waiting_for_input"}:
        diagnostics.set_boot_symptom(symptom)
        diagnostics.set_harness_readiness("unknown")
        return True  # Echoed instructions alone are not a displayed prompt.
    if allow_tool_approval and model._observe_notify_approval(diagnostics, symptom):
        return True  # Human may answer a real tool request; runner never does.
    if model._handle_boot_symptom(diagnostics, symptom):
        return False
    readiness = model._harness_readiness_status(session, "codex")
    diagnostics.set_harness_readiness(readiness)
    if readiness == "ready_marker_seen":
        diagnostics.passed("harness_boot")
    return True


def _approval_notice(session, wolts_dir, env, tmux_root, seconds):
    if not _verify_record(session, wolts_dir, env, tmux_root):
        raise RuntimeError("refusing unverified test-session attach instructions")
    record = model._record_for_test(session, wolts_dir)
    target = record["runtime"].get("tmux_session_name", "")
    model._print_approval_notice(target, tmux_root, seconds, command="startup-notify-probe")


def _directory_trust_environment(env: dict, run_root: Path, mode: str) -> None:
    """Allow normal trust writes only to the fresh private Codex config."""
    if mode not in {"preserve", "normal"}:
        raise ValueError("unsupported directory trust inspection mode")
    home = Path(env.get("CODEX_HOME", ""))
    expected = run_root / "codex-home"
    if (home.is_symlink() or home.resolve() != expected.resolve()
            or not home.is_dir() or home.stat().st_uid != os.getuid()
            or (home / "config.toml").exists() or (home / "config.toml").is_symlink()):
        raise RuntimeError("startup inspection requires a fresh private Codex config")
    if mode == "preserve":
        env["WOLTSPACE_PRESERVE_TRUST_PROMPTS"] = "1"
    else:
        env.pop("WOLTSPACE_PRESERVE_TRUST_PROMPTS", None)


def _run_startup_session(
    harness, inherited, auth_variable, auth_path, run_root, tmux_root, diagnostics,
    *, timeout_seconds=120, directory_trust="preserve", capture_snapshot=False,
    probe_transport=False, request_notify_approval=False, notify_owner_tool=False,
):
    """Real local server/session machinery; every outbound sender is mocked."""
    if (harness != "codex" or not 1 <= timeout_seconds <= MAX_SECONDS
            or request_notify_approval and not probe_transport
            or notify_owner_tool and (probe_transport or request_notify_approval or directory_trust != "normal")):
        raise ValueError("unsupported startup inspection settings")
    wolts_dir = run_root / "wolts"
    listener, port = model._bind_local_socket()
    env = model._private_environment(
        run_root, wolts_dir, harness, auth_variable, auth_path,
        "127.0.0.1", port, inherited, tmux_root,
    )
    if notify_owner_tool:
        env["WOLTSPACE_NOTIFY_TOOL"] = "1"
    try:
        _directory_trust_environment(env, run_root, directory_trust)
    except BaseException:
        listener.close()
        raise
    old_env, old_path = os.environ.copy(), list(sys.path)
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    session = ""
    session_start_requested = False
    server = thread = None
    notify_module = None
    original_send = original_dotenv = None
    server_errors = []
    dispatch_done = threading.Event()
    gate = None
    counter = None
    probe_config = probe_result = None
    host_health = "not_checked"
    prompt = OWNER_TOOL_PROMPT if notify_owner_tool else APPROVAL_PROBE_PROMPT if request_notify_approval else PROBE_PROMPT if probe_transport else PROMPT
    fd = os.open(run_root / "runner.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(p) for p in model._PATHS]
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            import uvicorn
            import server.app as server_app
            import server.notify as notify_module

            original_send, original_dotenv = notify_module._send, notify_module.dotenv_env

            def verify(candidate, participant, routing):
                if (candidate != session or participant != PARTICIPANT
                        or not isinstance(routing, dict)
                        or routing.get("adapter") != "whatsapp"
                        or routing.get("chat_id") != PARTICIPANT
                        or routing.get("whatsapp_account") != ACCOUNT
                        or routing.get("wolt") != model.TEST_WOLT):
                    return ""
                return session if _verify_record(session, wolts_dir, env, tmux_root) else ""

            gate = model.OneNonceSendGate(
                model._expected_notify_payload(BODY), verify, dispatch_done,
                wait_seconds=min(timeout_seconds, model.DISPATCH_WAIT_SECONDS),
                diagnostics=diagnostics,
            )

            async def mocked_sender():
                # No provider call or credential lookup exists on this path.
                return {"adapter": "whatsapp", "mocked": True}

            async def local_send(
                adapter_name, candidate, message, chat_id, thread_ts=None,
                routing=None, attachments=(), speech=None, session_record=None,
            ):
                if adapter_name != "whatsapp" or attachments or speech:
                    gate._set_status("rejected")
                    gate.failure = "startup inspection refused unsupported notification"
                    raise RuntimeError("startup inspection refused unsupported notification")
                return await gate.send(candidate, str(chat_id), message, routing, mocked_sender)

            # Replace the entire outbound boundary, not just WhatsApp transport:
            # a malformed route cannot fall back to a live Telegram/Slack sender.
            notify_module._send = local_send
            notify_module.dotenv_env = lambda name: ({
                "WOLTSPACE_NOTIFY_TOOL": "1", "WHATSAPP_AGENT_TOKEN": SYNTHETIC_TOKEN,
            }.get(name, "") if notify_owner_tool else "")
            counter = _NotifyCounter(server_app.app)
            server = uvicorn.Server(uvicorn.Config(
                counter, host="127.0.0.1", port=port, log_level="error",
                access_log=False, log_config=None, lifespan="off",
            ))

            def serve():
                try:
                    asyncio.run(server.serve(sockets=[listener]))
                except BaseException:
                    server_errors.append("server_start_failed")

            thread = threading.Thread(target=serve, name="startup-inspection-api", daemon=True)
            thread.start()
            diagnostics.enter("server_health")
            model._wait_for_server(env["WOLTSPACE_API"], wolts_dir, thread, server_errors)
            diagnostics.passed("server_health")
            if probe_transport:
                probe_config = _prepare_transport_probe(wolts_dir / model.TEST_WOLT, env["WOLTSPACE_API"])
            diagnostics.enter("session_spawn")
            deadline = time.monotonic() + timeout_seconds
            try:
                session_start_requested = True  # Before dispatch, including ambiguous throws.
                result = _start_session(prompt) if probe_transport or notify_owner_tool else _start_session()
                session = result.get("name", "") if isinstance(result, dict) else ""
                if probe_config is not None:
                    config = transport.read_config(probe_config)
                    config["session"] = session
                    transport.write_json(probe_config, config)
                if not session or not _verify_record(session, wolts_dir, env, tmux_root):
                    diagnostics.fail("session_not_started")
                    raise RuntimeError("startup session could not be verified")
                diagnostics.passed("session_spawn")
            finally:
                dispatch_done.set()
            if notify_owner_tool:
                deadline = time.monotonic() + timeout_seconds
                model._say("Owner-tool inspection awaiting one local mocked tool notification; no dialog will be answered.")
            if request_notify_approval:
                _approval_notice(session, wolts_dir, env, tmux_root, timeout_seconds)
            next_notice = time.monotonic() + 15
            while time.monotonic() < deadline:
                if request_notify_approval and probe_result is None and time.monotonic() >= next_notice:
                    model._say("Approval-needed task still pending; no helper result observed. "
                               f"Remaining observation budget: {max(0, int(deadline-time.monotonic()))}s.")
                    next_notice = time.monotonic() + 15
                if probe_config is not None:
                    probe_result = _read_probe(probe_config)
                    if probe_result is not None:
                        if host_health == "not_checked":
                            host_health = transport.health(env["WOLTSPACE_API"])
                        if probe_result["phase"] == "complete" and probe_result["notify"] != "sent":
                            diagnostics.enter("notify")
                            diagnostics.fail("local_transport_probe_failed")
                            raise RuntimeError("local transport probe did not pass")
                if server_errors:
                    diagnostics.enter("server_health")
                    diagnostics.fail("server_start_failed")
                    raise RuntimeError("local startup server stopped")
                if gate.failure:
                    diagnostics.enter("notify")
                    diagnostics.fail("notify_ambiguous" if gate.status == "ambiguous"
                                     else "notify_rejected")
                    raise RuntimeError("local mocked notification failed")
                if (model._confirmed_notify(gate, session, diagnostics)
                        and (probe_config is None or probe_result is not None and probe_result["phase"] == "complete")):
                    if notify_owner_tool:
                        with counter.lock:
                            if counter.owner_count != 1 or counter.legacy_count:
                                diagnostics.fail("notify_rejected")
                                raise RuntimeError("owner-tool inspection saw an unexpected endpoint")
                        model._say("Owner-tool requests=1 legacy-notify=0.")
                    model._say("Startup inspection received one verified local mocked notification.")
                    return 0
                if not _observe_boot(session, env, diagnostics, allow_tool_approval=request_notify_approval,
                                     owner_tool=notify_owner_tool):
                    raise RuntimeError("startup inspection detected an interactive boot prompt")
                time.sleep(0.2)
            diagnostics.enter("notify")
            if request_notify_approval and probe_result is None:
                model._say("Approval-needed task did not complete before the observation deadline; "
                           "no helper result observed. This does not prove approval was denied. Cleaning up.")
                diagnostics.fail("approval_not_completed")
            else:
                diagnostics.fail("run_timeout")
            raise TimeoutError("startup inspection timed out")
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        # Capture while the pane still exists, then write only sanitized data
        # after scoped cleanup. The snapshot never contains auth file contents.
        if probe_config is not None:
            probe_result = _read_probe(probe_config)
        snapshot = _capture_snapshot(
            session, wolts_dir, env, tmux_root, directory_trust, counter,
            prompt=prompt, probe=probe_result, host_health=host_health,
        ) if capture_snapshot else None
        diagnostics.enter("cleanup")
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            cleanup_ok = model._cleanup_resources(server, thread, listener, session, env, tmux_root, diagnostics, session_start_requested)
        diagnostics.set_check("cleanup", "passed" if cleanup_ok else "failed")
        if not cleanup_ok:
            diagnostics.fail("cleanup_failed")
        if notify_module is not None and original_send is not None:
            notify_module._send = original_send
            notify_module.dotenv_env = original_dotenv
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        log.close()
        model._remove_raw_log(run_root)
        if snapshot is not None:
            if counter is not None:
                with counter.lock:
                    snapshot["notify_requests"] = min(counter.count, 10000)
                    snapshot["notify_last_status"] = counter.status
            try:
                _write_snapshot(run_root, snapshot)
                diagnostics.private_snapshot_written = True
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--harness", choices=("codex",), required=True)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--directory-trust", choices=("preserve", "normal"), default="preserve")
    parser.add_argument("--capture-snapshot", action="store_true")
    parser.add_argument("--probe-transport", action="store_true")
    parser.add_argument("--request-notify-approval", action="store_true")
    parser.add_argument("--notify-owner-tool", action="store_true")
    args = parser.parse_args()
    if args.request_notify_approval and not args.probe_transport:
        parser.error("--request-notify-approval requires --probe-transport")
    if args.notify_owner_tool and (args.probe_transport or args.request_notify_approval or args.directory_trust != "normal"):
        parser.error("--notify-owner-tool requires normal directory trust and no CLI probe/approval mode")
    if not args.run or not 1 <= args.timeout <= MAX_SECONDS:
        parser.error("--run and a timeout between 1 and 180 seconds are required")
    diagnostics = model.RunDiagnostics("codex")
    try:
        model._say(f"Startup inspection directory_trust={args.directory_trust}.")
        result = model._run_model_test(
            "codex", diagnostics,
            startup_runner=lambda *items: _run_startup_session(
                *items, timeout_seconds=args.timeout, directory_trust=args.directory_trust,
                capture_snapshot=args.capture_snapshot,
                probe_transport=args.probe_transport,
                request_notify_approval=args.request_notify_approval,
                notify_owner_tool=args.notify_owner_tool,
            ),
        )
        if result:
            model._say(model._diagnostic_line(diagnostics))
        else:
            model._say("Startup inspection cleanup=passed.")
        model._say(_status_line(diagnostics))
        if args.capture_snapshot:
            model._say("Startup inspection snapshot=" + (
                "saved." if getattr(diagnostics, "private_snapshot_written", False) else "unavailable."))
        return result
    except KeyboardInterrupt:
        model._say("Startup inspection interrupted; isolated cleanup was attempted.")
        return 130
    except Exception:
        if not diagnostics.category:
            diagnostics.fail()
        model._say(model._diagnostic_line(diagnostics))
        model._say(_status_line(diagnostics))
        if args.capture_snapshot:
            model._say("Startup inspection snapshot=" + (
                "saved." if getattr(diagnostics, "private_snapshot_written", False) else "unavailable."))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
