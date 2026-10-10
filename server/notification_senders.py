"""Provider transport and formatting. Senders never write lodge state."""

import logging

import httpx

from .config import DEN_REPLY_FOOTER
from .notification_types import Attachment, OutboundMessage, SendContext, Send

logger = logging.getLogger(__name__)

# A file upload can take minutes; httpx's 5-second default suits small JSON calls.
UPLOAD_TIMEOUT = httpx.Timeout(30.0, read=120.0, write=300.0)

# The Bot API's limit for a file a bot uploads.
TELEGRAM_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
# A caption's limit. Telegram counts UTF-16 code units, so an emoji is two.
TELEGRAM_CAPTION_LIMIT = 1024


async def telegram_send_document(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload one file as a document: it arrives byte-for-byte, whatever its type."""
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    with open(attachment.path, "rb") as document:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data=fields,
                files={"document": (attachment.filename, document, attachment.content_type)},
                timeout=UPLOAD_TIMEOUT,
            )
            try:
                data = resp.json()
            except ValueError:
                # A proxy in front of the API answers an oversized upload in HTML.
                raise RuntimeError(f"telegram answered HTTP {resp.status_code}") from None
            if not data.get("ok"):
                raise RuntimeError(data.get("description", "telegram error"))
            return data


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


SLACK_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
SLACK_MISSING_FILES_SCOPE = (
    "Slack app lacks the files:write scope: add it to the app manifest and "
    "reinstall the app. Text notifications are unaffected."
)


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


def _slack_upload_data(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # An error page from a proxy, say: the status is all there is to report.
        raise RuntimeError(f"slack answered HTTP {response.status_code}")
    if not data.get("ok"):
        if data.get("error") == "missing_scope":
            needed = data.get("needed")
            if isinstance(needed, str) and needed and "files:write" not in needed:
                # Slack says which scope it wanted: do not send the owner after another.
                raise RuntimeError(
                    f"Slack app lacks the {needed} scope: add it to the app manifest "
                    "and reinstall the app."
                )
            raise RuntimeError(SLACK_MISSING_FILES_SCOPE)
        raise RuntimeError(data.get("error", "slack error"))
    return data


async def _file_chunks(handle, size: int = 64 * 1024):
    # An AsyncClient refuses a plain file object as the body; it needs an async stream.
    while chunk := handle.read(size):
        yield chunk


async def slack_upload_file(
    token: str, channel: str, thread_ts: str | None, attachment: Attachment, comment: str,
) -> dict:
    """Slack's external upload: ask for a URL, send the bytes there, then share the file."""
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as client:
        ticket = _slack_upload_data(await client.post(
            "https://slack.com/api/files.getUploadURLExternal",
            data={"filename": attachment.filename, "length": attachment.size},
            headers=headers,
            timeout=UPLOAD_TIMEOUT,
        ))
        upload_url, file_id = ticket.get("upload_url"), ticket.get("file_id")
        if not upload_url or not file_id:
            raise RuntimeError("slack gave no upload URL")
        with open(attachment.path, "rb") as handle:
            # The explicit length keeps httpx from sending the stream chunked.
            uploaded = await client.post(
                upload_url,
                content=_file_chunks(handle),
                headers={"Content-Length": str(attachment.size)},
                timeout=UPLOAD_TIMEOUT,
            )
        if uploaded.status_code != 200:
            raise RuntimeError(f"slack upload failed: HTTP {uploaded.status_code}")
        payload = {
            "files": [{"id": file_id, "title": attachment.filename}],
            "channel_id": channel,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        if comment:
            payload["initial_comment"] = comment
        return _slack_upload_data(await client.post(
            "https://slack.com/api/files.completeUploadExternal",
            json=payload,
            headers=headers,
            timeout=UPLOAD_TIMEOUT,
        ))


def telegram_sender(transport, send_document=None) -> Send:
    """Temporary transport injection preserves notify's existing patch points."""
    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        token, chat_id = context.secrets["TELEGRAM_BOT_TOKEN"], destination["chat_id"]
        footer = ""
        if message.session_link:
            footer = f"\n\n---{DEN_REPLY_FOOTER}\n{message.session_link}"
        text = message.text + footer
        if not message.attachments:
            await transport(token, chat_id, text)
            return
        if send_document is None:
            raise RuntimeError("telegram sender has no document transport")
        if len(message.attachments) > 1:
            raise RuntimeError("telegram sender carries one file per message")
        attachment = message.attachments[0]
        if len(text.encode("utf-16-le")) // 2 <= TELEGRAM_CAPTION_LIMIT:
            await send_document(token, chat_id, attachment, text)
            return
        # Too long for a caption: the text goes first, exactly as a text
        # notification, and the file carries the footer so either can be replied to.
        await transport(token, chat_id, text)
        try:
            await send_document(token, chat_id, attachment, footer.strip())
        except Exception as exc:
            raise RuntimeError(
                "the text was delivered but the file was not: "
                f"{str(exc) or type(exc).__name__}"
            ) from exc
    return send


def slack_sender(transport, set_status, upload_file=None) -> Send:
    """Temporary transport injection preserves notify's existing patch points."""
    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        token = context.secrets["SLACK_BOT_TOKEN"]
        channel = destination["channel"]
        thread_ts = destination.get("thread_ts")
        text = message.text
        if message.session_link:
            text += f"\n\n<{message.session_link}|Open session>"

        async def deliver() -> None:
            if not message.attachments:
                await transport(token, channel, thread_ts, text)
            elif upload_file is None:
                raise RuntimeError("slack sender has no upload transport")
            elif len(message.attachments) > 1:
                raise RuntimeError("slack sender carries one file per message")
            else:
                await upload_file(token, channel, thread_ts, message.attachments[0], text)

        if context.route_state.get("slack_progress_mode") == "agent" and thread_ts:
            try:
                await deliver()
            finally:
                try:
                    await set_status(token, channel, thread_ts, "active")
                except Exception as exc:
                    logger.warning("Could not clear Slack native agent status: %s", exc)
                context.updates["slack_progress_mode"] = ""
                context.updates["slack_progress_ts"] = ""
        else:
            await deliver()
    return send
