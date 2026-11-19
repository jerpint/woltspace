"""Provider transport and formatting. Senders never write lodge state."""

import logging
from pathlib import Path

from .config import DEN_REPLY_FOOTER
from .notification_types import Attachment, OutboundMessage, SendContext, Send

logger = logging.getLogger(__name__)


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


async def _via_connector(method: str, chat_id: str, text: str,
                         attachment: Attachment | None = None) -> dict:
    """Have the Telegram connector send, over its socket; the lodge holds no bot token.

    A refusal (Telegram answered, and not ok) is TelegramRefused; anything
    else, including an answer that may have arrived, is a RuntimeError, so
    nothing is sent twice.
    """
    import connector_broker

    broker = connector_broker.current()
    if broker is None:
        raise RuntimeError("telegram is not connected to the lodge")
    extra = {}
    if attachment is not None:
        extra = dict(path=str(attachment.path), filename=attachment.filename,
                     content_type=attachment.content_type)
    try:
        return await broker.send("telegram", {"chat_id": str(chat_id)}, method, text, **extra)
    except connector_broker.SendFailed as exc:
        if exc.status == "refused":
            raise TelegramRefused(str(exc)) from None
        raise RuntimeError(f"telegram {exc.status}: {exc}") from None
    except connector_broker.ConnectorUnavailable as exc:
        raise RuntimeError(str(exc)) from None


async def telegram_send_document(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload one file as a document: it arrives byte-for-byte, whatever its type."""
    return await _via_connector("document", chat_id, caption, attachment)


async def telegram_send_voice(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload audio Telegram shows as a voice note: Ogg Opus, MP3 or M4A."""
    return await _via_connector("voice", chat_id, caption, attachment)


async def telegram_send_audio(
    token: str, chat_id: str, attachment: Attachment, caption: str
) -> dict:
    """Upload audio Telegram shows in its music player: MP3 or M4A."""
    return await _via_connector("audio", chat_id, caption, attachment)


async def telegram_send(token: str, chat_id: str, text: str) -> dict:
    return await _via_connector("message", chat_id, text)


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
        # The token stays with the connector; the transports keep their old shape.
        token, chat_id = context.secrets.get("TELEGRAM_BOT_TOKEN", ""), destination["chat_id"]
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
