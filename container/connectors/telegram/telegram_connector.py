"""Telegram, as a connector the lodge ships (RFC #617).

This process speaks Telegram and nothing of the lodge but its socket
(connector_client). It verifies who is writing, applies Telegram's own rules
(groups, mentions, replies to the bot), downloads media into its own data
dir, and hands each owner event to the lodge. The lodge decides what happens
and answers with neutral replies, which this process renders: Telegram
Markdown and inline keyboards. Notifications from wolts come back the same way,
as `send` frames.

Environment (set by the lodge when it starts this program):
  TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS
  WOLTSPACE_CONNECTOR_SOCKET, WOLTSPACE_CONNECTOR_TOKEN
  WOLTSPACE_CONNECTOR_DATA     where uploads go (the lodge reads them from there)
  WOLTSPACE_CONNECTOR_OUTBOX   the only directory files are sent from
"""

import asyncio
import collections
import logging
import os
import random
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Update
from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

import connector_client
import connector_protocol as wire
from connector_contract import Reply

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("telegram_connector")

NAME = "telegram"
DATA_DIR = Path(os.environ.get("WOLTSPACE_CONNECTOR_DATA")
                or Path(os.environ.get("WOLTSPACE_WOLTS_DIR", "/workspace/wolts")) / ".space" / NAME)
UPLOADS_DIR = DATA_DIR / "uploads"

# The link to the lodge: a connector_client.Client, or a stand-in in tests.
CLIENT = None
# The update each event came from, so the lodge's answers land in the right
# chat (and a selection's ack and edit on its button). Bounded.
PENDING: "collections.OrderedDict[str, Update]" = collections.OrderedDict()
MAX_PENDING = 1000

DOG_VOICE_ACKS = [
    "🐶 *ear perks up*",
    "🐶 *listens intently*",
    "🐶 *head tilt*",
    "🐶 ...",
]


# --- who may talk ---------------------------------------------------------------

ALLOWED_USERS: set[int] = set()


def load_allowed_users():
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    for uid in raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))
    if not ALLOWED_USERS:
        logger.warning("TELEGRAM_ALLOWED_USERS is empty — bot will reject all messages. "
                       "Set it to a comma-separated list of Telegram user IDs.")


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in ALLOWED_USERS


# --- sending to Telegram ------------------------------------------------------------

async def _reply(update: Update, text: str, **kwargs):
    """reply_text with a single retry on TimedOut."""
    try:
        return await update.message.reply_text(text, **kwargs)
    except TimedOut:
        logger.warning("reply_text timed out, retrying once...")
        await asyncio.sleep(1)
        return await update.message.reply_text(text, **kwargs)


def _telegram_markdown(text: str) -> str:
    """Light markdown to Telegram's legacy Markdown: **bold** is *bold*."""
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _keyboard(choices) -> InlineKeyboardMarkup | None:
    if not choices:
        return None
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(c.label, url=c.url) if c.url
         else InlineKeyboardButton(c.label, callback_data=c.action)
         for c in row]
        for row in choices
    ])


def _render(reply: Reply) -> tuple[str, dict]:
    text, kwargs = reply.text, {}
    if reply.markdown:
        text = _telegram_markdown(text)
        kwargs["parse_mode"] = "Markdown"
    if reply.choices is not None:
        kwargs["reply_markup"] = _keyboard(reply.choices)
    return text, kwargs


# --- downloads ----------------------------------------------------------------------

# Telegram's file server is sometimes slow. A download that times out once
# usually works on the next try, so media handlers retry before giving up,
# and always tell the person when they do give up (never a silent drop).
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_READ_TIMEOUT = 30.0
DOWNLOAD_CONNECT_TIMEOUT = 10.0


async def _download_bytes(context: ContextTypes.DEFAULT_TYPE, file_id: str, what: str) -> bytes:
    """Fetch a file from Telegram, retrying on timeouts and network errors."""
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            file = await context.bot.get_file(
                file_id, read_timeout=DOWNLOAD_READ_TIMEOUT, connect_timeout=DOWNLOAD_CONNECT_TIMEOUT)
            return bytes(await file.download_as_bytearray(
                read_timeout=DOWNLOAD_READ_TIMEOUT, connect_timeout=DOWNLOAD_CONNECT_TIMEOUT))
        except (TimedOut, NetworkError) as exc:
            last_error = exc
            logger.warning(f"{what} download attempt {attempt}/{DOWNLOAD_ATTEMPTS} failed: {exc}")
            if attempt < DOWNLOAD_ATTEMPTS:
                await asyncio.sleep(attempt)
    raise last_error if last_error else RuntimeError(f"{what} download failed")


