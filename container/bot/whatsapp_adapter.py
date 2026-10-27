"""Text-only connector for the official WhatsApp Agent Platform API.

This API is creator-only and not end-to-end encrypted. It is separate from
WhatsApp Business Cloud API and linked-device automation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import ipaddress
import importlib
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from bot.core import message_session, start_claude_session
from wolts import list_wolts
from lib.paths import space_dir
from lib.notify_prompt import notify_reply_instruction
from woltspace.whatsapp_rate_limit import pace_request, request_slot

log = logging.getLogger("whatsapp")
API = "https://api.whatsapp.com/agent/v1"
TOKEN = os.environ.pop("WHATSAPP_AGENT_TOKEN")
WOLTS_DIR = Path(os.environ.get("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
WOLT_NAME = os.environ.get("WOLTSPACE_WOLT_NAME", "wolt")
STATE = space_dir(WOLTS_DIR) / "whatsapp"
STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
ACCOUNT = hashlib.sha256(TOKEN.encode()).hexdigest()[:20]
CURSOR = STATE / f"cursor-{ACCOUNT}.json"
ROUTES = STATE / f"routes-{ACCOUNT}.json"
LOCK = STATE / f"poll-{ACCOUNT}.lock"
RODENT_TYPES = {"raccoon", "beaver", "otter", "rodent"}


class WhatsAppAPIError(RuntimeError):
    """Safe typed metadata; no provider body/header/URL retained."""
    def __init__(self, message, *, http_status=None, provider_code=None):
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None
        self.provider_code = provider_code if type(provider_code) is int and provider_code == 100 else None
        super().__init__(message)


class WhatsAppConfigurationError(WhatsAppAPIError):
    """An account or poller conflict needs operator action, not retries."""


class WhatsAppRateLimit(WhatsAppAPIError):
    def __init__(self, retry_after: int = 5, *, provider_code=None):
        self.retry_after = max(1, min(60, retry_after))
        super().__init__("WhatsApp Agent API rate limit reached", http_status=429,
                         provider_code=provider_code)


def eligible_wolts() -> dict[str, dict]:
    return {
        item.get("name", ""): item for item in list_wolts()
        if item.get("name") and item.get("type") in RODENT_TYPES
    }


def api(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    if method == "POST" and path == "/messages":
        for attempt in range(3):
            try:
                with request_slot(STATE, TOKEN, 12, "send"):
                    return _api_request(method, path, payload)
            except WhatsAppRateLimit as exc:
                if attempt == 2:
                    raise
                time.sleep(exc.retry_after)
    elif method == "GET" and path.startswith("/updates?"):
        pace_request(STATE, TOKEN, 15, "poll")
    return _api_request(method, path, payload)


def _api_request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + TOKEN,
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        # Do not surface response bodies: they may echo submitted content.
        if exc.code == 204:
            return 204, {}
        # Parse only a documented allowlisted numeric error code. The bounded
        # body is consumed opaquely and never retained/logged with the error.
        provider_code = None
        try:
            error = json.loads(exc.read(4097)).get("error", {})
            code = error.get("code") if isinstance(error, dict) else None
            if type(code) is int and code == 100:
                provider_code = code
        except (OSError, AttributeError, TypeError, ValueError):
            pass
        if exc.code in (401, 403):
            raise WhatsAppConfigurationError(
                f"WhatsApp Agent API authentication failed (HTTP {exc.code}); check the Agent API key",
                http_status=exc.code, provider_code=provider_code,
            ) from None
        if exc.code == 400 and method == "GET" and path.startswith("/updates?"):
            if provider_code == 100:
                raise WhatsAppConfigurationError(
                    "WhatsApp Agent API rejected the key (HTTP 400, error 100); check the Agent API key",
                    http_status=400, provider_code=100,
                ) from None
        if exc.code == 409:
            raise WhatsAppConfigurationError(
                "WhatsApp Agent API rejected this poller (HTTP 409); verify there is only one active poller",
                http_status=409, provider_code=provider_code,
            ) from None
        if exc.code == 429:
            try:
                retry_after = int(exc.headers.get("Retry-After", "5")) if exc.headers else 5
            except (TypeError, ValueError):
                retry_after = 5
            raise WhatsAppRateLimit(retry_after, provider_code=provider_code) from None
        raise WhatsAppAPIError(f"WhatsApp Agent API request failed (HTTP {exc.code})",
                               http_status=exc.code, provider_code=provider_code) from None


def atomic_json(path: Path, value: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".wa-", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        try: os.unlink(tmp)
        except FileNotFoundError: pass


def load(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def messages(body: dict):
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for item in value.get("messages", []):
                yield item



def _participant(value) -> bool:
    return (isinstance(value, str) and value.startswith("user:") and 5 < len(value) <= 256
            and all(char.isprintable() and not char.isspace() for char in value))


def _owner_path() -> Path:
    return STATE / f"owner-{ACCOUNT}.json"


def _creator() -> str | None:
    # Key changes must never inherit another account's creator/command authority.
    if hashlib.sha256(TOKEN.encode()).hexdigest()[:20] != ACCOUNT:
        raise WhatsAppConfigurationError("WhatsApp account binding does not match the configured key")
    path = _owner_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise WhatsAppConfigurationError("WhatsApp creator binding is unreadable") from None
    try:
        with os.fdopen(fd) as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError("invalid private binding")
            data = json.load(source)
        if (not isinstance(data, dict) or set(data) != {"account", "participant"}
                or data["account"] != ACCOUNT or not _participant(data["participant"])):
            raise ValueError("invalid creator binding")
        return data["participant"]
    except (OSError, ValueError, TypeError, RecursionError):
        raise WhatsAppConfigurationError("WhatsApp creator binding is invalid; inspect private state") from None


def _provider_owner(item: dict, activation: int) -> bool:
    """Called only on the authenticated creator-only provider poll, never by handle.

    First valid fresh text establishes ownership before dispatch, under the
    existing exclusive per-key consumer lock. Syntax alone is not authentication.
    """
    sender = item.get("from")
    mid = item.get("id")
    if not isinstance(mid, str) or not mid.strip() or len(mid) > 512 or not _participant(sender):
        return False
    try:
        if int(item.get("timestamp", "0")) < activation:
            return False
    except (TypeError, ValueError):
        return False
    if item.get("type") == "text":
        text = item.get("text")
        if (not isinstance(text, dict) or not isinstance(text.get("body"), str)
                or not text["body"].strip() or len(text["body"]) > 4096):
            return False
    creator = _creator()
    if creator is None:
        if item.get("type") != "text":
            return False
        try:
            atomic_json(_owner_path(), {"account": ACCOUNT, "participant": sender})
        except OSError:
            raise WhatsAppConfigurationError("Could not persist WhatsApp creator binding; no command was dispatched") from None
        creator = _creator()
    return creator == sender


def _reply(sender: str, text: str) -> None:
    if len(text) > 4096:
        text = text[:4070] + "\n… (list truncated)"
    api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
        "type": "text", "text": {"body": text}})


def _select(sender: str, name: str, routes: dict) -> None:
    updated = {**routes, sender: {"wolt": name}}
    atomic_json(ROUTES, updated)
    routes.clear()
    routes.update(updated)


def _command(text: str, sender: str, routes: dict) -> tuple[str | None, str]:
    command, *args = text.split()
    command = command.lower()
    if command == "/wolt" and len(args) <= 1:
        wolts = eligible_wolts()
        if not args:
            return None, "Available: " + (", ".join(sorted(wolts)) or "none") + "\nUse /wolt NAME."
        if args[0] not in wolts:
            return None, "That wolt is unavailable. Use /wolt to choose an installed wolt."
        _select(sender, args[0], routes)
        return None, f"Selected {args[0]}. Send a message to start its session."
    return None, "Choose a target with /wolt NAME, then send text."


def handle(item: dict, routes: dict, activation: int) -> None:
    mid, sender = item.get("id"), item.get("from")
    if not isinstance(mid, str) or not _participant(sender) or _creator() != sender:
        return
    try:
        timestamp = int(item.get("timestamp", "0"))
    except (TypeError, ValueError):
        return
    if timestamp < activation:
        return
    if item.get("type") != "text":
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": "I can handle text messages here; attachments are not supported yet."}})
        return
    raw_text = item["text"].get("body", "") if isinstance(item.get("text"), dict) else ""
    if not isinstance(raw_text, str):
        return
    text = raw_text.strip()
    fresh_command = False
    if not text:
        return
    if text.startswith("wolt "):
        text = "/" + text  # Compatibility only; slash notation is canonical.
    if text.startswith("/"):
        try:
            prompt_text, reply = _command(text, sender, routes)
        except Exception as exc:
            log.error("WhatsApp control outcome unconfirmed (%s)", type(exc).__name__)
            prompt_text, reply = None, "Could not confirm the command outcome. Check the lodge before repeating it."
        if reply:
            _reply(sender, reply)  # Ack failure never retries or reruns the action.
        if prompt_text is None:
            return
        fresh_command = True
        text = prompt_text
    route = routes.get(sender)
    if not route or not route.get("wolt"):
        names = ", ".join(eligible_wolts())
        reply = "Choose a wolt first by sending `/wolt NAME`. Available: " + (names or "none")
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": reply}})
        return
    # Persisted route is bound to this account's hashed key; selection cannot
    # carry over when credentials change. A first message creates a session;
    # later messages go only to the exact recorded session.
    if route["wolt"] not in eligible_wolts():
        routes.pop(sender, None)
        atomic_json(ROUTES, routes)
        api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
             "type": "text", "text": {"body": "That wolt is no longer available. Choose again with `/wolt NAME`."}})
        return
    route_instruction = notify_reply_instruction("--whatsapp", sender)
    prompt = f"[WhatsApp message from {sender}]: {text}\n{route_instruction}"
    routing = {"adapter": "whatsapp", "chat_id": sender, "whatsapp_account": ACCOUNT}
    started_session = False
    link = ""
    if route.get("session"):
        try:
            result = message_session(route["session"], prompt)
            if not result.get("ok"):
                raise RuntimeError("session did not accept message")
        except Exception as exc:
            log.error("Could not confirm WhatsApp message dispatch (%s)", type(exc).__name__)
            try:
                api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                     "type": "text", "text": {"body": "I could not confirm delivery to the session. Check the session before sending this message again."}})
            except Exception:
                log.error("Could not send the WhatsApp dispatch status reply")
        return

    # The session start can have an ambiguous outcome if the control plane
    # fails after creating it. Never ask the owner to blindly resubmit.
    try:
        session = start_claude_session(prompt, wolt=route["wolt"], routing=routing)
    except Exception as exc:
        log.error("Could not confirm WhatsApp session start (%s)", type(exc).__name__)
        try:
            api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                 "type": "text", "text": {"body": "I could not confirm whether a session started. Check your sessions before sending this message again."}})
        except Exception:
            log.error("Could not send the WhatsApp session status reply")
        return
    if not isinstance(session, dict) or not isinstance(session.get("name"), str) or not session["name"]:
        log.error("WhatsApp session start returned no session name")
        try:
            api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                 "type": "text", "text": {"body": "I could not confirm whether a session started. Check your sessions before sending this message again."}})
        except Exception:
            log.error("Could not send the WhatsApp session status reply")
        return

    # Session creation succeeded. Route persistence is a separate outcome; if
    # it fails, keep the in-memory route and report the tracking problem without
    # claiming the session failed or inviting blind resubmission.
    route["session"] = session["name"]
    routes[sender] = route
    started_session = True
    link = session.get("url", "")
    try:
        atomic_json(ROUTES, routes)
    except Exception as exc:
        log.error("WhatsApp session started but route persistence failed (%s)", type(exc).__name__)
        try:
            api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                 "type": "text", "text": {"body": "The session started, but I could not save its reply route. Check your sessions before sending another message here."}})
        except Exception:
            log.error("Could not send the WhatsApp route status reply")
        return
    # Starting/resuming the session is the dispatch. A failed acknowledgement
    # must not invite a duplicate message and duplicate instructions.
    if started_session and (link or fresh_command):
        try:
            api("POST", "/messages", {"messaging_product": "whatsapp", "to": sender,
                 "type": "text", "text": {"body": f"Session started: {link}"}})
        except Exception:
            log.error("Session started, but the WhatsApp acknowledgement could not be delivered")


def poll_once(state: dict, routes: dict, activation: int, *, cursor_path: Path = CURSOR,
              request=api, dispatch=handle) -> dict:
    """Fetch and checkpoint one batch. A 204 keeps the current cursor."""
    query = urllib.parse.urlencode({"offset": int(state.get("offset", 0)), "limit": 50, "timeout": 20})
    status, body = request("GET", "/updates?" + query)
    if status == 204:
        return state
    if status != 200:
        raise ValueError("WhatsApp updates request did not succeed")
    if not isinstance(body, dict):
        raise ValueError("WhatsApp updates response is malformed")
    next_offset = body.get("next_offset")
    batch = list(messages(body))
    if not isinstance(next_offset, int) or next_offset < int(state.get("offset", 0)):
        raise ValueError("WhatsApp updates response has no valid next_offset")
    state["offset"] = next_offset
    atomic_json(cursor_path, state)
    processed = list(state.get("processed", []))
    known = set(processed)
    for item in batch:
        if not isinstance(item, dict):
            log.error("Ignoring malformed WhatsApp update item")
            continue
        mid = item.get("id")
        if not isinstance(mid, str) or not mid or len(mid) > 512 or mid in known:
            continue
        try:
            too_old = int(item.get("timestamp", "0")) < activation
        except (TypeError, ValueError):
            too_old = True
        processed.append(mid)
        known.add(mid)
        state["processed"] = processed[-10000:]
        atomic_json(cursor_path, state)
        if not too_old:
            # Cursor/dedup are checkpointed first: a binding-write failure may
            # safely lose this item, but must never dispatch without ownership.
            if not _provider_owner(item, activation):
                continue
            try:
                dispatch(item, routes, activation)
            except Exception as exc:
                # This item is marked before dispatch to avoid replaying an
                # ambiguous side effect. Continue so unrelated later updates
                # in the same checkpointed batch are not lost.
                log.error("WhatsApp message dispatch failed (%s)", type(exc).__name__)
    return state


def main() -> None:
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("WhatsApp API key already has an active poller")
        state = load(CURSOR)
        activation = state.get("activation")
        if not isinstance(activation, int):
            activation = int(time.time())
            state = {"offset": 0, "activation": activation, "processed": []}
            atomic_json(CURSOR, state)
        routes = load(ROUTES)
        failures = 0
        while True:
            try:
                state = poll_once(state, routes, activation)
                failures = 0
            except WhatsAppConfigurationError as exc:
                log.error("%s", exc)
                raise SystemExit(1) from None
            except Exception as exc:
                failures += 1
                if failures >= 5:
                    raise RuntimeError("WhatsApp polling failed five consecutive times") from None
                delay = min(60, 5 * (2 ** (failures - 1)))
                log.error("WhatsApp poll failed (%s); retry %d/5 in %ds", type(exc).__name__, failures, delay)
                time.sleep(delay)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
