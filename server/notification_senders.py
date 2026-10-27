"""Provider transport and formatting. Senders never write lodge state."""

import asyncio
import logging
import re
from pathlib import Path

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
# How a file travels is read from its name: the lists Python keeps of types
# differ by platform. Any other name goes out with sendDocument.
_TELEGRAM_VOICE_SUFFIXES = (".ogg", ".oga", ".opus")   # sendVoice, field "voice"
_TELEGRAM_AUDIO_SUFFIXES = (".mp3", ".m4a")            # sendAudio, field "audio"


class TelegramRefused(RuntimeError):
    """Telegram answered an upload, and the answer was not ok.

    Its own type so the sender can tell a file Telegram will not take this way
    from a request that may never have arrived.
    """


async def _telegram_upload(
    method: str, field: str, token: str, chat_id: str, attachment: Attachment, caption: str,
) -> dict:
    fields = {"chat_id": chat_id}
    if caption:
        fields["caption"] = caption
    with open(attachment.path, "rb") as upload:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{token}/{method}",
                data=fields,
                files={field: (attachment.filename, upload, attachment.content_type)},
                timeout=UPLOAD_TIMEOUT,
            )
            try:
                data = resp.json()
            except ValueError:
                # A proxy in front of the API answers an oversized upload in HTML.
                raise TelegramRefused(f"telegram answered HTTP {resp.status_code}") from None
            if not data.get("ok"):
                raise TelegramRefused(data.get("description", "telegram error"))
            return data