async def _say_download_failed(update: Update, what: str) -> None:
    """Best effort: the person must hear that their message did not arrive."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    logger.error(f"{what} lost after {DOWNLOAD_ATTEMPTS} attempts (chat_id={chat_id})")
    try:
        await _reply(update, f"I couldn't get that {what} from Telegram (it kept timing out). Please send it again.")
    except Exception as exc:
        logger.error(f"Could not tell chat_id={chat_id} that the {what} was lost: {exc}")


def _save_upload(file_name: str, data: bytes) -> Path:
    """Save an upload in this connector's data dir, never over another one."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    # Only the base name: a file name from Telegram is anyone's to choose.
    safe = Path(file_name).name.replace("\x00", "") or "file"
    dest = UPLOADS_DIR / f"{ts}_{safe}"
    stem, suffix, counter = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = UPLOADS_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    dest.write_bytes(data)
    return dest


# --- replies to the bot ---------------------------------------------------------------

def _from_this_bot(message, context) -> bool:
    """Whether a message was sent by this bot. Only its own notifications carry a
    footer worth honoring: in a group anyone can paste one into theirs."""
    bot_id = getattr(getattr(context, "bot", None), "id", None)
    author = getattr(getattr(message, "from_user", None), "id", None)
    return isinstance(bot_id, int) and not isinstance(bot_id, bool) and author == bot_id


def _reply_to(reply_to, context) -> dict | None:
    """What the lodge needs of a replied-to message: who wrote it and what it says."""
    if not reply_to:
        return None
    # A file or a voice note carries the footer in its caption; a text message in its text.
    body = next((value for value in (getattr(reply_to, "text", None), getattr(reply_to, "caption", None))
                 if isinstance(value, str) and value), "")
    file_name = None
    # Telegram keeps a file as a document, and as an audio when it is one it plays.
    for part in ("document", "audio"):
        name = getattr(getattr(reply_to, part, None), "file_name", None)
        if isinstance(name, str) and name:
            file_name = name[:512]
            break
    # Judged by type, as the file name is: a stand-in has every attribute.
    voice = isinstance(getattr(getattr(reply_to, "voice", None), "file_unique_id", None), str)
    found = {"from_bot": _from_this_bot(reply_to, context), "text": body[:wire.MAX_TEXT], "voice": voice}
    if file_name:
        found["file_name"] = file_name
    return found


# --- to the lodge ---------------------------------------------------------------------

def _event_id(update: Update) -> str:
    update_id = getattr(update, "update_id", None)
    if isinstance(update_id, int) and not isinstance(update_id, bool):
        return f"tg-{update_id}"
    return f"tg-x{uuid.uuid4().hex}"


def _sender(update: Update) -> str:
    user = update.effective_user
    return str(user.id) if user and user.id is not None else "?"


async def _send_event(update: Update, kind: str, chat_id, body: dict) -> str:
    event_id = _event_id(update)
    # A repeat of an update (Telegram redelivers) keeps the first one's place.
    PENDING.setdefault(event_id, update)
    while len(PENDING) > MAX_PENDING:
        PENDING.popitem(last=False)
    status = await CLIENT.event(kind, {"chat_id": str(chat_id)}, _sender(update), body, event_id)
    if status == "failed":
        try:
            await _reply(update, "The lodge couldn't take that message right now. Please send it again.")
        except Exception:
            pass
    return status


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A text message: Telegram's group and mention rules, then the lodge routes it."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Message from user_id={user_id}")
    if not is_allowed(update):
        return
    chat_id = update.effective_chat.id
    text = update.message.text or ""
    bot_username = context.bot.username or ""
    mention = bool(bot_username) and f"@{bot_username}" in text
    reply_to = update.message.reply_to_message
    replied_to_bot = bool(reply_to and reply_to.from_user and reply_to.from_user.id == context.bot.id)
    if mention:
        text = text.replace(f"@{bot_username}", "").strip()
    await _send_event(update, "message", chat_id, {
        "text": text,
        "group": update.effective_chat.type in ("group", "supergroup"),
        "addressed": mention or replied_to_bot,
        "mention": mention,
        "reply_to": _reply_to(reply_to, context),
    })


