"""Text-only, launcher-bound stdio notification tool.

Session/binding values route requests under the existing trusted-host API
boundary. They are NOT credentials or protection against same-UID host code.
No provider key, shell execution, arbitrary URL, or model-supplied route here.
This file is also the standalone worker: python -I /trusted/path/to/this.py ...
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import tomllib
from urllib.parse import urlsplit

MAX_MESSAGE_CHARS = 4096
MAX_MESSAGE_BYTES = 16 * 1024
MAX_FRAME_BYTES = 32 * 1024
SERVER_NAME = "woltspace_notify"
TOOL_NAME = "notify_owner"
STATUSES = {"sent", "unavailable", "invalid_message", "stale_binding", "busy", "ambiguous"}
REQUEST_DEADLINE_SECONDS = 90


def valid_message(message: object) -> bool:
    if not isinstance(message, str) or not message.strip() or len(message) > MAX_MESSAGE_CHARS:
        return False
    try:
        return len(message.encode("utf-8")) <= MAX_MESSAGE_BYTES
    except UnicodeError:
        return False


def route_binding(record: dict) -> str:
    """Non-secret digest of the launch generation and exact stored route."""
    fields = ("name", "wolt", "created_at", "adapter", "chat_id", "thread_ts",
              "whatsapp_account", "notify_owner_generation")
    return hashlib.sha256(json.dumps(
        [record.get(key) for key in fields], ensure_ascii=True,
        separators=(",", ":"),
    ).encode()).hexdigest()


def local_endpoint(value: str) -> tuple[str, int]:
    """Only a loopback HTTP lodge endpoint; never proxy/redirect/provider URLs."""
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment):
        raise ValueError("notify tool requires a local lodge endpoint")
    return parsed.hostname, parsed.port or 80


def codex_overrides(record: dict, endpoint: str, writable_roots: list[Path]) -> list[str]:
    """Set launcher-owned server fields at CLI precedence.

    -I ignores PYTHONPATH/user site/customizations. Both script and interpreter
    must resolve outside the intended session's writable roots. The effective
    installation still relies on the actual sandbox policy/trusted host.
    Nested maps merge; verify_codex_definition refuses unexpected additions.
    """
    local_endpoint(endpoint)
    worker = Path(__file__).resolve()
    python = Path(sys.executable).resolve()
    for root in writable_roots:
        root = root.resolve()
        if any(path == root or path.is_relative_to(root) for path in (worker, python)):
            raise ValueError("notify tool installation is writable by this session")
    definition = {
        "command": str(python),
        "args": ["-I", str(worker), "--session", record["name"],
                 "--binding", route_binding(record), "--api", endpoint],
        "cwd": str(worker.parent),
        "env": {}, "env_vars": [],
        "enabled": True, "required": True,
        "enabled_tools": [TOOL_NAME], "disabled_tools": [],
        "tools": {TOOL_NAME: {"approval_mode": "approve"}},
        "startup_timeout_sec": 10, "tool_timeout_sec": 120,
    }
    # JSON strings/arrays are valid TOML values; tables require key = value.
    def toml(value):
        if isinstance(value, dict):
            return "{" + ", ".join(f"{key} = {toml(item)}" for key, item in value.items()) + "}"
        return json.dumps(value, ensure_ascii=True)
    return ["-c", f"mcp_servers.{SERVER_NAME}={toml(definition)}"]


def verify_codex_definition(overrides: list[str], wrapper: str, cwd: Path) -> None:
    """Check effective launch fields using the same wrapper/config/cwd.

    Codex merges nested maps: env={} does NOT erase a preexisting env entry.
    Refuse any resulting deviation rather than launching a worker with it.
    This checks configuration, not effective runtime tool approval behavior.
    """
    expected = tomllib.loads(overrides[1])["mcp_servers"][SERVER_NAME]
    env = dict(os.environ)
    env.pop("WHATSAPP_AGENT_TOKEN", None)
    try:
        version = subprocess.run([wrapper, "--version"], cwd=cwd, env=env,
                                 capture_output=True, text=True, timeout=15, check=True)
        match = re.search(r"codex-cli (\d+)\.(\d+)\.(\d+)", version.stdout)
        if not match or tuple(map(int, match.groups())) < (0, 163, 0):
            raise ValueError("unsupported Codex")
        result = subprocess.run([wrapper, *overrides, "mcp", "get", SERVER_NAME, "--json"],
                                cwd=cwd, env=env, capture_output=True, text=True,
                                timeout=15, check=True)
        data = json.loads(result.stdout)
        transport = data.get("transport", {})
        if (data.get("enabled") is not True or data.get("disabled_reason")
                or data.get("enabled_tools") != [TOOL_NAME] or data.get("disabled_tools") != []
                or transport.get("type") != "stdio"
                or any(transport.get(key) != expected[key]
                       for key in ("command", "args", "cwd", "env", "env_vars"))
                or any(data.get(key) != expected[key]
                       for key in ("startup_timeout_sec", "tool_timeout_sec"))):
            raise ValueError("conflicting MCP definition")
        # mcp get does NOT expose required or per-tool approval_mode. Their
        # trusted CLI values/parser controls are not proof of runtime policy.
    except Exception:
        raise ValueError("owner notification tool requires Codex 0.163+ and an unmodified effective worker definition") from None


def _post_message_once(session: str, binding: str, api: str, message: str) -> str:
    host, port = local_endpoint(api)
    connection = http.client.HTTPConnection(host, port, timeout=90)
    body = json.dumps({"session": session, "binding": binding, "message": message},
                      ensure_ascii=False).encode("utf-8")
    try:
        # http.client does not follow redirects, use proxy env, or retry sends.
        connection.request("POST", "/notify/owner", body=body,
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        data = response.read(4097)
        if len(data) > 4096:
            return "ambiguous"
        value = json.loads(data)
        status = value.get("status") if isinstance(value, dict) else None
        return status if isinstance(status, str) and status in STATUSES else "ambiguous"
    except (OSError, ValueError, http.client.HTTPException):
        # Even a connect failure gets the conservative unknown-outcome rule.
        return "ambiguous"
    finally:
        connection.close()


def post_message(session: str, binding: str, api: str, message: str) -> str:
    """One attempt with a wall deadline shorter than the 120s tool deadline.

    A timed-out accepted server request may continue sending. Never retry or
    claim cancellation; the worker subsequently refuses any further sends.
    """
    finished = threading.Event()
    status = ["ambiguous"]
    def attempt():
        try:
            status[0] = _post_message_once(session, binding, api, message)
        except Exception:
            pass
        finally:
            finished.set()
    threading.Thread(target=attempt, name="owner-notify-request", daemon=True).start()
    return status[0] if finished.wait(REQUEST_DEADLINE_SECONDS) else "ambiguous"


def serve(input_stream, output_stream, send) -> None:
    """Serial newline-delimited MCP JSON-RPC. EOF revokes this worker."""
    ambiguous = False
    attempted = False

    def emit(value):
        output_stream.write(json.dumps(value, ensure_ascii=True).encode() + b"\n")
        output_stream.flush()

    while True:
        raw = input_stream.readline(MAX_FRAME_BYTES + 1)
        if not raw:
            return
        if len(raw) > MAX_FRAME_BYTES:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "request too large"}})
            return
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "invalid JSON"}})
            continue
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}})
            continue
        if "id" not in request:
            if request.get("method") == "notifications/cancelled" and attempted:
                # Serial I/O cannot undo an admitted send. A cancellation
                # queued during it is processed afterwards; conservatively
                # disable later sends because caller may not know the result.
                ambiguous = True
            continue
        request_id = request["id"]
        if not (type(request_id) is int or isinstance(request_id, str)):
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request id"}})
            continue
        method = request.get("method")
        params = request.get("params", {})
        result = None
        error = None
        if method == "initialize":
            version = params.get("protocolVersion") if isinstance(params, dict) else None
            result = {"protocolVersion": version if version in {
                "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
            } else "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0"},
                "instructions": "Use notify_owner for this session's owner. Do not resend after ambiguous status."}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [{"name": TOOL_NAME,
                "description": "Send a text notification to this session's fixed owner route. Never automatically resend an ambiguous outcome.",
                "inputSchema": {"type": "object", "properties": {
                    "message": {"type": "string", "minLength": 1, "maxLength": MAX_MESSAGE_CHARS}},
                    "required": ["message"], "additionalProperties": False},
                "annotations": {"readOnlyHint": False, "destructiveHint": False,
                                "idempotentHint": False, "openWorldHint": True}}]}
        elif method == "tools/call":
            args = params.get("arguments") if isinstance(params, dict) else None
            if (not isinstance(params, dict) or params.get("name") != TOOL_NAME
                    or not isinstance(args, dict) or set(args) != {"message"}
                    or not valid_message(args["message"])):
                error = {"code": -32602, "message": "only notify_owner(message) is supported"}
            else:
                attempted = True
                status = "ambiguous" if ambiguous else send(args["message"])
                if not isinstance(status, str) or status not in STATUSES:
                    status = "ambiguous"
                ambiguous = status == "ambiguous"
                text = ("Notification sent." if status == "sent" else
                        "Notification outcome ambiguous. Do not resend; this worker will make no further send attempts."
                        if status == "ambiguous" else f"Notification refused: {status}.")
                result = {"content": [{"type": "text", "text": text}], "isError": status != "sent"}
        else:
            error = {"code": -32601, "message": "method not supported"}
        emit({"jsonrpc": "2.0", "id": request_id, **({"error": error} if error else {"result": result})})


def main() -> int:
    os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
    parser = argparse.ArgumentParser(description="Launcher-bound owner notification MCP worker")
    parser.add_argument("--session", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--api", required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,160}", args.session):
            raise ValueError("invalid session")
        if not re.fullmatch(r"[a-f0-9]{64}", args.binding):
            raise ValueError("invalid binding")
        local_endpoint(args.api)
        serve(sys.stdin.buffer, sys.stdout.buffer,
              lambda text: post_message(args.session, args.binding, args.api, text))
        return 0
    except Exception:
        # No raw exception/request/route or environment in stdout/stderr.
        print("notification tool unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