async def telegram_send_document(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload one file as a document: it arrives byte-for-byte, whatever its type."""
    return await _telegram_upload("sendDocument", "document", token, chat_id, attachment, caption)


async def telegram_send_voice(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload audio Telegram shows as a voice note: Ogg Opus, MP3 or M4A."""
    return await _telegram_upload("sendVoice", "voice", token, chat_id, attachment, caption)


async def telegram_send_audio(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload audio Telegram shows in its music player: MP3 or M4A."""
    return await _telegram_upload("sendAudio", "audio", token, chat_id, attachment, caption)


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
# Slack shares a file into a conversation id only. A member id, which
# chat.postMessage accepts for text, is refused after the bytes are uploaded.
SLACK_FILE_DESTINATION = re.compile(r"[CGDZ][A-Z0-9]{8,}")
SLACK_NOT_A_FILE_DESTINATION = (
    "Slack can attach a file only to a channel or DM id (C..., G..., D...), "
    "not to a member id; send the file from the Slack thread, or send text only"
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


async def whatsapp_send(token: str, participant: str, text: str) -> dict:
    """Send text through the official WhatsApp Agent Platform API."""
    if len(text) > 4096:
        raise RuntimeError("WhatsApp text exceeds the 4096 character limit")
    from woltspace.whatsapp_rate_limit import async_request_slot
    from .config import SPACE_DIR
    for attempt in range(3):
        async with async_request_slot(SPACE_DIR / "whatsapp", token, 12, "send"):
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    "https://api.whatsapp.com/agent/v1/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messaging_product": "whatsapp", "to": participant,
                          "type": "text", "text": {"body": text}},
                )
        if response.status_code == 429:
            if attempt == 2:
                raise RuntimeError("WhatsApp rate limit reached after bounded retries")
            try:
                retry_after = max(1, min(60, int(response.headers.get("Retry-After", "5"))))
            except (TypeError, ValueError):
                retry_after = 5
            await asyncio.sleep(retry_after)
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"WhatsApp Agent API answered HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            return {}


def whatsapp_sender(transport) -> Send:
    """WhatsApp Agent Platform supports only text in this initial sender."""
    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        if message.attachments:
            raise RuntimeError("WhatsApp file and media delivery is not supported yet")
        text = message.text
        if message.session_link:
            text += f"\n\nOpen session: {message.session_link}"
        await transport(context.secrets["WHATSAPP_AGENT_TOKEN"], destination["participant"], text)
    return send


def _slack_upload_data(response: httpx.Response, method: str) -> dict:
    try:
        data = response.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        # An error page from a proxy, say: the status is all there is to report.
        raise RuntimeError(f"slack {method} answered HTTP {response.status_code}")
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
        # Slack's own explanation, when it gives one, names the argument it refused.
        metadata = data.get("response_metadata")
        messages = metadata.get("messages") if isinstance(metadata, dict) else None
        explained = [m for m in messages if isinstance(m, str)] if isinstance(messages, list) else []
        detail = f" ({'; '.join(explained)})" if explained else ""
        raise RuntimeError(f"slack {method}: {data.get('error', 'unknown error')}{detail}")
    return data


async def _file_chunks(handle, size: int = 64 * 1024):
    # An AsyncClient refuses a plain file object as the body; it needs an async stream.
    while chunk := handle.read(size):
        yield chunk


async def slack_upload_file(
    token: str, channel: str, thread_ts: str | None, attachment: Attachment, comment: str,
) -> dict:
    """Slack's external upload: ask for a URL, send the bytes there, then share the file."""
    if not SLACK_FILE_DESTINATION.fullmatch(channel or ""):
        raise RuntimeError(SLACK_NOT_A_FILE_DESTINATION)
    async with httpx.AsyncClient() as client:
        async def call(method: str, **body) -> dict:
            return _slack_upload_data(await client.post(
                f"https://slack.com/api/{method}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=UPLOAD_TIMEOUT,
                **body,
            ), method)

        ticket = await call(
            "files.getUploadURLExternal",
            data={"filename": attachment.filename, "length": attachment.size},
        )
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
        return await call("files.completeUploadExternal", json=payload)


def telegram_sender(transport, send_document=None, send_voice=None, send_audio=None) -> Send:
    """Temporary transport injection preserves notify's existing patch points."""
    async def _by_name(token: str, chat_id: str, attachment: Attachment, caption: str) -> None:
        """Send a file the way its name asks for, and as a document if Telegram refuses that."""
        suffix = Path(attachment.filename).suffix.lower()
        if suffix in _TELEGRAM_VOICE_SUFFIXES and send_voice is not None:
            method, upload = "sendVoice", send_voice
        elif suffix in _TELEGRAM_AUDIO_SUFFIXES and send_audio is not None:
            method, upload = "sendAudio", send_audio
        else:
            await send_document(token, chat_id, attachment, caption)
            return
        try:
            await upload(token, chat_id, attachment, caption)
        except TelegramRefused as exc:
            # An .ogg that is Vorbis and not Opus, say. The file still arrives. Any
            # other failure may have reached Telegram, so it is not sent twice.
            logger.warning("Telegram refused %s (%s); sending the file as a document", method, exc)
            await send_document(token, chat_id, attachment, caption)

    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        token, chat_id = context.secrets["TELEGRAM_BOT_TOKEN"], destination["chat_id"]
        footer = ""
        if message.session_link:
            footer = f"\n\n---{DEN_REPLY_FOOTER}\n{message.session_link}"
        text = message.text + footer
        if not message.attachments:
            await transport(token, chat_id, text)
            return
        attachment = message.attachments[0]
        if attachment.kind == "voice":
            # A voice note was asked for: if it cannot be one, the caller is told.
            upload, what = send_voice, "voice note"
            if upload is None:
                raise RuntimeError("telegram sender has no voice transport")
        else:
            upload, what = _by_name, "file"
            if send_document is None:
                raise RuntimeError("telegram sender has no document transport")
        if len(message.attachments) > 1:
            raise RuntimeError("telegram sender carries one file per message")
        if len(text.encode("utf-16-le")) // 2 <= TELEGRAM_CAPTION_LIMIT:
            await upload(token, chat_id, attachment, text)
            return
        # Too long for a caption: the text goes first, exactly as a text
        # notification, and the file carries the footer so either can be replied to.
        await transport(token, chat_id, text)
        try:
            await upload(token, chat_id, attachment, footer.strip())
        except Exception as exc:
            raise RuntimeError(
                f"the text was delivered but the {what} was not: "
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
