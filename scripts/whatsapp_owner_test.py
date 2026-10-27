#!/usr/bin/env python3
"""Owner-operated WhatsApp transport check with isolated, mocked dispatch.

The parent prompts without echo and writes the Agent API key to a private,
gitignored file. A child process reads that file directly; the key is never
passed in argv or environment. The child polls the real API and routes one
owner message through the WhatsApp adapter to a canned reply. It never starts
a lodge session, agent, tmux server, or local Woltspace server.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / ".whatsapp-owner-test"
TOKEN_FILE = PRIVATE / "agent-key"
TIMEOUT_SECONDS = 300
TEST_WOLT = "wa-test"


def _check_private_dir() -> None:
    if PRIVATE.is_symlink():
        raise RuntimeError("private test directory must not be a symlink")
    PRIVATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(PRIVATE, 0o700)
    st = PRIVATE.stat()
    if st.st_uid != os.getuid() or not stat.S_ISDIR(st.st_mode):
        raise RuntimeError("private test directory must belong to the current user")


def _require_tty() -> None:
    if not sys.stdin.isatty():
        raise RuntimeError("credential entry requires an interactive terminal")
    try:
        fd = os.open("/dev/tty", os.O_RDWR | getattr(os, "O_NOCTTY", 0))
    except OSError:
        raise RuntimeError("credential entry requires a real terminal at /dev/tty") from None
    try:
        if not os.isatty(fd):
            raise RuntimeError("credential entry requires a real terminal at /dev/tty")
    finally:
        os.close(fd)


def _child_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("WHATSAPP_AGENT_TOKEN", None)
    return env


def _store_token(token: str) -> None:
    _check_private_dir()
    if TOKEN_FILE.is_symlink():
        raise RuntimeError("credential file must not be a symlink")
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-key-", dir=PRIVATE)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(token)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, TOKEN_FILE)
        os.chmod(TOKEN_FILE, 0o600)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _read_token() -> str:
    _check_private_dir()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(TOKEN_FILE, flags)
    except OSError:
        raise RuntimeError("private credential file is missing or unsafe; rerun setup") from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise RuntimeError("private credential file must be owned by you with mode 0600")
        stream = os.fdopen(fd, "r", encoding="utf-8")
        fd = -1
        with stream:
            token = stream.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)
    if not token:
        raise RuntimeError("private credential file is empty; rerun setup")
    return token


def _setup() -> int:
    _require_tty()
    print("This check polls the WhatsApp Agent API and sends one canned reply after your test message.")
    print("It uses isolated temporary state and a mocked session handler; it will not start a Wolt or agent.")
    if input("Continue? Type TEST to proceed: ").strip() != "TEST":
        print("Cancelled; no credential was requested.")
        return 1
    with warnings.catch_warnings():
        # Turn getpass's echoing fallback into a failure before it reads input.
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("WhatsApp Agent API key (input hidden): ", stream=sys.stderr).strip()
    if not token:
        print("No credential entered.", file=sys.stderr)
        return 1
    try:
        _store_token(token)
    finally:
        del token
    print("Credential stored with private permissions. Starting isolated transport check.")
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--transport-run"],
        cwd=ROOT,
        env=_child_environment(),
        check=False,
    )
    return result.returncode


def _isolated_run() -> int:
    # Use a process-local token value only; do not export it into the
    # environment inherited by adapters, sessions, tmux, or child processes.
    token = _read_token()
    run_root = Path(tempfile.mkdtemp(prefix="run-", dir=PRIVATE))
    try:
        os.chmod(run_root, 0o700)
        return _isolated_run_body(token, run_root)
    except KeyboardInterrupt:
        print("Stopped by owner; no additional reply was sent.")
        return 130
    except Exception as exc:
        # Setup and import errors are kept as private as request errors.
        print(f"Isolated check stopped ({type(exc).__name__}); no credential value was printed.", file=sys.stderr)
        return 2
    finally:
        del token
        shutil.rmtree(run_root, ignore_errors=True)


def _isolated_run_body(token: str, run_root: Path) -> int:
    wolt_json = run_root / TEST_WOLT / "wolt" / "wolt.json"
    wolt_json.parent.mkdir(mode=0o700, parents=True)
    wolt_json.write_text(json.dumps({"name": TEST_WOLT, "type": "otter"}) + "\n")
    os.chmod(wolt_json, 0o600)
    os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
    os.environ.pop("WOLTS_DIR", None)
    os.environ.pop("WOLT_DIR", None)
    os.environ.pop("WOLT_NAME", None)
    os.environ.pop("WOLTSPACE_WOLT_DIR", None)
    os.environ.pop("WOLTSPACE_API", None)
    os.environ["WOLTSPACE_DIR"] = str(ROOT)
    os.environ["WOLTSPACE_WOLTS_DIR"] = str(run_root)
    os.environ["WOLTSPACE_WOLT_NAME"] = TEST_WOLT
    sys.path[:0] = [str(ROOT / "container"), str(ROOT / "container" / "lib"),
                    str(ROOT / "src"), str(ROOT)]

    # Isolate the adapter from bot.core and the lodge's Wolt discovery. These
    # stubs cannot start sessions or reach the running server.
    core = types.ModuleType("bot.core")
    completed = {"reply": False, "error": ""}
    send_attempt = {"started": False}
    adapter_ref: dict[str, object] = {}
    import secrets
    challenge = "wa-check-" + secrets.token_hex(4)

    def send_canned_reply(participant: str) -> None:
        if send_attempt["started"] or completed["reply"] or completed["error"]:
            return
        # Set before I/O: timeout/error leaves delivery ambiguous, so never
        # issue a second send for the same one-time phrase.
        send_attempt["started"] = True
        adapter = adapter_ref["adapter"]
        try:
            adapter.api("POST", "/messages", {
                "messaging_product": "whatsapp",
                "to": participant,
                "type": "text",
                "text": {"body": "WhatsApp transport check succeeded; the isolated test runner received your message."},
            })
        except Exception as exc:
            completed["error"] = type(exc).__name__
        else:
            completed["reply"] = True

    def message_text(prompt: str) -> str:
        marker = "]:"
        return prompt.split(marker, 1)[1].split("\n", 1)[0].strip() if marker in prompt else ""

    def start_claude_session(prompt: str, *, routing: dict, **kwargs) -> dict:
        if message_text(prompt) == challenge:
            send_canned_reply(routing["chat_id"])
        return {"name": "isolated-whatsapp-test-session", "url": ""}

    core.start_claude_session = start_claude_session

    def message_session(_session_name: str, prompt: str) -> dict:
        if message_text(prompt) != challenge or completed["reply"] or completed["error"]:
            return {"ok": True}
        marker = "[WhatsApp message from "
        participant = prompt.split(marker, 1)[1].split("]:", 1)[0]
        send_canned_reply(participant)
        if completed["error"]:
            raise RuntimeError("mocked reply outcome is ambiguous")
        return {"ok": True}

    core.message_session = message_session
    sys.modules["bot.core"] = core

    wolts = types.ModuleType("wolts")
    wolts.list_wolts = lambda: [{"name": TEST_WOLT, "type": "otter"}]
    sys.modules["wolts"] = wolts

    lib = types.ModuleType("lib")
    lib.__path__ = [str(ROOT / "container" / "lib")]
    paths = types.ModuleType("lib.paths")
    paths.space_dir = lambda _root=None: run_root / ".space"
    notify_prompt = types.ModuleType("lib.notify_prompt")
    notify_prompt.notify_reply_instruction = lambda *args: "test-only notify route"
    sys.modules.update({"lib": lib, "lib.paths": paths, "lib.notify_prompt": notify_prompt})

    # Prevent the adapter's import-time env requirement from exposing the
    # actual value. Replace its in-memory constant immediately after import.
    os.environ["WHATSAPP_AGENT_TOKEN"] = "isolated-test-placeholder"
    try:
        import bot.whatsapp_adapter as adapter
    finally:
        os.environ.pop("WHATSAPP_AGENT_TOKEN", None)
    adapter_ref["adapter"] = adapter
    adapter.TOKEN = token
    adapter.ACCOUNT = hashlib.sha256(token.encode()).hexdigest()[:20]
    adapter.CURSOR = adapter.STATE / f"cursor-{adapter.ACCOUNT}.json"
    adapter.ROUTES = adapter.STATE / f"routes-{adapter.ACCOUNT}.json"
    adapter.LOCK = adapter.STATE / f"poll-{adapter.ACCOUNT}.lock"
    del token

    activation = int(time.time())
    state = {"offset": 0, "activation": activation, "processed": []}
    adapter.atomic_json(adapter.CURSOR, state)
    mode = sys.argv[1]
    if mode == "--notify-run":
        dispatch = _notify_path_dispatch(adapter, run_root, challenge, completed, send_attempt)
        print(f"Isolated notify-path poll started. Send `{challenge}` once in WhatsApp.")
    else:
        dispatch = adapter.handle
        print("Isolated poll started. In WhatsApp, send `/wolt wa-test` and wait for its confirmation.")
        print(f"Then send `{challenge}` once. The runner will send one canned reply and exit.")
    print("No incoming message text or participant ID is printed. Press Ctrl+C to stop.")
    routes = adapter.load(adapter.ROUTES)
    selected_prompted = False
    deadline = time.monotonic() + TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline and not completed["reply"] and not completed["error"]:
            state = adapter.poll_once(
                state, routes, activation, cursor_path=adapter.CURSOR, dispatch=dispatch
            )
            routes = adapter.load(adapter.ROUTES)
            if routes and not selected_prompted:
                selected_prompted = True
                print("Test destination selected. Send the one-time test phrase now.")
        if completed["reply"]:
            print("One owner message received and one canned WhatsApp reply sent.")
            return 0
        if completed["error"]:
            print(f"Reply attempt ended ambiguously ({completed['error']}); check WhatsApp before another test.")
            return 2
        print("Timed out waiting for the test message; no reply was sent by the test handler.")
        return 2
    except KeyboardInterrupt:
        print("Stopped by owner; no additional reply was sent.")
        return 130
    except Exception as exc:
        # Do not print exception text or traceback; neither is needed to
        # diagnose the test and this keeps transport details out of logs.
        print(f"Isolated check stopped ({type(exc).__name__}); no credential value was printed.", file=sys.stderr)
        return 2


def _cleanup() -> int:
    if PRIVATE.is_symlink():
        print("Refusing to remove a symlinked private directory.", file=sys.stderr)
        return 2
    if PRIVATE.exists():
        shutil.rmtree(PRIVATE)
    print("Removed the private WhatsApp test directory and stored credential.")
    return 0


def _notify_path_dispatch(adapter, run_root: Path, challenge: str, completed: dict,
                          send_attempt: dict):
    """Build a real registry→notify→sender test in the isolated data root."""
    import asyncio
    import server.notify as notify

    notify.dotenv_env = lambda name: adapter.TOKEN if name == "WHATSAPP_AGENT_TOKEN" else ""
    notify.append_chat_history = lambda *args: None
    session = "isolated-whatsapp-notify-test"

    def dispatch(item: dict, routes: dict, activation: int) -> None:
        text = (item.get("text") or {}).get("body", "")
        participant = item.get("from", "")
        if (text.strip() != challenge or not participant.startswith("user:")
                or send_attempt["started"] or completed["reply"] or completed["error"]):
            return
        # Reserve the one permitted delivery before any session/notify work.
        send_attempt["started"] = True
        try:
            registry = notify.SessionRegistry(run_root)
            registry.create(
                session,
                wolt=TEST_WOLT,
                adapter="whatsapp",
                chat_id=participant,
                whatsapp_account=adapter.ACCOUNT,
            )
            asyncio.run(notify.send_notification(
                session, "WhatsApp registry and notify path check succeeded."
            ))
            completed["reply"] = True
        except Exception as exc:
            completed["error"] = type(exc).__name__
            # Do not print participant, message, token, or exception details.
            print(f"Notify test failed ({type(exc).__name__}); check before retrying.", file=sys.stderr)
    return dispatch


def main() -> int:
    args = sys.argv[1:]
    if args == ["--cleanup"]:
        return _cleanup()
    if args == ["--notify-path"]:
        try:
            _require_tty()
            print("This sends one test message through the isolated real notify route.")
            if input("Continue? Type NOTIFY to proceed: ").strip() != "NOTIFY":
                print("Cancelled; no API call was made.")
                return 1
            _check_private_dir()
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--notify-run"],
                cwd=ROOT,
                env=_child_environment(),
                check=False,
            )
            return result.returncode
        except Exception as exc:
            print(f"Setup stopped ({type(exc).__name__}); no credential value was printed.", file=sys.stderr)
            return 2
    if args in (["--transport-run"], ["--notify-run"]):
        return _isolated_run()
    if args:
        print("Usage: python scripts/whatsapp_owner_test.py [--cleanup]", file=sys.stderr)
        return 2
    try:
        return _setup()
    except Exception as exc:
        print(f"Setup stopped ({type(exc).__name__}); no credential value was printed.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
