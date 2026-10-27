#!/usr/bin/env python3
"""Owner-operated real WhatsApp → model session → notify test.

This is deliberately separate from whatsapp_owner_test.py. It is guarded by
an interactive confirmation and uses a fresh private data root, loopback-only
uvicorn socket, and private tmux socket. Do not launch before review.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import hashlib
import io
import json
import os
import secrets
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_TMUX_BASE = ROOT.parents[2] / ".whatsapp-model-tmux"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import scripts.whatsapp_owner_test as owner_test

TEST_WOLT = "wa-test"
TIMEOUT_SECONDS = 300
DISPATCH_WAIT_SECONDS = 45
NOTIFY_APPROVAL_INSTRUCTION = (
    "This isolated test uses the local notify API, whose access from the command sandbox "
    "was previously confirmed blocked. Load the general notify skill (woltspace-notify for copy "
    "delivery, woltspace:notify for plugin delivery). BEFORE the first "
    "notify invocation, request normal one-time shell-tool approval using "
    "sandbox_permissions=require_escalated with a justification explaining access to the "
    "isolated local Woltspace API. Keep the exact participant route and nonce on literal stdin. "
    "Wait for the current reviewer; feature authorization does not replace runtime approval. "
    "Do not request a persistent prefix grant, change network/sandbox/reviewer settings, or "
    "answer dialogs yourself. If approval is denied or unavailable, report that blocker and stop. "
    "Never retry an ambiguous send. Send only the requested exact nonce, with no extra notification."
)
NOTIFY_OWNER_INSTRUCTION = (
    "Use only the woltspace_notify notify_owner tool to send the exact requested nonce "
    "as message, once. This test requires the preauthorized owner tool, not the notify CLI. "
    "Do not invoke shell/curl/HTTP/legacy notify, request approval, answer dialogs, "
    "or fall back to another route. If the tool is unavailable or requests approval, stop. "
    "Never retry an ambiguous outcome. Send no extra notification."
)
_PATHS = (ROOT, ROOT / "container", ROOT / "container" / "lib", ROOT / "src")
_DIAGNOSTIC_STAGES = (
    "preflight", "server_health", "inbound_selection", "selection_reply",
    "challenge_dispatch", "session_spawn", "harness_boot", "session_ack",
    "notify", "cleanup",
)
_DIAGNOSTIC_STATUSES = {"pending", "passed", "failed", "not_reached"}
_NOTIFY_ATTEMPT_STATES = {
    "not_seen", "dispatch_wait", "reserved", "route_verified",
    "send_in_flight", "sent", "rejected", "ambiguous",
}
_NOTIFY_REJECTIONS = {"none", "dispatch_timeout", "payload_mismatch", "route_unverified"}
_NOTIFY_PAYLOAD_SHAPES = {"not_seen", "exact", "exact_plus_terminal_lf", "other"}
_PROCESS_STATES = {"not_checked", "present", "absent", "unknown"}
_READINESS_STATES = {
    "not_checked", "ready_marker_seen", "prompt_detected", "not_ready",
    "unknown", "unsupported",
}
_FAILURE_CATEGORIES = {
    "preflight_failed", "executable_missing", "version_probe_failed",
    "help_probe_failed", "bootstrap_missing", "server_start_failed",
    "selection_not_confirmed", "adapter_reply_failed", "session_not_started", "auth_or_config",
    "harness_boot_unconfirmed", "notify_rejected", "notify_ambiguous",
    "trust_or_approval_prompt", "waiting_for_input", "run_timeout",
    "directory_trust_prompt", "onboarding_prompt", "tool_approval_prompt",
    "local_transport_probe_failed",
    "approval_not_completed",
    "cleanup_failed", "internal_error",
}

_CONTROL_EXCEPTIONS = {"none", "timeout", "connection", "terminal_configuration", "rate_limited", "other"}
_CLEANUP_COMPONENTS = {"api_server", "listener", "session", "tmux", "finalize"}
_CLEANUP_STATES = {"not_checked", "not_needed", "passed", "failed", "retained"}


def _empty_control_reply():
    return {"operation": "none", "call_attempted": False, "submission": "not_attempted",
            "http_status": None, "provider_code": None, "exception": "none"}


class RunDiagnostics:
    """Fixed-schema status only; never stores exception, prompt, or identity text."""

    def __init__(self, harness: str):
        self._lock = threading.RLock()
        self.harness = harness if harness in {"claude", "codex"} else "unknown"
        self.stage = "preflight"
        self.failure_stage = ""
        self.category = ""
        self.checks = {name: "pending" for name in _DIAGNOSTIC_STAGES}
        self.checks["cleanup"] = "not_reached"
        self.notify_attempt = "not_seen"
        self.notify_rejection = "none"
        self.notify_payload_shape = "not_seen"
        self.harness_process = "not_checked"
        self.harness_readiness = "not_checked"
        self.boot_symptom = "not_checked"
        self.control_reply = _empty_control_reply()
        self.cleanup_components = dict.fromkeys(_CLEANUP_COMPONENTS, "not_checked")

    def enter(self, stage: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES:
                self.stage = stage

    def passed(self, stage: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES:
                self.checks[stage] = "passed"

    def set_check(self, stage: str, state: str) -> None:
        with self._lock:
            if stage in _DIAGNOSTIC_STAGES and state in _DIAGNOSTIC_STATUSES:
                self.checks[stage] = state

    def set_notify_attempt(self, state: str) -> None:
        with self._lock:
            if state in _NOTIFY_ATTEMPT_STATES:
                self.notify_attempt = state

    def set_notify_details(self, *, rejection=None, payload_shape=None) -> None:
        with self._lock:
            if isinstance(rejection, str) and rejection in _NOTIFY_REJECTIONS:
                self.notify_rejection = rejection
            if isinstance(payload_shape, str) and payload_shape in _NOTIFY_PAYLOAD_SHAPES:
                self.notify_payload_shape = payload_shape

    def set_harness_process(self, state: str) -> None:
        with self._lock:
            if state in _PROCESS_STATES:
                self.harness_process = state

    def set_harness_readiness(self, state: str) -> None:
        with self._lock:
            if state in _READINESS_STATES:
                self.harness_readiness = state

    def set_boot_symptom(self, state: str) -> None:
        with self._lock:
            if state in _BOOT_SYMPTOMS:
                self.boot_symptom = state

    def fail(self, category: str | None = None) -> None:
        with self._lock:
            if not self.failure_stage:
                self.failure_stage = self.stage
            if self.stage in _DIAGNOSTIC_STAGES:
                self.checks[self.stage] = "failed"
            if category in _FAILURE_CATEGORIES and not self.category:
                self.category = category
            elif not self.category:
                self.category = {
                    "preflight": "preflight_failed",
                    "server_health": "server_start_failed",
                    "inbound_selection": "selection_not_confirmed",
                    "selection_reply": "adapter_reply_failed",
                    "challenge_dispatch": "session_not_started",
                    "session_spawn": "session_not_started",
                    "harness_boot": "harness_boot_unconfirmed",
                    "session_ack": "internal_error",
                    "notify": "notify_rejected",
                    "cleanup": "cleanup_failed",
                }.get(self.stage, "internal_error")

    def set_cleanup(self, component, state):
        with self._lock:
            if component in _CLEANUP_COMPONENTS and state in _CLEANUP_STATES:
                self.cleanup_components[component] = state

    def as_record(self) -> dict[str, Any]:
        with self._lock:
            return {
                "schema": 4,
                "harness": self.harness,
                "stage": self.failure_stage or self.stage,
                "category": self.category or "internal_error",
                "checks": {name: self.checks[name] for name in _DIAGNOSTIC_STAGES},
                "notify_attempt": self.notify_attempt,
                "notify_rejection": self.notify_rejection,
                "notify_payload_shape": self.notify_payload_shape,
                "harness_process": self.harness_process,
                "harness_readiness": self.harness_readiness,
                "boot_symptom": self.boot_symptom,
                "control_reply": dict(self.control_reply),
                "cleanup_components": dict(self.cleanup_components),
            }


_BOOT_SYMPTOMS = {
    "auth_or_config", "bootstrap_missing", "trust_or_approval",
    "directory_trust_prompt", "onboarding_prompt", "tool_approval_prompt",
    "waiting_for_input", "none_detected", "capture_unavailable",
}
_BOOT_PATTERNS = (
    ("auth_or_config", re.compile(
        r"not logged in|login required|run codex login|authentication required|"
        r"unauthorized|invalid config|failed to load config|api key required", re.I,
    )),
    ("bootstrap_missing", re.compile(
        r"command not found|no such file or directory|notify: not found|"
        r"skill.{0,40}(not found|unavailable)|unknown skill", re.I,
    )),
    ("trust_or_approval", re.compile(
        r"trust(ed)? .{0,60}(folder|workspace|project)|"
        r"(approve|approval).{0,60}(workspace|permission|request|command)|"
        r"permission.{0,60}(approval|allow|request)|onboard(ing)?|allow .{0,40}\?", re.I,
    )),
    ("waiting_for_input", re.compile(
        r"press (enter|return)|waiting for (input|your)|type .* to continue|"
        r"continue\?\s*$|waiting for approval", re.I | re.M,
    )),
)


def _classify_boot_symptom(text: str) -> str:
    """Map private pane text to one fixed category; never return source text."""
    # Require both a specific question/header and concrete interactive choices.
    # A mere occurrence of "trust"/"approval" is only an ambiguous symptom.
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    choices = bool(re.search(
        r"(?im)^\s*(?:[>❯›]\s*)?\d+[.)]\s*(yes|no|proceed|exit|dark|light)\b|"
        r"(?im:press (enter|return) to (confirm|continue))", text,
    ))
    directory = choices and bool(re.search(
        r"do you trust (the contents of )?(this|the) (directory|folder|workspace|project)\??",
        text, re.I,
    ))
    tool = choices and bool(re.search(
        r"would you like to run the following command\??|approve (this|the following) command\??",
        text, re.I,
    ))
    # Codex's MCP picker uses Allow/Cancel rather than shell Yes/No choices.
    # Require its concrete question AND both numbered action choices.
    mcp_tool = bool(re.search(
        r'(?im)^\s*Allow the [A-Za-z0-9_.-]+ MCP server to run tool "[^"\n]{1,128}"\?', text,
    )) and all(re.search(pattern, text) for pattern in (
        r"(?im)^\s*(?:[>❯›]\s*)?\d+[.)]\s*Allow\b",
        r"(?im)^\s*(?:[>❯›]\s*)?\d+[.)]\s*Cancel\b",
    ))
    tool = tool or mcp_tool
    onboarding = choices and bool(re.search(
        r"welcome to codex[\s\S]{0,300}(choose|select).{0,50}(theme|appearance)|"
        r"(choose|select).{0,50}(theme|appearance)[\s\S]{0,300}welcome to codex",
        text, re.I,
    ))
    specific = [name for name, match in (
        ("directory_trust_prompt", directory), ("tool_approval_prompt", tool),
        ("onboarding_prompt", onboarding),
    ) if match]
    if len(specific) == 1:
        return specific[0]
    if len(specific) > 1:
        return "trust_or_approval"
    for category, pattern in _BOOT_PATTERNS:
        if pattern.search(text):
            return category
    return "none_detected"


def _write_failure_record(run_root: Path, diagnostics: RunDiagnostics) -> Path:
    """Atomically store only the allowlisted status schema with mode 0600."""
    path = run_root / "failure.json"
    temp = run_root / ".failure.json.tmp"
    record = diagnostics.as_record()
    if not _valid_diagnostic_record(record):
        raise ValueError("diagnostic record failed its fixed-schema check")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _valid_diagnostic_record(record: Any) -> bool:
    """Validate the bounded versioned schema before it reaches disk."""
    def enum(value: Any, choices) -> bool:
        return isinstance(value, str) and value in choices

    if (not isinstance(record, dict) or type(record.get("schema")) is not int
            or record["schema"] not in {2, 3, 4}):
        return False
    keys = {"schema", "harness", "stage", "category", "checks", "notify_attempt",
            "harness_process", "harness_readiness", "boot_symptom"}
    if record["schema"] >= 3:
        keys |= {"notify_rejection", "notify_payload_shape"}
        if (not enum(record.get("notify_rejection"), _NOTIFY_REJECTIONS)
                or not enum(record.get("notify_payload_shape"), _NOTIFY_PAYLOAD_SHAPES)):
            return False
    if record["schema"] == 4:
        keys |= {"control_reply", "cleanup_components"}
        control = record.get("control_reply")
        cleanup = record.get("cleanup_components")
        if (not isinstance(control, dict) or set(control) != set(_empty_control_reply())
                or not enum(control.get("operation"), {"none", "selection", "session_ack"})
                or type(control.get("call_attempted")) is not bool
                or not enum(control.get("submission"), {"not_attempted", "unknown", "http_response_received"})
                or not (control.get("http_status") is None or type(control.get("http_status")) is int and 100 <= control["http_status"] <= 599)
                or not (control.get("provider_code") is None or type(control.get("provider_code")) is int and control["provider_code"] == 100)
                or not enum(control.get("exception"), _CONTROL_EXCEPTIONS)
                or not isinstance(cleanup, dict) or set(cleanup) != _CLEANUP_COMPONENTS
                or not all(enum(value, _CLEANUP_STATES) for value in cleanup.values())):
            return False
        if not control["call_attempted"]:
            if control != _empty_control_reply():
                return False
        elif (control["operation"] == "none" or control["submission"] == "not_attempted"
              or (control["submission"] == "http_response_received") != (control["http_status"] is not None)
              or control["provider_code"] is not None and control["http_status"] is None):
            return False
    return bool(
        set(record) == keys
        and enum(record.get("harness"), {"claude", "codex", "unknown"})
        and enum(record.get("stage"), _DIAGNOSTIC_STAGES)
        and enum(record.get("category"), _FAILURE_CATEGORIES)
        and isinstance(record.get("checks"), dict)
        and set(record["checks"]) == set(_DIAGNOSTIC_STAGES)
        and all(enum(value, _DIAGNOSTIC_STATUSES) for value in record["checks"].values())
        and enum(record.get("notify_attempt"), _NOTIFY_ATTEMPT_STATES)
        and enum(record.get("harness_process"), _PROCESS_STATES)
        and enum(record.get("harness_readiness"), _READINESS_STATES)
        and enum(record.get("boot_symptom"), _BOOT_SYMPTOMS | {"not_checked"})
    )


def _remove_raw_log(run_root: Path) -> None:
    for path in run_root.rglob("*.log"):
        try:
            if not path.is_symlink() and path.is_file():
                path.unlink()
        except OSError:
            pass


def _diagnostic_line(diagnostics: RunDiagnostics) -> str:
    stage = diagnostics.failure_stage or diagnostics.stage
    category = diagnostics.category if diagnostics.category in _FAILURE_CATEGORIES else "internal_error"
    line = f"Run stopped: stage={stage} category={category}."
    if diagnostics.notify_attempt != "not_seen":
        rejection = diagnostics.notify_rejection
        shape = diagnostics.notify_payload_shape
        rejection = rejection if isinstance(rejection, str) and rejection in _NOTIFY_REJECTIONS else "none"
        shape = shape if isinstance(shape, str) and shape in _NOTIFY_PAYLOAD_SHAPES else "other"
        line += f" notify_rejection={rejection} notify_payload_shape={shape}."
    record = diagnostics.as_record()
    if _valid_diagnostic_record(record):
        control = record["control_reply"]
        if control["call_attempted"]:
            line += (f" control_operation={control['operation']} call_attempted=true"
                     f" submission={control['submission']} http_status={control['http_status']}"
                     f" provider_code={control['provider_code']} exception={control['exception']}.")
        failed = sorted(name for name, state in record["cleanup_components"].items() if state == "failed")
        if failed:
            line += " cleanup_failed_components=" + ",".join(failed) + "."
    return line


def _track_adapter_replies(real_api, diagnostics: RunDiagnostics):
    """Retain fixed metadata only, never provider bodies or exception text."""
    def tracked(method: str, path: str, payload: dict | None = None):
        body = ((payload or {}).get("text") or {}).get("body", "")
        if method == "POST" and path == "/messages" and body.startswith("Selected wa-test."):
            stage = "selection_reply"
        elif method == "POST" and path == "/messages" and body.startswith("Session started:"):
            stage = "session_ack"
        else:
            return real_api(method, path, payload)
        diagnostics.enter(stage)
        # Call admission is NOT evidence that network/provider submission occurred.
        diagnostics.control_reply = dict(_empty_control_reply(),
            operation="selection" if stage == "selection_reply" else "session_ack",
            call_attempted=True, submission="unknown")
        def response_metadata(status, code=None):
            if type(status) is int and 100 <= status <= 599:
                diagnostics.control_reply["http_status"] = status
                diagnostics.control_reply["submission"] = "http_response_received"
            if type(code) is int and code == 100:
                diagnostics.control_reply["provider_code"] = code
        try:
            result = real_api(method, path, payload)
        except Exception as exc:
            module = sys.modules.get(getattr(real_api, "__module__", ""))
            api_error = getattr(module, "WhatsAppAPIError", ())
            if isinstance(exc, api_error):
                response_metadata(exc.http_status, exc.provider_code)
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(reason, TimeoutError):
                category = "timeout"
            elif isinstance(reason, ConnectionError) or isinstance(reason, OSError) and reason.errno in {
                    errno.ECONNREFUSED, errno.ECONNRESET, errno.ENETUNREACH, errno.EHOSTUNREACH}:
                category = "connection"
            elif isinstance(exc, getattr(module, "WhatsAppConfigurationError", ())):
                category = "terminal_configuration"
            elif isinstance(exc, getattr(module, "WhatsAppRateLimit", ())):
                category = "rate_limited"
            else:
                category = "other"
            diagnostics.control_reply["exception"] = category
            diagnostics.set_check(stage, "failed")
            if stage == "selection_reply":
                diagnostics.fail("adapter_reply_failed")
            raise
        if isinstance(result, tuple) and result:
            response_metadata(result[0])
            # Only numeric allowlisted code; never retain any response body.
            if len(result) > 1 and isinstance(result[1], dict):
                error = result[1].get("error")
                if isinstance(error, dict):
                    response_metadata(result[0], error.get("code"))
        if isinstance(result, tuple) and result and type(result[0]) is int and 200 <= result[0] < 300:
            diagnostics.passed(stage)
        else:
            diagnostics.set_check(stage, "failed")
            if stage == "selection_reply":
                diagnostics.fail("adapter_reply_failed")
        return result
    return tracked


def _capture_boot_symptom(env: dict[str, str], session: str, *, visible_only=False) -> str:
    """Classify the private tmux pane without returning or persisting its text."""
    try:
        args = ("capture-pane", "-p", "-t", session)
        if not visible_only:
            args += ("-S", "-80")
        status, text, _ = _tmux_value(env, *args)
    except Exception:
        return "capture_unavailable"
    if status:
        return "capture_unavailable"
    return _classify_boot_symptom(text)


def _handle_boot_symptom(diagnostics: RunDiagnostics, symptom: str) -> bool:
    """Record an allowlisted boot symptom; return true for actionable prompts."""
    diagnostics.set_boot_symptom(symptom)
    if symptom in {"auth_or_config", "bootstrap_missing"}:
        diagnostics.set_harness_readiness("prompt_detected")
        diagnostics.enter("harness_boot")
        diagnostics.fail(symptom)
        return True
    if symptom in {"trust_or_approval", "waiting_for_input", "directory_trust_prompt",
                   "onboarding_prompt", "tool_approval_prompt"}:
        diagnostics.set_harness_readiness("prompt_detected")
        diagnostics.enter("harness_boot")
        diagnostics.fail(
            "trust_or_approval_prompt" if symptom == "trust_or_approval" else symptom,
        )
        return True
    return False


def _observe_notify_approval(diagnostics: RunDiagnostics, symptom: str) -> bool:
    """Keep pending approval observable; echoed instructions are not UI proof."""
    if symptom not in {"tool_approval_prompt", "trust_or_approval", "waiting_for_input"}:
        return False
    diagnostics.set_boot_symptom(symptom)
    diagnostics.set_harness_readiness(
        "prompt_detected" if symptom == "tool_approval_prompt" else "unknown",
    )
    return True


def _print_approval_notice(target, tmux_root, seconds, *, command="notify"):
    if (not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", target)
            or command not in {"notify", "startup-notify-probe"}):
        raise RuntimeError("invalid private test-session attach target")
    socket = tmux_root / f"tmux-{os.getuid()}" / "default"
    attach = shlex.join(["env", "-u", "TMUX", "tmux", "-S", str(socket),
                         "attach-session", "-t", "=" + target])
    _say("Approval needed: the synthetic task requests normal one-time command escalation; "
         "prompt visibility is not yet confirmed. No approval will be answered automatically.")
    _say("Attach only to this temporary test session: " + attach)
    _say(f"Approve only the intended {command} command if shown; do not grant a permanent rule. "
         f"Observation budget: {seconds}s. Detach with Ctrl-b then d; "
         "Ctrl-C in the runner stops and cleans up.")


def _harness_readiness_status(session: str, harness: str) -> str:
    """Report configured TUI readiness evidence, never process presence as readiness."""
    try:
        from harnesses import get_harness
        marker = get_harness(harness).get("tui_ready_marker")
    except Exception:
        return "unknown"
    # Codex currently has no documented, versioned ready marker. Do not infer
    # model readiness from a live process or a raw terminal.
    if not marker:
        return "unsupported"
    try:
        from sessions import SessionRegistry
        record = SessionRegistry().get(session, check_alive=False)
        if not record:
            return "unknown"
        pane = _capture_boot_symptom_text(record)
    except Exception:
        return "unknown"
    if pane is None:
        return "unknown"
    return "ready_marker_seen" if marker in pane else "not_ready"


def _capture_boot_symptom_text(record: dict) -> str | None:
    """Capture a pane transiently for readiness matching; caller never persists text."""
    try:
        from session_runtime import get_runtime
        return get_runtime().capture(record, start="-80")
    except Exception:
        return None


def _harness_process_status(session: str, harness: str) -> bool | None:
    """Probe the actual harness, excluding launching shims; never return argv."""
    try:
        from sessions import session_has_agent_process
        return session_has_agent_process(session, harness=harness, include_launching=False)
    except Exception:
        return None


def _say(message: str) -> None:
    """Write only fixed, credential-free progress messages to the terminal."""
    os.write(1, (message + "\n").encode("utf-8", errors="replace"))


def _auth_path(harness: str, inherited: dict[str, str]) -> tuple[str, Path]:
    """Select an existing supported login path without opening its contents."""
    variable, default_dir = {
        "claude": ("CLAUDE_CONFIG_DIR", ".claude"),
        "codex": ("CODEX_HOME", ".codex"),
    }[harness]
    configured = inherited.get(variable, "").strip()
    home = Path(inherited.get("HOME") or Path.home())
    selected = Path(configured).expanduser() if configured else home / default_dir
    if not selected.is_dir():
        raise RuntimeError("the selected harness has no existing authorized login directory")
    return variable, selected.resolve()


def _prepare_private_tmux_dir(run_root: Path, base: Path | None = None) -> Path:
    """Create a short, owned per-run socket dir and validate Linux sun_path."""
    base = base or PRIVATE_TMUX_BASE
    uid = os.getuid()
    for directory in (base,):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != uid):
            raise RuntimeError("private tmux directory ownership check failed")
        os.chmod(directory, 0o700)
    tmux_tmp = base / run_root.name
    try:
        tmux_tmp.mkdir(mode=0o700)
    except FileExistsError:
        raise RuntimeError("private tmux run directory already exists") from None
    info = tmux_tmp.lstat()
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != uid or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("private tmux run directory ownership check failed")
    (tmux_tmp / f"tmux-{uid}").mkdir(mode=0o700)
    socket_path = tmux_tmp / f"tmux-{uid}" / "default"
    # Linux sockaddr_un.sun_path reserves 108 bytes including the NUL.
    if len(os.fsencode(socket_path)) >= 108:
        shutil.rmtree(tmux_tmp)
        raise RuntimeError("private tmux socket path exceeds Linux pathname limit")
    return tmux_tmp


def _private_environment(
    run_root: Path, wolts_dir: Path, harness: str, auth_variable: str,
    auth_path: Path, host: str, port: int, inherited: dict[str, str],
    tmux_tmp: Path,
) -> dict[str, str]:
    """Build a narrow process env; never copy owner API tokens into it."""
    home = run_root / "home"
    xdg = run_root / "xdg"
    temp = run_root / "tmp"
    for directory in (home, xdg, xdg / "config", xdg / "data", xdg / "state",
                      xdg / "cache", tmux_tmp, temp):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)

    tmux = shutil.which("tmux", path=inherited.get("PATH"))
    if not tmux:
        raise RuntimeError("tmux is unavailable on PATH")
    env = {
        "PATH": inherited.get("PATH", os.defpath),
        "HOME": str(home),
        "TMPDIR": str(temp),
        "TMUX_TMPDIR": str(tmux_tmp),
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "WOLTSPACE_DIR": str(ROOT),
        "WOLTSPACE_WOLTS_DIR": str(wolts_dir),
        "WOLTS_DIR": str(wolts_dir),
        "WOLTSPACE_WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLT_DIR": str(wolts_dir / TEST_WOLT),
        "WOLTSPACE_WOLT_NAME": TEST_WOLT,
        "WOLT_NAME": TEST_WOLT,
        "WOLTSPACE_ISOLATION": "host",
        "WOLTSPACE_HOST": host,
        "WOLTSPACE_PORT": str(port),
        "PORT": str(port),
        "WOLTSPACE_API": f"http://{host}:{port}",
        "WOLTSPACE_PYTHON": sys.executable,
        "WOLTSPACE_TMUX_BIN": tmux,
        "WOLTSPACE_DEFAULT_HARNESS": harness,
        "BOT_ADAPTER": "whatsapp",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("LANG", "LC_ALL"):
        if inherited.get(key):
            env[key] = inherited[key]
    if auth_variable == "CODEX_HOME":
        # Keep Codex rollouts and config writes inside this run. The preflight
        # adds only an opaque symlink to the owner's existing auth.json.
        env[auth_variable] = str(run_root / "codex-home")
    else:
        env[auth_variable] = str(auth_path)
    # Never pass terminal multiplexing state from the owner's current shell.
    for key in ("TMUX", "TMUX_PANE", "WHATSAPP_AGENT_TOKEN"):
        env.pop(key, None)
    return env


def _preflight_harness(
    harness: str, env: dict[str, str], auth_path: Path, wolt_dir: Path,
    diagnostics: RunDiagnostics,
) -> None:
    """Check offline CLI, launch wrapper, notify path and seeded skills."""
    diagnostics.enter("preflight")
    if not auth_path.is_dir() or not os.access(auth_path, os.R_OK | os.X_OK):
        diagnostics.fail("preflight_failed")
        raise RuntimeError("selected harness login directory is unavailable")
    if (harness == "codex" and (
            Path(env.get("CODEX_HOME", "")).resolve() == auth_path.resolve()
            or env.get("WOLTSPACE_ISOLATION") != "host")):
        diagnostics.fail("preflight_failed")
        raise RuntimeError("isolated Codex and existing login paths are not separated")
    if harness == "codex":
        source_auth = auth_path / "auth.json"
        private_codex_home = Path(env["CODEX_HOME"])
        try:
            source_auth_stat = source_auth.stat()
        except OSError:
            diagnostics.fail("auth_or_config")
            raise RuntimeError("Codex auth file is unavailable") from None
        if not stat.S_ISREG(source_auth_stat.st_mode) or not os.access(source_auth, os.R_OK):
            diagnostics.fail("auth_or_config")
            raise RuntimeError("Codex auth file is unavailable")
        if not private_codex_home.resolve().is_relative_to(
                (wolt_dir.parent.parent).resolve()):
            diagnostics.fail("preflight_failed")
            raise RuntimeError("private Codex state is outside the test root")
        private_codex_home.mkdir(mode=0o700)
        os.chmod(private_codex_home, 0o700)
        (private_codex_home / "auth.json").symlink_to(source_auth.resolve())

    old_path = list(sys.path)
    sys.path[:0] = [str(p) for p in _PATHS]
    try:
        from harnesses import get_harness
        from skills_sync import seed_wolt_skills

        entry = get_harness(harness)
        wrapper = Path(entry["wrapper"])
        if not wrapper.is_file() or not os.access(wrapper, os.X_OK):
            diagnostics.fail("executable_missing")
            raise RuntimeError("selected harness wrapper is unavailable")

        bin_dir = ROOT / "container" / "bin"
        required = (bin_dir / "run-session.sh", bin_dir / "session-reg", bin_dir / "notify")
        if (any(not p.is_file() or not os.access(p, os.X_OK) for p in required)
                or not shutil.which("bash", path=env.get("PATH"))):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("session bootstrap executable is unavailable")
        session_path = os.pathsep.join((str(bin_dir), env.get("PATH", os.defpath)))
        notify_path = shutil.which("notify", path=session_path)
        if notify_path != str(bin_dir / "notify"):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("notify is not resolvable from the session path")

        # Match start_session's platform-skill bootstrap against this private
        # synthetic wolt. It copies only checkout skills into the test root.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            seed_wolt_skills(ROOT, wolt_dir)
        skill_dir = wolt_dir / ".agents" / "skills"
        if not ((skill_dir / "woltspace-start-chat" / "SKILL.md").is_file()
                and (skill_dir / "woltspace-notify" / "SKILL.md").is_file()):
            diagnostics.fail("bootstrap_missing")
            raise RuntimeError("Codex platform skills were not prepared for the test wolt")
        for installed, source in (
            (skill_dir / "woltspace-start-chat/modes/whatsapp.md",
             ROOT / "container/skills/start-chat/modes/whatsapp.md"),
            (skill_dir / "woltspace-notify/SKILL.md", ROOT / "container/skills/notify/SKILL.md"),
        ):
            # Copy delivery intentionally rewrites SKILL.md's frontmatter
            # name to woltspace-notify; the instructional body must match.
            if (not installed.is_file() or
                    installed.read_text().split("---", 2)[-1] != source.read_text().split("---", 2)[-1]):
                diagnostics.fail("bootstrap_missing")
                raise RuntimeError("test skills do not match reviewed WhatsApp/notify guidance")

        binary = shutil.which(harness, path=env.get("PATH"))
        if not binary:
            diagnostics.fail("executable_missing")
            raise RuntimeError("selected harness executable is unavailable")
        # The preflight home is disposable and CODEX_HOME is deliberately
        # omitted: --version/--help must not inspect the owner's auth directory.
        safe_env = {
            key: env[key] for key in (
                "PATH", "HOME", "TMPDIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_STATE_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL",
            ) if env.get(key)
        }
        safe_env.pop("WHATSAPP_AGENT_TOKEN", None)
        for args, category in ((("--version",), "version_probe_failed"),
                               (("--help",), "help_probe_failed")):
            try:
                result = subprocess.run(
                    [binary, *args], env=safe_env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=15, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                diagnostics.fail(category)
                raise RuntimeError("selected harness offline preflight failed") from None
            if result.returncode:
                diagnostics.fail(category)
                raise RuntimeError("selected harness offline preflight failed")
        diagnostics.passed("preflight")
    except RuntimeError:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    except Exception:
        diagnostics.fail("bootstrap_missing")
        raise RuntimeError("isolated harness bootstrap preflight failed") from None
    finally:
        sys.path[:] = old_path


def _create_synthetic_wolt(wolts_dir: Path, harness: str) -> Path:
    wolt = wolts_dir / TEST_WOLT
    memory = wolt / "wolt" / "memory"
    memory.mkdir(mode=0o700, parents=True)
    (wolt / ".state").mkdir(mode=0o700)
    (memory / "identity.md").write_text(
        "# wa-test\n\nI am a temporary WhatsApp integration test wolt.\n",
        encoding="utf-8",
    )
    (memory / "context.md").write_text("# Context\n\nTemporary test only.\n", encoding="utf-8")
    (memory / "learnings.md").write_text("# Learnings\n\nNone.\n", encoding="utf-8")
    (wolt / "wolt" / "wolt.json").write_text(json.dumps({
        "name": TEST_WOLT,
        "type": "otter",
        "harness": harness,
        "description": "Temporary, isolated WhatsApp model-session test.",
    }, indent=2) + "\n", encoding="utf-8")
    (wolts_dir / "woltspace.json").write_text(json.dumps({
        "harness": {"default": harness},
        "channels": {
            "whatsapp": {"enabled": False},
            "telegram": {"enabled": False},
            "slack": {"enabled": False},
        },
    }, indent=2) + "\n", encoding="utf-8")
    for path in (wolt / "wolt" / "wolt.json", memory / "identity.md",
                 memory / "context.md", memory / "learnings.md",
                 wolts_dir / "woltspace.json"):
        os.chmod(path, 0o600)
    return wolt


def _bind_local_socket() -> tuple[socket.socket, int]:
    """Bind now and hand the bound socket directly to uvicorn (no port race)."""
    for _ in range(8):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        if port != 7777:
            listener.listen(128)
            listener.set_inheritable(False)
            return listener, port
        listener.close()
    raise RuntimeError("could not reserve an isolated non-7777 loopback port")


def _wait_for_server(
    base_url: str, expected_wolts: Path,
    thread: threading.Thread, server_errors: list[str],
) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if server_errors or not thread.is_alive():
            raise RuntimeError("isolated local server did not start")
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as response:
                health = json.loads(response.read())
            if (health.get("ok") is True
                    and health.get("isolation") == "host"
                    and health.get("pid") == os.getpid()
                    and Path(health.get("wolts_dir", "/")).resolve() == expected_wolts.resolve()):
                return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("isolated local server did not pass its loopback health check")


def _tmux_value(env: dict[str, str], *args: str) -> tuple[int, str, str]:
    result = subprocess.run(
        [env["WOLTSPACE_TMUX_BIN"], *args], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=5, check=False,
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _verify_private_tmux(env: dict[str, str], tmux_root: Path, session: str) -> None:
    status, socket_path, _ = _tmux_value(env, "display-message", "-p", "#{socket_path}")
    if status or not socket_path:
        raise RuntimeError("could not verify the test tmux socket")
    try:
        Path(socket_path).resolve().relative_to(tmux_root.resolve())
    except ValueError:
        raise RuntimeError("tmux socket is outside the private socket directory") from None
    status, value, _ = _tmux_value(env, "show-environment", "-g", "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the private tmux server environment")
    status, value, _ = _tmux_value(env, "show-environment", "-t", session,
                                  "WHATSAPP_AGENT_TOKEN")
    if status == 0 and value and not _is_unset_or_empty_token(value):
        raise RuntimeError("WhatsApp credential appeared in the model session environment")


def _is_unset_or_empty_token(value: str) -> bool:
    return value.startswith("-WHATSAPP_AGENT_TOKEN") or value == "WHATSAPP_AGENT_TOKEN="


def _remove_stale_private_socket(tmux_root: Path) -> bool:
    """Remove only an owned socket node beneath this private per-run dir."""
    try:
        if tmux_root.is_symlink() or tmux_root.stat().st_uid != os.getuid():
            return False
        path = tmux_root / f"tmux-{os.getuid()}" / "default"
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            return False
        path.unlink()
        return not path.exists()
    except FileNotFoundError:
        return True
    except OSError:
        return False


def _validated_private_socket(env: dict[str, str], tmux_root: Path) -> Path | None:
    """Validate exact private owned directories; never follow socket symlinks."""
    try:
        base = PRIVATE_TMUX_BASE
        if (tmux_root.parent.resolve() != base.resolve()
                or not re.fullmatch(r"(?:model|startup)-[A-Za-z0-9_-]{1,40}", tmux_root.name)
                or env.get("TMUX_TMPDIR") != str(tmux_root)):
            return None
        parent = tmux_root / f"tmux-{os.getuid()}"
        for directory in (base, tmux_root, parent):
            info = directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                return None
        socket = parent / "default"
        try:
            info = socket.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                return None
        except FileNotFoundError:
            pass
        return socket
    except (OSError, ValueError):
        return None


def _socket_absent(socket: Path) -> bool:
    try:
        socket.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def _kill_private_tmux(env: dict[str, str], tmux_root: Path, *, never_started=False) -> bool:
    socket = _validated_private_socket(env, tmux_root)
    if socket is None:
        return False
    if _socket_absent(socket):
        # Absence cannot prove a previously started server exited: its socket
        # could have been unlinked while it remains alive.
        return never_started is True
    try:
        status, socket_path, error = _tmux_value(
            env, "display-message", "-p", "#{socket_path}",
        )
    except Exception:
        return False
    if status:
        # No positive stop evidence for a previously started/unknown server.
        return False
    if Path(socket_path) != socket:
        return False
    try:
        status, _, _ = _tmux_value(env, "kill-server")
    except Exception:
        return False
    if status:
        return False
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        # Absence after a successful exact-server kill is positive stop evidence.
        if _validated_private_socket(env, tmux_root) == socket and _socket_absent(socket):
            return True
        try:
            check_status, _, check_error = _tmux_value(
                env, "display-message", "-p", "#{socket_path}",
            )
        except Exception:
            return False
        if check_status and "no server running" in check_error.lower():
            return _remove_stale_private_socket(tmux_root)
        time.sleep(0.05)
    return False


class OneNonceSendGate:
    """Allow one precisely formatted notify send after authoritative checks."""

    def __init__(self, expected_payload: str, verify, dispatch_done: threading.Event,
                 wait_seconds: float = DISPATCH_WAIT_SECONDS,
                 diagnostics: RunDiagnostics | None = None):
        self.expected_payload = expected_payload
        self.verify = verify
        self.dispatch_done = dispatch_done
        self.wait_seconds = wait_seconds
        self._lock = threading.Lock()
        self.status = "ready"
        self.verified_session = ""
        self.failure = ""
        self.diagnostics = diagnostics

    def _set_status(self, state: str) -> None:
        self.status = state
        if self.diagnostics is not None:
            self.diagnostics.set_notify_attempt(state)

    async def send(self, session: str, participant: str, message: str,
                   routing: dict | None, sender):
        # A model can notify while adapter.handle is still returning from
        # start_session and before it atomically saves adapter.ROUTES. Wait on
        # a thread event without blocking this server's asyncio loop or holding
        # the gate/phase locks. The dispatch thread always sets it in finally.
        with self._lock:
            if self.status != "ready":
                raise RuntimeError("the single WhatsApp test send is already reserved")
            self._set_status("dispatch_wait")
            # Only the single LF separator from literal heredoc input is an
            # accepted transport variation. Never strip arbitrary whitespace.
            shape = ("exact" if message == self.expected_payload else
                     "exact_plus_terminal_lf" if message == self.expected_payload + "\n" else "other")
            if self.diagnostics is not None:
                self.diagnostics.set_notify_details(payload_shape=shape)
        dispatch_finished = await asyncio.to_thread(
            self.dispatch_done.wait, self.wait_seconds,
        )
        with self._lock:
            if self.status != "dispatch_wait":
                raise RuntimeError("the single WhatsApp test send is already reserved")
            # Reserve after bounded dispatch synchronization but before route
            # validation and definitely before any network I/O.
            self._set_status("reserved")
            if not dispatch_finished:
                if self.diagnostics is not None:
                    self.diagnostics.set_notify_details(rejection="dispatch_timeout")
                self._set_status("rejected")
                self.failure = "the inbound dispatch did not finish in time"
                raise RuntimeError(self.failure)
            if shape == "other":
                if self.diagnostics is not None:
                    self.diagnostics.set_notify_details(rejection="payload_mismatch")
                self._set_status("rejected")
                self.failure = "the notify payload did not match the exact nonce"
                raise RuntimeError(self.failure)
            try:
                verified = self.verify(session, participant, routing)
            except Exception:
                verified = ""
            if not verified:
                if self.diagnostics is not None:
                    self.diagnostics.set_notify_details(rejection="route_unverified")
                self._set_status("rejected")
                self.failure = "the test session route was not verified"
                raise RuntimeError(self.failure)
            self.verified_session = verified
            self._set_status("route_verified")
        self._set_status("send_in_flight")
        try:
            result = await sender()
        except BaseException:
            with self._lock:
                self._set_status("ambiguous")
                self.failure = "the single WhatsApp send had an ambiguous outcome"
            raise
        with self._lock:
            self._set_status("sent")
        return result


def _cleanup_resources(
    server, server_thread, listener, session_name: str, env: dict[str, str],
    tmux_root: Path, diagnostics=None, session_start_requested=None, *,
    stop_session_fn=None, kill_tmux_fn=None,
) -> bool:
    """Attempt all independent steps, retaining only fixed component outcomes."""
    outcomes = dict.fromkeys(("api_server", "listener", "session", "tmux"), "not_needed")
    server_ok = True
    if server is not None:
        outcomes["api_server"] = "passed"
        try:
            server.should_exit = True
        except Exception:
            server_ok = False
    if server_thread is not None:
        outcomes["api_server"] = "passed"
        try:
            server_thread.join(timeout=10)
            if server_thread.is_alive():
                server_ok = False
        except Exception:
            server_ok = False
    if not server_ok:
        outcomes["api_server"] = "failed"
    if listener is not None:
        try:
            listener.close()
            outcomes["listener"] = "passed"
        except Exception:
            outcomes["listener"] = "failed"
    # Stop the exact verified private server BEFORE registry stop_session:
    # that API can stamp stopped while ignoring its runtime.stop boolean.
    try:
        socket = _validated_private_socket(env, tmux_root) if kill_tmux_fn is None else None
        never_created = (session_start_requested is False and socket is not None
                         and _socket_absent(socket))
        stopped = (_kill_private_tmux(env, tmux_root,
                                     never_started=session_start_requested is False)
                   if kill_tmux_fn is None else kill_tmux_fn(env, tmux_root))
        # Preserve which positive evidence justified completion: no creation
        # was requested, or a verified private-server stop was confirmed.
        outcomes["tmux"] = ("not_needed" if never_created else "passed") if stopped else "failed"
    except Exception:
        outcomes["tmux"] = "failed"
    if session_name:
        try:
            if stop_session_fn is None:
                from sessions import stop_session as stop_session_fn
            result = stop_session_fn(session_name)
            outcomes["session"] = ("passed" if isinstance(result, dict)
                                   and result.get("status") == "stopped" else "failed")
        except Exception:
            outcomes["session"] = "failed"
    if diagnostics is not None:
        for component, state in outcomes.items():
            diagnostics.set_cleanup(component, state)
    return "failed" not in outcomes.values()


def _finalize_run_root(run_root: Path, cleanup_ok: bool,
                       tmux_root: Path | None = None,
                       *, preserve_failure: bool = False, preserve_snapshot: bool = False) -> bool:
    """After verified cleanup keep only a sanitized failure record, if any."""
    if cleanup_ok:
        try:
            if tmux_root is not None:
                expected_parent = PRIVATE_TMUX_BASE.resolve()
                if (tmux_root.is_symlink()
                        or tmux_root.resolve().parent != expected_parent
                        or tmux_root.resolve().name != run_root.name):
                    raise RuntimeError("refusing to remove unexpected tmux directory")
                shutil.rmtree(tmux_root)
            if preserve_failure or preserve_snapshot:
                retained = ({"failure.json"} if preserve_failure else set())
                if preserve_snapshot:
                    retained.add("startup-snapshot.json")
                for name in retained:
                    record = run_root / name
                    if (record.is_symlink() or not record.is_file()
                            or record.stat().st_uid != os.getuid()
                            or stat.S_IMODE(record.stat().st_mode) != 0o600
                            or record.stat().st_size > 32768):
                        raise RuntimeError("private diagnostic artifact validation failed")
                for child in run_root.iterdir():
                    if child.name in retained:
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink(missing_ok=True)
                return True
            shutil.rmtree(run_root)
            if not run_root.exists():
                return True
        except Exception:
            pass
    try:
        if run_root.exists():
            (run_root / ".cleanup-required").touch(mode=0o600, exist_ok=True)
    except Exception:
        pass
    return False


def _record_for_test(session: str, wolts_dir: Path) -> dict[str, Any] | None:
    from sessions import SessionRegistry
    return SessionRegistry(wolts_dir).get(session, wolt=TEST_WOLT, check_alive=False)


def _expected_notify_payload(nonce: str) -> str:
    # container/bin/notify prefixes the creature emoji and display name before
    # posting /notify. The synthetic wolt's creature is otter and name is fixed.
    return f"🦦 {TEST_WOLT}: {nonce}"


def _wrap_notify_send(real_send, gate: OneNonceSendGate):
    async def guarded_send(
        adapter_name: str, session: str, message: str, chat_id: str,
        thread_ts: str | None = None, routing: dict | None = None,
        attachments=(), speech=None, session_record: dict | None = None,
    ):
        if adapter_name != "whatsapp":
            return await real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            )
        return await gate.send(
            session, str(chat_id), message, routing,
            lambda: real_send(
                adapter_name, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=session_record,
            ),
        )
    return guarded_send


def _confirmed_notify(gate: OneNonceSendGate, session: str,
                      diagnostics: RunDiagnostics) -> bool:
    if gate.status != "sent" or not session or gate.verified_session != session:
        return False
    diagnostics.enter("notify")
    diagnostics.passed("notify")
    return True


def _run_model_test(harness: str, diagnostics: RunDiagnostics | None = None,
                    *, startup_runner=None, notify_owner_tool=False) -> int:
    diagnostics = diagnostics or RunDiagnostics(harness)
    owner_test._check_private_dir()
    inherited = {key: value for key, value in os.environ.items()
                 if key != "WHATSAPP_AGENT_TOKEN"}
    prefix = "startup-" if startup_runner is not None else "model-"
    run_root = Path(tempfile.mkdtemp(prefix=prefix, dir=owner_test.PRIVATE))
    secret = {"value": ""}
    tmux_root = None
    cleanup_ok = True
    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    try:
        os.chmod(run_root, 0o700)
        diagnostics.enter("preflight")
        auth_variable, auth_path = _auth_path(harness, inherited)
        # Bound and validate the private tmux socket path before key access,
        # adapter initialization, or any API polling.
        tmux_root = _prepare_private_tmux_dir(run_root)
        wolts_dir = run_root / "wolts"
        wolts_dir.mkdir(mode=0o700)
        wolt_dir = _create_synthetic_wolt(wolts_dir, harness)
        preflight_env = _private_environment(
            run_root, wolts_dir, harness, auth_variable, auth_path,
            "127.0.0.1", 1, inherited, tmux_root,
        )
        _preflight_harness(harness, preflight_env, auth_path, wolt_dir, diagnostics)
        # The executable, host authentication path, skills, wrappers, and
        # notify command are checked before the private WhatsApp key is read.
        if startup_runner is not None:
            result = startup_runner(
                harness, inherited, auth_variable, auth_path, run_root, tmux_root,
                diagnostics,
            )
        else:
            secret["value"] = owner_test._read_token()
            result = _run_model_session(
                harness, inherited, auth_variable, auth_path, run_root, secret, tmux_root,
                diagnostics, notify_owner_tool=notify_owner_tool,
            )
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        if run_root.exists():
            _remove_raw_log(run_root)
            # Persist the intended retention/finalization outcome before
            # finalizer removes everything except this fixed failure record.
            diagnostics.set_cleanup("finalize", "retained" if diagnostics.checks["cleanup"] == "failed" else "passed")
            if diagnostics.category:
                try:
                    _write_failure_record(run_root, diagnostics)
                except OSError:
                    cleanup_ok = False
            cleanup_ok = cleanup_ok and diagnostics.checks["cleanup"] != "failed"
            finalized = _finalize_run_root(
                run_root, cleanup_ok, tmux_root,
                preserve_failure=bool(diagnostics.category) and cleanup_ok,
                preserve_snapshot=startup_runner is not None and bool(
                    getattr(diagnostics, "private_snapshot_written", False)),
            )
            if not finalized:
                diagnostics.set_cleanup("finalize", "failed" if cleanup_ok else "retained")
                diagnostics.enter("cleanup")
                diagnostics.fail("cleanup_failed")
                try:
                    _write_failure_record(run_root, diagnostics)
                except OSError:
                    pass
    # Finalization can add a failure after the inner runner returned success.
    # Compute the exit status only after that evidence-preserving step.
    return 2 if diagnostics.category else result



def _run_model_session(
    harness: str, inherited: dict[str, str], auth_variable: str,
    auth_path: Path, run_root: Path, secret: dict[str, str], tmux_root: Path,
    diagnostics: RunDiagnostics, *, notify_owner_tool=False,
) -> int:
    wolts_dir = run_root / "wolts"
    wolt_dir = wolts_dir / TEST_WOLT
    listener, port = _bind_local_socket()
    host = "127.0.0.1"
    env = _private_environment(
        run_root, wolts_dir, harness, auth_variable, auth_path,
        host, port, inherited, tmux_root,
    )
    if notify_owner_tool:
        env["WOLTSPACE_NOTIFY_TOOL"] = "1"
    if "WHATSAPP_AGENT_TOKEN" in env:
        raise RuntimeError("refusing to start with WhatsApp credentials in child environment")

    old_env = os.environ.copy()
    old_env.pop("WHATSAPP_AGENT_TOKEN", None)
    old_path = list(sys.path)
    account = hashlib.sha256(secret["value"].encode()).hexdigest()[:20]
    session_name = ""
    adapter = None
    original_start = None
    server = None
    server_thread = None
    server_errors: list[str] = []
    verified_session = ""
    send_gate = None
    session_start_requested = False
    counter = None
    run_log = run_root / "runner.log"
    fd = os.open(run_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    log_stream = os.fdopen(fd, "w", encoding="utf-8", buffering=1)
    original_stdout, original_stderr = sys.stdout, sys.stderr
    os.environ.clear()
    os.environ.update(env)
    sys.path[:0] = [str(p) for p in _PATHS]
    try:
        with contextlib.redirect_stdout(log_stream), contextlib.redirect_stderr(log_stream):
            import uvicorn
            # Import the real control plane only after its environment points
            # at this private root. Lifespan is off: no tunnel, channel runner,
            # watchers, or lodge lifecycle hooks start in this test server.
            import server.app as server_app
            import server.notify as notify

            notify.dotenv_env = lambda name: {
                "WHATSAPP_AGENT_TOKEN": secret["value"],
                "WOLTSPACE_NOTIFY_TOOL": "1" if notify_owner_tool else "",
            }.get(name, "")
            # Reuse the existing body-blind counter; owner-only mode refuses
            # CLI fallback before the real application/sender is reached.
            from scripts.whatsapp_notify_counter import _NotifyCounter
            counter = _NotifyCounter(server_app.app, owner_only=notify_owner_tool)
            config = uvicorn.Config(
                counter, host=host, port=port, log_level="error",
                access_log=False, log_config=None, lifespan="off",
            )
            server = uvicorn.Server(config)

            def serve() -> None:
                try:
                    asyncio.run(server.serve(sockets=[listener]))
                except Exception as exc:
                    server_errors.append(type(exc).__name__)

            server_thread = threading.Thread(target=serve, name="wa-model-test-api", daemon=True)
            server_thread.start()
            diagnostics.enter("server_health")
            _wait_for_server(
                env["WOLTSPACE_API"], wolts_dir, server_thread, server_errors,
            )
            diagnostics.passed("server_health")

            # Import the real adapter against a placeholder so the actual key
            # never enters os.environ, even transiently.
            os.environ["WHATSAPP_AGENT_TOKEN"] = "model-test-import-placeholder"
            try:
                import bot.whatsapp_adapter as adapter_module
            finally:
                os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
            adapter = adapter_module
            adapter.TOKEN = secret["value"]
            adapter.ACCOUNT = account
            adapter.STATE = run_root / "wolts" / ".space" / "whatsapp"
            adapter.STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
            adapter.CURSOR = adapter.STATE / f"cursor-{account}.json"
            adapter.ROUTES = adapter.STATE / f"routes-{account}.json"
            adapter.LOCK = adapter.STATE / f"poll-{account}.lock"
            adapter.api = _track_adapter_replies(adapter.api, diagnostics)
            original_start = adapter.start_claude_session

            def approval_start(prompt, **kwargs):
                nonlocal session_start_requested
                session_start_requested = True  # Before the call, including throws.
                return original_start(prompt + "\n" + (NOTIFY_OWNER_INSTRUCTION if notify_owner_tool
                                                       else NOTIFY_APPROVAL_INSTRUCTION), **kwargs)

            adapter.start_claude_session = approval_start
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            adapter.atomic_json(adapter.CURSOR, state)
            routes = adapter.load(adapter.ROUTES)

            nonce = "wa-model-check-" + secrets.token_hex(6)
            challenge = f"Reply with exactly {nonce} and do nothing else."
            phase: dict[str, Any] = {"selected": False, "prompted": False,
                                     "participant": "", "unexpected": False}
            phase_lock = threading.RLock()
            dispatch_done = threading.Event()

            def verify_send_route(session: str, participant: str,
                                  routing: dict | None) -> str:
                nonlocal session_name
                with phase_lock:
                    if (not phase["selected"] or not phase["prompted"]
                            or participant != phase["participant"]):
                        return ""
                if not session or not isinstance(routing, dict):
                    return ""
                if (routing.get("adapter") != "whatsapp"
                        or routing.get("chat_id") != participant
                        or routing.get("whatsapp_account") != account
                        or routing.get("wolt") != TEST_WOLT):
                    return ""
                persisted_routes = adapter.load(adapter.ROUTES)
                route = persisted_routes.get(participant, {})
                if route.get("wolt") != TEST_WOLT or route.get("session") != session:
                    return ""
                record = _record_for_test(session, wolts_dir)
                if (not record or record.get("adapter") != "whatsapp"
                        or record.get("whatsapp_account") != account
                        or record.get("chat_id") != participant
                        or record.get("wolt") != TEST_WOLT):
                    return ""
                _verify_private_tmux(env, tmux_root, session)
                session_name = session
                return session

            send_gate = OneNonceSendGate(
                _expected_notify_payload(nonce), verify_send_route, dispatch_done,
                diagnostics=diagnostics,
            )
            notify._send = _wrap_notify_send(notify._send, send_gate)

            def dispatch_test_message(item: dict, current_routes: dict, started_at: int) -> None:
                nonlocal session_start_requested
                text = (item.get("text") or {}).get("body", "")
                participant = item.get("from", "")
                if text.strip() in {f"/wolt {TEST_WOLT}", f"wolt {TEST_WOLT}"}:
                    diagnostics.enter("inbound_selection")
                    with phase_lock:
                        if phase["selected"]:
                            phase["unexpected"] = True
                            return
                        phase["participant"] = participant
                    adapter.handle(item, current_routes, started_at)
                    with phase_lock:
                        if current_routes.get(participant, {}).get("wolt") == TEST_WOLT:
                            phase["selected"] = True
                            diagnostics.passed("inbound_selection")
                        else:
                            phase["unexpected"] = True
                    return
                with phase_lock:
                    accepted_challenge = (
                        phase["selected"] and not phase["prompted"]
                        and participant == phase["participant"]
                        and text.strip() == challenge
                    )
                    if not accepted_challenge:
                        phase["unexpected"] = True
                        return
                    # Mark before dispatch: a thrown/ambiguous start must not
                    # allow another model session from a duplicate phrase.
                    phase["prompted"] = True
                diagnostics.enter("challenge_dispatch")
                session_start_requested = True  # Creation/resume may throw ambiguously.
                try:
                    adapter.handle(item, current_routes, started_at)
                    route = current_routes.get(participant, {})
                    if route.get("session"):
                        diagnostics.passed("challenge_dispatch")
                        diagnostics.enter("session_spawn")
                    else:
                        diagnostics.fail("session_not_started")
                finally:
                    # The notify server may already be waiting for route
                    # persistence. Signal only after handle fully returns.
                    dispatch_done.set()

            _say("Isolated server is healthy on a private 127.0.0.1 port.")
            _say("In WhatsApp, send `/wolt wa-test` and wait for the selection confirmation.")
            _say(f"Then send exactly: `{challenge}`")
            _say("The test sends no automatic retry after an ambiguous outbound timeout.")
            diagnostics.enter("inbound_selection")
            deadline = time.monotonic() + TIMEOUT_SECONDS
            approval_started = False
            next_notice = float("inf")
            delivered = False
            while time.monotonic() < deadline:
                state = adapter.poll_once(
                    state, routes, activation, cursor_path=adapter.CURSOR,
                    dispatch=dispatch_test_message,
                )
                routes = adapter.load(adapter.ROUTES)
                if notify_owner_tool and (counter.legacy_count or counter.owner_count > 1):
                    diagnostics.enter("notify")
                    diagnostics.fail("notify_rejected")
                    raise RuntimeError("owner-tool-only request boundary refused fallback or duplicate")
                if diagnostics.category:
                    raise RuntimeError("an isolated startup or control reply check failed")
                if phase["unexpected"]:
                    raise RuntimeError("an unexpected WhatsApp message arrived; it was not dispatched")
                if send_gate.failure:
                    diagnostics.enter("notify")
                    diagnostics.fail(
                        "notify_ambiguous" if send_gate.status == "ambiguous"
                        else "notify_rejected",
                    )
                    raise RuntimeError("the single notify attempt was rejected or ambiguous")
                # Participant IDs are opaque; discover them only in memory.
                matching = next((route for route in routes.values()
                                 if route.get("wolt") == TEST_WOLT), None)
                if matching and matching.get("session"):
                    session_name = matching["session"]
                    if session_name != verified_session:
                        diagnostics.enter("session_spawn")
                        record = _record_for_test(session_name, wolts_dir)
                        if (not record or record.get("adapter") != "whatsapp"
                                or record.get("wolt") != TEST_WOLT or record.get("harness") != harness
                                or (record.get("execution_policy") or {}).get("mode") != "prompt"
                                or not isinstance(record.get("runtime"), dict)
                                or not record.get("runtime")):
                            diagnostics.fail("session_not_started")
                            raise RuntimeError("the test session route was not persisted")
                        if record.get("whatsapp_account") != account:
                            raise RuntimeError("the test session account binding did not match")
                        if record.get("chat_id") != next(
                            participant for participant, route in routes.items()
                            if route is matching
                        ):
                            raise RuntimeError("the test session participant binding did not match")
                        _verify_private_tmux(env, tmux_root, session_name)
                        verified_session = session_name
                        diagnostics.passed("session_spawn")
                        # Give the owner a full budget only once the exact
                        # private runtime/route is verified for attachment.
                        # This is not a claim of model readiness.
                        if not approval_started:
                            deadline = time.monotonic() + TIMEOUT_SECONDS
                            approval_started = True
                            next_notice = time.monotonic() + 15
                            if notify_owner_tool:
                                _say("Verified private session; waiting for one owner-tool notification. "
                                     "No attachment or approval interaction is required.")
                            else:
                                _print_approval_notice(
                                    record["runtime"].get("tmux_session_name", ""),
                                    tmux_root, TIMEOUT_SECONDS,
                                )
                    if diagnostics.checks["harness_boot"] != "passed":
                        diagnostics.enter("harness_boot")
                        process_status = _harness_process_status(session_name, harness)
                        diagnostics.set_harness_process(
                            "present" if process_status is True else
                            "absent" if process_status is False else "unknown",
                        )
                        symptom = _capture_boot_symptom(env, session_name, visible_only=True)
                        if notify_owner_tool and symptom in {"trust_or_approval", "waiting_for_input"}:
                            # Instruction echoes do not establish an actual prompt.
                            diagnostics.set_boot_symptom(symptom)
                            diagnostics.set_harness_readiness("unknown")
                        elif notify_owner_tool or not _observe_notify_approval(diagnostics, symptom):
                            if _handle_boot_symptom(diagnostics, symptom):
                                raise RuntimeError("the isolated harness has an actionable boot prompt")
                            readiness = _harness_readiness_status(session_name, harness)
                            diagnostics.set_harness_readiness(readiness)
                            if readiness == "ready_marker_seen":
                                diagnostics.passed("harness_boot")
                    else:
                        process_status = _harness_process_status(session_name, harness)
                        diagnostics.set_harness_process(
                            "present" if process_status is True else
                            "absent" if process_status is False else "unknown",
                        )
                if send_gate.verified_session:
                    # The send gate may verify first if the model notifies
                    # before this polling loop observes the persisted route.
                    verified_session = send_gate.verified_session
                if (verified_session == send_gate.verified_session
                        and _confirmed_notify(send_gate, session_name, diagnostics)):
                    if notify_owner_tool and (counter.owner_count != 1 or counter.legacy_count != 0):
                        diagnostics.enter("notify")
                        diagnostics.fail("notify_rejected")
                        raise RuntimeError("owner-tool request counts did not match")
                    delivered = True
                    break
                if server_errors:
                    diagnostics.enter("server_health")
                    diagnostics.fail("server_start_failed")
                    raise RuntimeError("the isolated local server stopped unexpectedly")
                if approval_started and send_gate.status == "ready" and time.monotonic() >= next_notice:
                    _say(("Owner-tool task still pending; no notify attempt observed. " if notify_owner_tool
                          else "Approval-needed task still pending; no notify attempt observed. ") +
                         f"Remaining observation budget: {max(0, int(deadline-time.monotonic()))}s.")
                    next_notice = time.monotonic() + 15
            if not delivered:
                if not phase["selected"]:
                    diagnostics.enter("inbound_selection")
                    diagnostics.fail("selection_not_confirmed")
                elif not phase["prompted"]:
                    diagnostics.enter("challenge_dispatch")
                    diagnostics.fail("session_not_started")
                elif (diagnostics.checks["session_spawn"] == "passed"
                      and diagnostics.harness_process == "absent"):
                    diagnostics.enter("harness_boot")
                    diagnostics.fail("harness_boot_unconfirmed")
                else:
                    diagnostics.enter("notify")
                    if not notify_owner_tool and approval_started and send_gate.status == "ready":
                        _say("Approval-needed task did not complete before the observation deadline; "
                             "no notify attempt observed. This does not prove approval was denied. Cleaning up.")
                        diagnostics.fail("approval_not_completed")
                    else:
                        diagnostics.fail("run_timeout")
                raise TimeoutError("timed out waiting for the model reply")
            _say("Verified one real model session, account-bound registry route, and nonce reply.")
            diagnostics.enter("notify")
            diagnostics.passed("notify")
            return 0
    except BaseException:
        if not diagnostics.category:
            diagnostics.fail()
        raise
    finally:
        diagnostics.enter("cleanup")
        cleanup_ok = _cleanup_resources(
            server, server_thread, listener, session_name, env, tmux_root, diagnostics,
            session_start_requested,
        )
        diagnostics.checks["cleanup"] = "passed" if cleanup_ok else "failed"
        if notify_owner_tool and counter is not None:
            # On successful cleanup the local server is stopped/drained.
            # A cleanup failure remains a failed test, regardless of counts.
            _say(f"Owner-tool requests={counter.owner_count} legacy-notify={counter.legacy_count}.")
            if not diagnostics.category and (counter.owner_count != 1 or counter.legacy_count != 0):
                diagnostics.enter("notify")
                diagnostics.fail("notify_rejected")
        if not cleanup_ok and not diagnostics.category:
            diagnostics.category = "cleanup_failed"
        if adapter is not None:
            adapter.TOKEN = ""
            if original_start is not None:
                adapter.start_claude_session = original_start
        secret["value"] = ""
        os.environ.clear()
        os.environ.update(old_env)
        sys.path[:] = old_path
        sys.stdout, sys.stderr = original_stdout, original_stderr
        log_stream.close()
        _remove_raw_log(run_root)
        if cleanup_ok:
            _say("Model test cleanup=passed.")
        else:
            _say("Cleanup needs attention; the private test directory was retained.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="run the guarded live model-session test")
    parser.add_argument("--harness", choices=("codex",),
                        help="use this already-authorized harness login path")
    parser.add_argument("--notify-owner-tool", action="store_true",
                        help="require owner tool only, with no CLI fallback or approval interaction")
    args = parser.parse_args()
    if not args.run or not args.harness:
        parser.error("both --run and --harness are required")
    diagnostics = RunDiagnostics(args.harness)
    try:
        owner_test._require_tty()
        if input("This starts one real model session in private test state. Type MODEL to continue: ").strip() != "MODEL":
            print("Cancelled; no key was read and no process was started.")
            return 1
        result = (_run_model_test(args.harness, diagnostics, notify_owner_tool=True)
                  if args.notify_owner_tool else _run_model_test(args.harness, diagnostics))
        if result and diagnostics.category:
            _say(_diagnostic_line(diagnostics))
        return result
    except KeyboardInterrupt:
        print("Stopped by owner; cleanup was attempted.")
        return 130
    except Exception:
        if not diagnostics.category:
            diagnostics.fail()
        _say(_diagnostic_line(diagnostics))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
