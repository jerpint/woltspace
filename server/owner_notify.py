"""Constrained owner notifications under the lodge's existing host trust.

Session IDs and binding digests route/revoke; they do not authenticate callers.
"""
import asyncio
import hashlib
import hmac
import re

from notify_owner_mcp import MAX_MESSAGE_BYTES, MAX_MESSAGE_CHARS, route_binding, valid_message
from . import notify

_pending: set[str] = set()
MAX_PENDING = 8


async def deliver(body: object) -> str:
    if (not isinstance(body, dict) or set(body) != {"session", "binding", "message"}
            or not valid_message(body.get("message"))):
        return "invalid_message"
    session, binding = body["session"], body["binding"]
    if (not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,160}", session)
            or not isinstance(binding, str) or not re.fullmatch(r"[a-f0-9]{64}", binding)):
        return "stale_binding"
    if notify.dotenv_env("WOLTSPACE_NOTIFY_TOOL") != "1":
        return "unavailable"
    record = await asyncio.to_thread(notify.read_session_registry, session)
    if (not isinstance(record, dict) or record.get("name") != session
            or record.get("status") != "running" or record.get("harness") != "codex"
            or record.get("adapter") != "whatsapp" or not record.get("chat_id")
            or not record.get("wolt") or not record.get("notify_owner_generation")
            or not hmac.compare_digest(route_binding(record), binding)):
        return "stale_binding"
    token = notify.dotenv_env("WHATSAPP_AGENT_TOKEN")
    if not token:
        return "unavailable"
    account = hashlib.sha256(token.encode()).hexdigest()[:20]
    if record.get("whatsapp_account") != account:
        return "stale_binding"
    if session in _pending or len(_pending) >= MAX_PENDING:
        return "busy"
    emoji = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "wolf": "🐺"}.get(record.get("creature"), "🦫")
    message = f"{emoji} {record['wolt']}: {body['message']}"
    # Bound the final identity-prefixed provider text too.
    if len(message) > MAX_MESSAGE_CHARS or len(message.encode()) > MAX_MESSAGE_BYTES:
        return "invalid_message"
    _pending.add(session)
    try:
        # Acceptance uses the validated registry snapshot above. Once passed
        # to the sender (including its rate wait), this call is in flight:
        # stop/relaunch/route changes reject future calls but do not cancel
        # this accepted one or retarget it to a different owner/account.
        # No generic route resolver / Telegram fallback or caller overrides.
        # The existing sender rechecks current account and shares send pacing.
        await notify._send("whatsapp", session, message, str(record["chat_id"]),
                           routing=record, session_record=record)
        return "sent"
    except Exception:
        # A timeout or post-send history failure may already have delivered.
        # Never expose raw provider/exception/message/route data or retry here.
        return "ambiguous"
    finally:
        _pending.discard(session)
