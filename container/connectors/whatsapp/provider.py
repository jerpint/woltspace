"""Official Agent Platform transport and private per-key checkpoints.

No lodge commands, routing, sessions or model imports. The authenticated
creator-only poll and explicit lodge owner list must both authorize a sender.
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from rate_limit import pace_request, request_slot
from whatsapp_binding import BindingError, account_for_key, binding_path, read_creator, valid_participant

log = logging.getLogger("whatsapp_provider")
API = "https://api.whatsapp.com/agent/v1"
TOKEN = ""
OWNER = ""
STATE = Path(".")
ACCOUNT = ""
CURSOR = Path("cursor.json")


def configure(token: str, state: Path, owner: str) -> None:
    global TOKEN, OWNER, STATE, ACCOUNT, CURSOR
    if not token or not valid_participant(owner):
        raise WhatsAppConfigurationError("Configure an Agent API key and one explicit creator owner id")
    TOKEN, OWNER, STATE = token, owner, state
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    ACCOUNT = account_for_key(token)
    CURSOR = STATE / f"cursor-{ACCOUNT}.json"


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


def _owner_path() -> Path:
    return binding_path(STATE, ACCOUNT)


def _creator(*, expected: str | None = None) -> str | None:
    # Key changes must never inherit another account's creator/command authority.
    if account_for_key(TOKEN) != ACCOUNT:
        raise WhatsAppConfigurationError("WhatsApp account binding does not match the configured key")
    try:
        return read_creator(STATE, ACCOUNT, expected=expected)
    except BindingError as exc:
        raise WhatsAppConfigurationError(str(exc)) from None


def _provider_owner(item: dict, activation: int) -> bool:
    """Called only on the authenticated creator-only provider poll, never by handle.

    First valid fresh text establishes ownership before dispatch, under the
    existing exclusive per-key consumer lock. Syntax alone is not authentication.
    """
    sender = item.get("from")
    if sender != OWNER:
        return False
    mid = item.get("id")
    if not isinstance(mid, str) or not mid.strip() or len(mid) > 512 or not valid_participant(sender):
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


def poll_once(state: dict, activation: int, *, request=api, dispatch) -> dict:
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
    if type(next_offset) is not int or next_offset < int(state.get("offset", 0)):
        raise ValueError("WhatsApp updates response has no valid next_offset")
    state["offset"] = next_offset
    atomic_json(CURSOR, state)
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
        atomic_json(CURSOR, state)
        if not too_old:
            # Cursor/dedup are checkpointed first: a binding-write failure may
            # safely lose this item, but must never dispatch without ownership.
            if not _provider_owner(item, activation):
                continue
            try:
                dispatch(item)
            except Exception as exc:
                # This item is marked before dispatch to avoid replaying an
                # ambiguous side effect. Continue so unrelated later updates
                # in the same checkpointed batch are not lost.
                log.error("WhatsApp message dispatch failed (%s)", type(exc).__name__)
    return state