async def _attachment(update, context, media: str, file_id: str, file_name: str, mime: str, what: str):
    try:
        data = await _download_bytes(context, file_id, what)
    except Exception:
        await _say_download_failed(update, what)
        return
    saved = _save_upload(file_name, data)
    logger.info(f"Saved {media}: {saved} ({mime}, {len(data)} bytes)")
    await _send_event(update, "attachment", update.effective_chat.id, {
        "media": media, "path": str(saved), "name": file_name, "mime": mime, "size": len(data),
        "caption": update.message.caption if isinstance(update.message.caption, str) else "",
        "reply_to": _reply_to(update.message.reply_to_message, context),
    })


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Voice or audio: acknowledged at once, then downloaded; the lodge transcribes it."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Voice message from user_id={user_id}")
    if not is_allowed(update):
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    try:
        await _reply(update, random.choice(DOG_VOICE_ACKS))
    except Exception as exc:
        logger.warning(f"Voice ack failed (chat_id={update.effective_chat.id}): {exc}")
    await _attachment(update, context, "voice", voice.file_id, f"voice_{voice.file_unique_id}.ogg",
                      "audio/ogg", "voice message")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Photo from user_id={user_id}")
    if not is_allowed(update):
        return
    if update.message.photo:
        photo, mime_type = update.message.photo[-1], "image/jpeg"
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        photo = update.message.document
        mime_type = update.message.document.mime_type or "image/jpeg"
    else:
        return
    ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"
    if ext == "jpeg":
        ext = "jpg"
    await _attachment(update, context, "image", photo.file_id, f"photo_{photo.file_unique_id}.{ext}",
                      mime_type, "image")


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Video from user_id={user_id}")
    if not is_allowed(update):
        return
    msg = update.message
    media = msg.video or msg.animation or msg.video_note
    if not media:
        return
    mime_type = getattr(media, "mime_type", None) or "video/mp4"
    ext = mime_type.split("/")[-1] if "/" in mime_type else "mp4"
    file_name = getattr(media, "file_name", None) or f"video_{media.file_unique_id}.{ext}"
    await _attachment(update, context, "video", media.file_id, file_name, mime_type, "video")


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Document from user_id={user_id}")
    if not is_allowed(update):
        return
    doc = update.message.document
    if not doc:
        return
    await _attachment(update, context, "file", doc.file_id, doc.file_name or f"file_{doc.file_unique_id}",
                      doc.mime_type or "application/octet-stream", "file")


