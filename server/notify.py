"""Notification system — send messages to Telegram/Slack."""

import json
from pathlib import Path

import httpx

from .config import (
    STATE_DIR,
    SPACE_PLATFORM_DIR,
    WOLTS_DIR,
    DEN_REPLY_FOOTER,
    get_env,
)
from .state import sanitize_session


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


async def _send_telegram(session: str, message: str, chat_id: str) -> dict:
    """Send a notification via Telegram with den-reply footer."""
    token = get_env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    tunnel_url = ""
    tunnel_file = SPACE_PLATFORM_DIR / "tunnel-url"
    try:
        tunnel_url = tunnel_file.read_text().strip()
    except Exception:
        pass

    footer = ""
    if session:
        session_url = f"{tunnel_url}/tui?session={session}" if tunnel_url else f"session={session}"
        footer = f"\n\n---{DEN_REPLY_FOOTER}\n{session_url}"

    await telegram_send(token, chat_id, message + footer)
    append_chat_history("telegram", chat_id, message)
    return {"adapter": "telegram", "chat_id": chat_id}


async def _send_slack(message: str, routing: dict) -> dict:
    """Send a notification via Slack to the originating thread."""
    token = get_env("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    channel = routing.get("chat_id") or get_env("SLACK_NOTIFY_CHANNEL")
    if not channel:
        raise RuntimeError("no slack channel in routing and SLACK_NOTIFY_CHANNEL not set")
    await slack_send(token, channel, routing.get("thread_ts"), message)
    append_chat_history("slack", channel, message)
    return {"adapter": "slack", "channel": channel}


async def send_notification(session: str, message: str) -> dict:
    routing = read_session_registry(session)

    # Respect the session's routing adapter — Slack sessions go to Slack,
    # Telegram sessions go to Telegram. Only fall back to Telegram default
    # when there's no routing or the adapter is unknown.
    adapter = routing.get("adapter") if routing else None

    if adapter == "slack":
        return await _send_slack(message, routing)

    if adapter == "telegram":
        chat_id = routing.get("chat_id")
        if chat_id:
            return await _send_telegram(session, message, str(chat_id))

    # No routing or unknown adapter — fall back to Telegram default
    telegram_token = get_env("TELEGRAM_BOT_TOKEN")
    allowed = [s.strip() for s in get_env("TELEGRAM_ALLOWED_USERS").split(",") if s.strip()]
    telegram_chat_id = allowed[0] if allowed else None

    if telegram_token and telegram_chat_id:
        return await _send_telegram(session, message, telegram_chat_id)

    raise RuntimeError("no notification target — set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS")
