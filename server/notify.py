"""Notification system — send messages to Telegram/Slack.

Routing is explicit: the caller (rodent) tells us where to send via adapter flags.
Falls back to session registry lookup, then Telegram default.
"""

import json
import logging
import mimetypes
import urllib.parse
from pathlib import Path

from .notification_senders import (
    slack_send, slack_sender, slack_set_agent_status, telegram_send, telegram_sender,
)
from .notification_types import Attachment, OutboundMessage, Plugin, SendContext
import outbox
from sessions import SessionRegistry

from .config import (
    STATE_DIR,
    SPACE_DIR,
    SPACE_PLATFORM_DIR,
    WOLTS_DIR,
    DEN_REPLY_FOOTER,
    dotenv_env,
)
from .state import sanitize_session

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 1
# Where `notify --file` stages what it wants sent. Core is the only server code
# that touches it, and it opens entries by id, never a path a caller names.
OUTBOX_DIR = SPACE_DIR / "outbox"


class AttachmentError(RuntimeError):
    """A file the request named cannot be sent. `reason` is machine-readable."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class NoNotificationTarget(RuntimeError):
    """Nowhere to deliver to — a configuration gap, not a platform failure.

    Its own type so the API can answer 409 instead of 500: an unconnected chat
    is the user's next step, and a 500 tells them woltspace is broken. The
    agent that hit this had to guess which it was.
    """


def _slack_session_link(session: str, routing: dict | None) -> str:
    """Return only the adapter-validated link bound to this exact session."""
    url = (routing or {}).get("slack_session_link", "")
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    except (TypeError, ValueError):
        return ""
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path != "/tui" or parsed.fragment or
            query != {"session": [session]}):
        return ""
    return url



def read_session_registry(session: str) -> dict | None:
    """Find a session file by scanning all per-wolt .state/sessions/ dirs."""
    safe = sanitize_session(session)
    for entry in WOLTS_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        f = entry / ".state" / "sessions" / f"{safe}.json"
        if f.exists():
            try:
                return json.loads(f.read_text())
            except Exception:
                return None
    return None


def append_chat_history(adapter: str, chat_id: str, content: str):
    """Write notify messages into the bot's chat history."""
    from datetime import datetime, timezone

    if adapter == "slack":
        subdir = STATE_DIR / "chat" / "slack"
    else:
        subdir = STATE_DIR / "chat"
    subdir.mkdir(parents=True, exist_ok=True)
    chat_file = subdir / f"{chat_id}.jsonl"

    clean = content.replace(DEN_REPLY_FOOTER, "")
    entry = {
        "role": "user",
        "content": f"<system>This message was sent by a Claude Code session directly to the user. It is context only — do not respond to it.</system>\n{clean}",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with open(chat_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# Transport aliases are temporary compatibility patch points for existing tests.
# The providers own each implementation; these lambdas resolve patched names at call time.
SENDERS = {
    "telegram": Plugin(
        "telegram", telegram_sender(lambda *args: telegram_send(*args)),
        secrets=("TELEGRAM_BOT_TOKEN",),
    ),
    "slack": Plugin(
        "slack", slack_sender(
            lambda *args: slack_send(*args),
            lambda *args: slack_set_agent_status(*args),
        ),
        secrets=("SLACK_BOT_TOKEN",),
        route_state=("slack_progress_mode", "slack_progress_ts"),
    ),
}


def _resolve_attachments(attachments) -> tuple[Attachment, ...]:
    """Turn the request's outbox ids into attachments, measured and named by core."""
    if (
        not isinstance(attachments, (list, tuple))
        or len(attachments) > MAX_ATTACHMENTS
        or not all(
            isinstance(entry, dict) and isinstance(entry.get("id"), str)
            for entry in attachments
        )
    ):
        raise AttachmentError(
            "attachment_invalid",
            "attachments must be a list with at most one {id, name} entry",
        )
    resolved = []
    for entry in attachments:
        try:
            path, size = outbox.resolve(OUTBOX_DIR, entry["id"])
        except outbox.OutboxError as exc:
            raise AttachmentError(exc.reason, str(exc)) from None
        filename = outbox.display_name(entry.get("name"))
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        resolved.append(Attachment(path, filename, size, content_type))
    return tuple(resolved)


async def _send(
    adapter: str, session: str, message: str, chat_id: str,
    thread_ts: str | None = None, routing: dict | None = None,
    attachments: tuple[Attachment, ...] = (),
) -> dict:
    """Resolve core context, dispatch a built-in, and persist its declared updates."""
    sender = SENDERS[adapter]
    secrets = {key: dotenv_env(key) for key in sender.secrets}
    session_link = ""
    if adapter == "telegram":
        if not secrets.get("TELEGRAM_BOT_TOKEN"):
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        tunnel_url = ""
        try:
            state = json.loads((SPACE_PLATFORM_DIR / "tunnel.json").read_text())
            tunnel_url = state.get("url", "").strip()
        except Exception:
            pass
        if session:
            session_link = f"{tunnel_url}/tui?session={session}" if tunnel_url else f"session={session}"
        destination = {"chat_id": chat_id}
    else:
        if not secrets.get("SLACK_BOT_TOKEN"):
            raise RuntimeError("SLACK_BOT_TOKEN not set")
        if not chat_id:
            chat_id = dotenv_env("SLACK_NOTIFY_CHANNEL")
        if not chat_id:
            raise RuntimeError("no slack channel provided and SLACK_NOTIFY_CHANNEL not set")
        session_link = _slack_session_link(session, routing)
        destination = {"channel": chat_id}
        if thread_ts is not None:
            destination["thread_ts"] = thread_ts

    for attachment in attachments:
        if sender.max_attachment_bytes == 0:
            raise AttachmentError("attachments_unsupported", f"{adapter} cannot carry files")
        if attachment.size > sender.max_attachment_bytes:
            raise AttachmentError(
                "attachment_too_large",
                f"{attachment.filename} is {attachment.size} bytes; "
                f"{adapter} accepts at most {sender.max_attachment_bytes}",
            )

    context = SendContext(
        secrets,
        {key: routing[key] for key in sender.route_state if routing and key in routing},
    )
    try:
        await sender.send(
            OutboundMessage(message, session_link or None, attachments), destination, context,
        )
    finally:
        updates = {}
        for key, value in context.updates.items():
            if key in sender.route_state:
                updates[key] = value
            else:
                logger.warning("Ignored undeclared notification state key: %s", key)
        if updates and session and routing:
            SessionRegistry(WOLTS_DIR).update(session, wolt=routing.get("wolt", ""), **updates)
    append_chat_history(adapter, chat_id, message + "".join(
        f"\n[file sent: {a.filename}, {a.content_type}, {a.size} bytes]" for a in attachments
    ))
    result = {"adapter": adapter, "chat_id" if adapter == "telegram" else "channel": chat_id}
    if attachments:
        # Metadata only: this dict is logged and returned to the caller.
        result["attachments"] = [
            {"name": a.filename, "content_type": a.content_type, "size": a.size}
            for a in attachments
        ]
    return result


async def _send_telegram(session: str, message: str, chat_id: str) -> dict:
    """Compatibility wrapper; routing and local state remain in core."""
    return await _send("telegram", session, message, chat_id)


async def _send_slack(
    message: str, channel: str, thread_ts: str | None = None, *,
    session: str = "", routing: dict | None = None,
) -> dict:
    """Compatibility wrapper; routing and local state remain in core."""
    return await _send("slack", session, message, channel, thread_ts, routing)


async def send_notification(
    session: str, message: str, explicit: dict | None = None, attachments=(),
) -> dict:
    """Send a notification. Explicit routing takes priority over session lookup.

    explicit dict can contain:
      {"adapter": "slack", "channel": "C123", "thread_ts": "1234.5678"}
      {"adapter": "telegram", "chat_id": "98765"}

    attachments is what the request carried: [{"id": <outbox entry>, "name": ...}].
    Every entry it names is removed from the outbox, whatever happens to the send.
    """
    try:
        outbox.sweep(OUTBOX_DIR)
    except Exception as exc:
        logger.warning("Could not sweep the outbox: %s", exc)
    try:
        return await _route(session, message, explicit, _resolve_attachments(attachments))
    finally:
        if isinstance(attachments, (list, tuple)):
            for entry in attachments:
                if isinstance(entry, dict):
                    outbox.discard(OUTBOX_DIR, entry.get("id"))


async def _route(
    session: str, message: str, explicit: dict | None, attachments: tuple[Attachment, ...],
) -> dict:
    """Pick the destination: explicit route, then the session's, then the Telegram default."""
    routing = read_session_registry(session) if session else None

    # 1. Explicit routing — caller knows exactly where to send. A temporary
    # progress surface is usable only when the explicit route exactly matches
    # the authoritative session record.
    if explicit and explicit.get("adapter"):
        adapter = explicit["adapter"]
        if adapter == "slack":
            channel = explicit.get("channel", "")
            thread_ts = explicit.get("thread_ts")
            exact = bool(
                routing
                and routing.get("adapter") == "slack"
                and routing.get("chat_id") == channel
                and routing.get("thread_ts") == thread_ts
            )
            return await _send(
                adapter, session if exact else "", message, channel, thread_ts,
                routing if exact else None, attachments=attachments,
            )
        if adapter == "telegram":
            chat_id = explicit.get("chat_id", "")
            if chat_id:
                return await _send(
                    adapter, session, message, str(chat_id), attachments=attachments,
                )

    # 2. Session registry lookup — find routing from session metadata
    if session:
        if routing:
            adapter = routing.get("adapter")
            chat_id = routing.get("chat_id", "")
            if adapter == "slack" or (adapter == "telegram" and chat_id):
                return await _send(
                    adapter, session, message,
                    str(chat_id) if adapter == "telegram" else chat_id,
                    routing.get("thread_ts"), routing, attachments=attachments,
                )

    # 3. Telegram default — fall back to first allowed user
    telegram_token = dotenv_env("TELEGRAM_BOT_TOKEN")
    allowed = [s.strip() for s in dotenv_env("TELEGRAM_ALLOWED_USERS").split(",") if s.strip()]
    telegram_chat_id = allowed[0] if allowed else None

    if telegram_token and telegram_chat_id:
        return await _send(
            "telegram", session, message, telegram_chat_id, attachments=attachments,
        )

    raise NoNotificationTarget(
        "no notification target — set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS "
        f"in {WOLTS_DIR}/.env, or connect a chat with the /telegram skill"
    )