def command_handler(name: str):
    async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """One owner command: the lodge runs it, Telegram says what it answered."""
        if not is_allowed(update):
            return
        await _send_event(update, "command", update.effective_chat.id,
                          {"name": name, "args": [str(a) for a in (context.args or [])][:wire.MAX_ARGS]})
    handle.__name__ = f"handle_{name}"
    return handle


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A button press: the lodge runs its action; its ack and edit come back."""
    if not is_allowed(update):
        return
    query = update.callback_query
    chat_id = query.message.chat.id if query.message else update.effective_chat.id
    await _send_event(update, "selection", chat_id, {"action": query.data or ""})


# --- from the lodge -------------------------------------------------------------------

async def on_say(frame: dict) -> None:
    reply = wire.reply_from_wire(frame["reply"])
    update = PENDING.get(frame.get("event") or "")
    message = getattr(update, "message", None) if update is not None else None
    if frame.get("photo"):
        with open(frame["photo"], "rb") as f:
            if message is not None:
                await message.reply_photo(photo=f, caption=reply.text or None)
            else:
                await APP.bot.send_photo(frame["route"]["chat_id"], photo=f, caption=reply.text or None)
        return
    text, kwargs = _render(reply)
    if message is not None:
        await _reply(update, text, **kwargs)
    else:
        # A selection's update has no message of its own to answer: say it in the chat.
        await APP.bot.send_message(frame["route"]["chat_id"], text, **kwargs)


async def on_edit(frame: dict) -> None:
    update = PENDING.get(frame["event"])
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    text, kwargs = _render(wire.reply_from_wire(frame["reply"]))
    try:
        await query.edit_message_text(text, **kwargs)
    except Exception:
        pass


async def on_ack(frame: dict) -> None:
    update = PENDING.get(frame["event"])
    query = getattr(update, "callback_query", None)
    if query is None:
        return
    ack = wire.ack_from_wire(frame["ack"])
    kwargs = {} if ack.alert is None else {"show_alert": ack.alert}
    if ack.text is None:
        await query.answer(**kwargs)
    else:
        await query.answer(ack.text, **kwargs)


UPLOAD_TIMEOUT = 300.0
# The Bot API's limit for a file a bot uploads. Checked here, at the send: a
# staged file can be replaced or grow after the lodge measured it.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_UPLOADS = {"document": ("send_document", "document"), "voice": ("send_voice", "voice"),
            "audio": ("send_audio", "audio")}


def _outbox_file(path: str) -> Path:
    """A file the lodge staged for sending; nothing else on this machine goes out."""
    root = os.environ.get("WOLTSPACE_CONNECTOR_OUTBOX", "")
    if not root:
        raise PermissionError("no outbox configured: files cannot be sent")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(Path(root).resolve()) or not resolved.is_file():
        raise PermissionError("not a staged file")
    if resolved.stat().st_size > MAX_UPLOAD_BYTES:
        raise PermissionError(f"larger than Telegram's {MAX_UPLOAD_BYTES} byte limit")
    return resolved


async def on_send(frame: dict) -> dict:
    """A notification from a wolt: text, or one file, to a chat."""
    chat_id = frame["route"]["chat_id"]
    try:
        if frame["method"] == "message":
            sent = await APP.bot.send_message(chat_id, frame.get("text", ""))
        else:
            method, field = _UPLOADS[frame["method"]]
            path = _outbox_file(frame["path"])
            with open(path, "rb") as upload:
                sent = await getattr(APP.bot, method)(
                    chat_id, **{field: upload}, filename=frame["filename"],
                    caption=frame.get("text") or None,
                    read_timeout=UPLOAD_TIMEOUT, write_timeout=UPLOAD_TIMEOUT)
    except PermissionError as exc:
        return {"status": "refused", "error": str(exc)}
    except TimedOut as exc:
        # It may have arrived: the lodge must not send it twice.
        return {"status": "ambiguous", "error": str(exc) or "timed out"}
    except BadRequest as exc:
        # Telegram answered, and the answer was not ok. (BadRequest is a
        # NetworkError in python-telegram-bot: it must be caught first.)
        return {"status": "refused", "error": str(exc)}
    except NetworkError as exc:
        return {"status": "failed", "error": str(exc)}
    except TelegramError as exc:
        # Forbidden and the rest: answered, and not ok.
        return {"status": "refused", "error": str(exc)}
    return {"status": "delivered", "message_id": str(getattr(sent, "message_id", "") or "")}


# --- run ----------------------------------------------------------------------------------

APP = None


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log and swallow TimedOut, log others."""
    err = context.error
    if isinstance(err, TimedOut):
        chat = getattr(update, "effective_chat", None)
        logger.warning(f"Telegram TimedOut (swallowed, chat_id={getattr(chat, 'id', None)}): {err}")
        return
    logger.error(f"Unhandled error: {err}", exc_info=err)


def build_app(token: str, commands: list):
    app = ApplicationBuilder().token(token).build()
    app.add_error_handler(_error_handler)
    for name, _description in commands:
        app.add_handler(CommandHandler(name, command_handler(name)))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(
        filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(
        filters.Document.ALL & ~filters.Document.IMAGE & ~filters.Document.VIDEO, handle_document))
    return app


async def _register_commands(app, commands: list):
    """Publish the slash-command menu so Telegram's '/' autocomplete is populated."""
    try:
        await app.bot.set_my_commands([BotCommand(n, d) for n, d in commands])
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info(f"registered {len(commands)} bot commands + commands menu button")
    except Exception as e:
        logger.warning(f"failed to register bot commands: {e}")


async def _connect(client) -> dict:
    delay = 0.5
    while True:
        try:
            return await client.connect()
        except OSError as exc:
            logger.warning(f"lodge not reachable ({exc}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def main() -> None:
    global CLIENT, APP
    load_allowed_users()
    CLIENT = connector_client.Client(NAME, on_say=on_say, on_edit=on_edit, on_ack=on_ack, on_send=on_send)
    welcome = await _connect(CLIENT)
    commands = [tuple(c) for c in welcome["commands"]]
    APP = build_app(os.environ["TELEGRAM_BOT_TOKEN"], commands)
    logger.info("telegram connector starting")
    async with APP:
        await _register_commands(APP, commands)
        await APP.start()
        await APP.updater.start_polling()
        try:
            await CLIENT.run_forever()
        finally:
            await APP.updater.stop()
            await APP.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except connector_client.Refused as exc:
        # A newer launch holds the socket: this one has nothing left to do.
        logger.warning(f"the lodge closed this connector: {exc}")
