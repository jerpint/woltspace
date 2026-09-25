"""Notification system — send messages to Telegram/Slack.

Routing is explicit: the caller (rodent) tells us where to send via adapter flags.
Falls back to session registry lookup, then Telegram default.
"""

import json
import logging
import urllib.parse
from pathlib import Path

import httpx
from sessions import SessionRegistry

from .config import (
    STATE_DIR,
    SPACE_PLATFORM_DIR,
    WOLTS_DIR,
    DEN_REPLY_FOOTER,
    dotenv_env,
)
from .state import sanitize_session

logger = logging.getLogger(__name__)


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


async def telegram_send(token: str, chat_id: str, text: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "telegram error"))
        return data


async def slack_send(token: str, channel: str, thread_ts: str | None, text: str) -> dict:
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/chat.postMessage",
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "slack error"))
        return data


async def slack_finish_progress(
    token: str, channel: str, message_ts: str, mode: str, text: str
) -> dict:
    async with httpx.AsyncClient() as client:
        headers = {"Authorization": f"Bearer {token}"}
        if mode == "stream":
            stopped = await client.post(
                "https://slack.com/api/chat.stopStream",
                json={"channel": channel, "ts": message_ts},
                headers=headers,
            )
            stopped_data = stopped.json()
            if not stopped_data.get("ok"):
                raise RuntimeError(stopped_data.get("error", "chat.stopStream error"))
        # stopStream's markdown_text is an additional final chunk. Always use
        # chat.update after stopping so the temporary `gnawing…` bytes are
        # replaced, not retained above the persisted final answer.
        updated = await client.post(
            "https://slack.com/api/chat.update",
            json={"channel": channel, "ts": message_ts, "text": text},
            headers=headers,
        )
        updated_data = updated.json()
        if not updated_data.get("ok"):
            raise RuntimeError(updated_data.get("error", "chat.update error"))
        return updated_data


async def slack_delete(token: str, channel: str, message_ts: str) -> None:
    async with httpx.AsyncClient() as client:
        await client.post(
            "https://slack.com/api/chat.delete",
            json={"channel": channel, "ts": message_ts},
            headers={"Authorization": f"Bearer {token}"},
        )


async def slack_set_agent_status(
    token: str, channel: str, thread_ts: str, status: str
) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://slack.com/api/agents.sessions.setStatus",
            json={
                "channel_id": channel,
                "thread_ts": thread_ts,
                "status": status,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("error", "agents.sessions.setStatus error"))
        return data


async def _send_telegram(session: str, message: str, chat_id: str) -> dict:
    """Send a notification via Telegram with den-reply footer."""
    token = dotenv_env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    tunnel_url = ""
    try:
        import json
        state = json.loads((SPACE_PLATFORM_DIR / "tunnel.json").read_text())
        tunnel_url = state.get("url", "").strip()
    except Exception:
        pass

    footer = ""
    if session:
        session_url = f"{tunnel_url}/tui?session={session}" if tunnel_url else f"session={session}"
        footer = f"\n\n---{DEN_REPLY_FOOTER}\n{session_url}"

    await telegram_send(token, chat_id, message + footer)
    append_chat_history("telegram", chat_id, message)
    return {"adapter": "telegram", "chat_id": chat_id}


async def _send_slack(
    message: str,
    channel: str,
    thread_ts: str | None = None,
    *,
    session: str = "",
    routing: dict | None = None,
) -> dict:
    """Send a notification via Slack to a specific channel/thread."""
    token = dotenv_env("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    if not channel:
        channel = dotenv_env("SLACK_NOTIFY_CHANNEL")
    if not channel:
        raise RuntimeError("no slack channel provided and SLACK_NOTIFY_CHANNEL not set")
    progress_ts = (routing or {}).get("slack_progress_ts", "")
    progress_mode = (routing or {}).get("slack_progress_mode", "")
    session_link = _slack_session_link(session, routing)
    final_message = message
    if session_link:
        final_message += f"\n\n<{session_link}|Open session>"
    if progress_mode == "agent" and thread_ts:
        try:
            await slack_send(token, channel, thread_ts, final_message)
        finally:
            try:
                await slack_set_agent_status(
                    token, channel, thread_ts, "active"
                )
            except Exception as exc:
                logger.warning("Could not clear Slack native agent status: %s", exc)
            if session and routing:
                SessionRegistry(WOLTS_DIR).update(
                    session,
                    wolt=routing.get("wolt", ""),
                    slack_progress_mode="",
                    slack_progress_ts="",
                )
    elif progress_ts and progress_mode in {"stream", "message"}:
        try:
            await slack_finish_progress(
                token, channel, progress_ts, progress_mode, final_message
            )
        except Exception:
            await slack_delete(token, channel, progress_ts)
            raise
        finally:
            if session and routing:
                SessionRegistry(WOLTS_DIR).update(
                    session,
                    wolt=routing.get("wolt", ""),
                    slack_progress_mode="",
                    slack_progress_ts="",
                )
    else:
        await slack_send(token, channel, thread_ts, final_message)
    append_chat_history("slack", channel, message)
    return {"adapter": "slack", "channel": channel}


async def send_notification(session: str, message: str, explicit: dict | None = None) -> dict:
    """Send a notification. Explicit routing takes priority over session lookup.

    explicit dict can contain:
      {"adapter": "slack", "channel": "C123", "thread_ts": "1234.5678"}
      {"adapter": "telegram", "chat_id": "98765"}
    """

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
            return await _send_slack(
                message, channel, thread_ts,
                session=session if exact else "",
                routing=routing if exact else None,
            )
        if adapter == "telegram":
            chat_id = explicit.get("chat_id", "")
            if chat_id:
                return await _send_telegram(session, message, str(chat_id))

    # 2. Session registry lookup — find routing from session metadata
    if session:
        if routing:
            adapter = routing.get("adapter")
            if adapter == "slack":
                return await _send_slack(
                    message,
                    routing.get("chat_id", ""),
                    routing.get("thread_ts"),
                    session=session,
                    routing=routing,
                )
            if adapter == "telegram":
                chat_id = routing.get("chat_id")
                if chat_id:
                    return await _send_telegram(session, message, str(chat_id))

    # 3. Telegram default — fall back to first allowed user
    telegram_token = dotenv_env("TELEGRAM_BOT_TOKEN")
    allowed = [s.strip() for s in dotenv_env("TELEGRAM_ALLOWED_USERS").split(",") if s.strip()]
    telegram_chat_id = allowed[0] if allowed else None

    if telegram_token and telegram_chat_id:
        return await _send_telegram(session, message, telegram_chat_id)

    raise NoNotificationTarget(
        "no notification target — set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS "
        f"in {WOLTS_DIR}/.env, or connect a chat with the /telegram skill"
    )
