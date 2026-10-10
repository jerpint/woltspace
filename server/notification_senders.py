"""Provider transport and formatting. Senders never write lodge state."""

import logging

import httpx

from .config import DEN_REPLY_FOOTER
from .notification_types import OutboundMessage, SendContext, Send

logger = logging.getLogger(__name__)

# A file upload can take minutes; httpx's 5-second default suits small JSON calls.
UPLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0, write=300.0)


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


def telegram_sender(transport) -> Send:
    """Temporary transport injection preserves notify's existing patch points."""
    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        footer = ""
        if message.session_link:
            footer = f"\n\n---{DEN_REPLY_FOOTER}\n{message.session_link}"
        await transport(
            context.secrets["TELEGRAM_BOT_TOKEN"], destination["chat_id"],
            message.text + footer,
        )
    return send


def slack_sender(transport, set_status) -> Send:
    """Temporary transport injection preserves notify's existing patch points."""
    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        token = context.secrets["SLACK_BOT_TOKEN"]
        channel = destination["channel"]
        thread_ts = destination.get("thread_ts")
        text = message.text
        if message.session_link:
            text += f"\n\n<{message.session_link}|Open session>"
        if context.route_state.get("slack_progress_mode") == "agent" and thread_ts:
            try:
                await transport(token, channel, thread_ts, text)
            finally:
                try:
                    await set_status(token, channel, thread_ts, "active")
                except Exception as exc:
                    logger.warning("Could not clear Slack native agent status: %s", exc)
                context.updates["slack_progress_mode"] = ""
                context.updates["slack_progress_ts"] = ""
        else:
            await transport(token, channel, thread_ts, text)
    return send
