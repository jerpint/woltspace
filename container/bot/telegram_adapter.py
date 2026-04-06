"""
Telegram adapter — thin layer over core.
Platform default. Wolt can override by placing wolt/bot/telegram_adapter.py in their repo.
"""

import os
import json
import base64
import logging
import asyncio
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import re
import sys
from bot.core import get_response, transcribe_audio, list_sessions, kill_session, get_tunnel_url, switch_wolt, list_wolts, _bot_log, build_ack_text, _sanitize_history, message_session
from wolts import get_active_creature

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from paths import wolt_state_dir, wolt_chat_dir, wolt_uploads_dir

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
_WOLT_NAME = os.environ.get("WOLT_NAME", WOLT_DIR.name)
_WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", str(WOLT_DIR.parent)))
STATE_DIR = wolt_state_dir(_WOLT_NAME, _WOLTS_DIR)
CHAT_DIR = wolt_chat_dir(_WOLT_NAME, _WOLTS_DIR)


def _dog_name() -> str:
    """Get the dog's display name — from dog-wolt if available, else WOLT_NAME."""
    name = get_active_creature("dog")
    return name or os.environ.get("WOLT_NAME", "wolt")

ALLOWED_USERS: set[int] = set()
chat_histories: dict[int, list] = defaultdict(list)
MAX_HISTORY = 20

# Immediate ack messages — sent before processing starts so the user knows we heard them
DOG_ACK_MESSAGES = [
    "🐶 *tail wags vigorously*",
    "🐶 *perks ears*",
    "🐶 woof woof",
    "🐶 ...",
    "🐶 *sniffs curiously*",
    "🐶 *head tilt*",
    "🐶 *tippy taps*",
    "🐶 *play bows*",
    "🐶 *ears forward*",
    "🐶 *full body wiggle*",
]

DOG_VOICE_ACKS = [
    "🐶 *ear perks up*",
    "🐶 *listens intently*",
    "🐶 *head tilt*",
    "🐶 ...",
]


async def _dog_ack(update: Update):
    """Fire an immediate acknowledgment so the user knows the message landed."""
    try:
        await update.message.reply_text(random.choice(DOG_ACK_MESSAGES))
    except Exception:
        pass  # never let ack failure block the real work

# Must match the sentinel in server.js — used to detect replies-to-den
DEN_REPLY_FOOTER = "\n↩️ reply to this message to talk to this session directly"
DEN_SESSION_RE = re.compile(r"session=([a-z0-9-]+)")


def _is_den_reply(update: Update) -> str | None:
    """If this message is a reply to a den (notify) message, return the session name."""
    reply = update.message.reply_to_message
    if not reply or not reply.text:
        return None
    if DEN_REPLY_FOOTER.strip() not in reply.text:
        return None
    match = DEN_SESSION_RE.search(reply.text)
    return match.group(1) if match else None


def _chat_file(chat_id: int) -> Path:
    return CHAT_DIR / f"{chat_id}.jsonl"


def _load_history(chat_id: int) -> list:
    """Load last N message pairs from disk, sanitized."""
    path = _chat_file(chat_id)
    if not path.exists():
        return []
    lines = path.read_text().strip().split("\n")
    messages = []
    for line in lines[-MAX_HISTORY * 2:]:
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return _sanitize_history(messages)


def _append_history(chat_id: int, role: str, content: str):
    """Append a single message to disk."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_chat_file(chat_id), "a") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def _append_message(chat_id: int, msg: dict):
    """Append any message dict to disk (e.g. tool call entries with full structure)."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_chat_file(chat_id), "a") as f:
        f.write(json.dumps({**msg, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def load_allowed_users():
    """Load allowed user IDs from env. Comma-separated."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    for uid in raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))


def format_response(result: dict) -> str:
    """Format a core response dict for Telegram (text only)."""
    if result["type"] == "session":
        # Use nw's crafted response if available; fall back to static ack
        if result.get("text"):
            text = result["text"]
        else:
            s = result["session"]
            text = build_ack_text(s.get("url"), s.get("name"), "telegram", creature=s.get("creature"))
    elif result["type"] == "image":
        text = result.get("text", "") or result.get("caption", "") or result.get("filename", "image")
    else:
        text = result["text"]
    return f"🐶 {_dog_name()}: {text}"


CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶"}


def _format_tool_log(tc: dict) -> str:
    """Format a single tool call as a compact log line.

    Format: 🪵 🦝 neowolt — new_session
            https://tunnel/tui?session=neowolt-xxx
    """
    name = tc["tool"]
    creature = tc.get("creature") or tc.get("args", {}).get("creature", "")
    emoji = CREATURE_EMOJIS.get(creature, "")
    wolt = tc.get("args", {}).get("wolt", "")
    recipient = f"{emoji} {wolt}".strip()

    line = f"🪵 {recipient} — {name}" if recipient else f"🪵 {name}"

    # Add session link from tool result
    url = tc.get("url", "")
    if url:
        line += f"\n{url}"

    return line


async def _send_result(update: Update, result: dict):
    """Send a result to the user — handles text, session, and image types.
    Sends deterministic tool call logs before the final response."""
    # Send tool call logs first
    for tc in result.get("tool_calls_log", []):
        try:
            await update.message.reply_text(_format_tool_log(tc))
        except Exception:
            pass  # don't let log failures block the response

    if result["type"] == "image":
        caption = result.get("text") or result.get("caption") or None
        with open(result["path"], "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
    else:
        await update.message.reply_text(format_response(result))


def is_allowed(update: Update) -> bool:
    """Check if the user is whitelisted. No whitelist = open access."""
    if not ALLOWED_USERS:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in ALLOWED_USERS


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming text messages."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Message from user_id={user_id}")
    if not is_allowed(update):
        logger.info(f"Blocked user_id={user_id}")
        return

    # --- Den reply: route directly to Claude Code session, bypass Haiku ---
    den_session = _is_den_reply(update)
    if den_session:
        text = update.message.text or ""
        human_name = "human"
        # Send to session with context about where it came from
        chat_id = update.effective_chat.id
        den_msg = (
            f"[telegram message from {human_name}, chat_id={chat_id}]: {text}\n"
            f"Reply back to them with: notify --telegram {chat_id} \"your message\""
        )
        result = message_session(den_session, den_msg)
        _bot_log("den_reply", {"session": den_session, "text": text[:200], "result": result})
        if result.get("ok"):
            _append_message(chat_id, {
                "role": "user",
                "content": (
                    f"<system>The user replied directly to Claude Code session {den_session}. "
                    f"This bypassed you — context only.</system>\n"
                    f"[user replied to den]: {text}"
                ),
            })
            session_link = result.get("url") or den_session
            if result.get("status") == "revived":
                await update.message.reply_text(f"🪵 session had exited — revived and delivered\n{session_link}")
            else:
                await update.message.reply_text(f"🪵 sent\n{session_link}")
        else:
            error = result.get("error", "unknown error")
            await update.message.reply_text(f"couldn't deliver: {error}")
        return

    # In group chats, only respond when @mentioned or replied to
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        bot_id = context.bot.id
        text = update.message.text or ""
        is_mention = f"@{bot_username}" in text
        is_reply = (update.message.reply_to_message
                    and update.message.reply_to_message.from_user
                    and update.message.reply_to_message.from_user.id == bot_id)
        if not is_mention and not is_reply:
            return
        text = text.replace(f"@{bot_username}", "").strip()
    else:
        text = update.message.text

    chat_id = update.effective_chat.id
    user_message = text
    if update.message.reply_to_message and update.message.reply_to_message.text:
        user_message = f"[replying to: \"{update.message.reply_to_message.text}\"]\n{user_message}"

    # Always reload from disk to pick up out-of-band messages (e.g. notify from sessions)
    chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    routing = {"adapter": "telegram", "chat_id": chat_id}

    # Immediate ack — let the user know we heard them before Haiku starts thinking
    await _dog_ack(update)

    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing)
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    # Store history — history_messages always present, includes tool calls
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    for msg in result["history_messages"]:
        _append_message(chat_id, msg)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await _send_result(update, result)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming voice messages — transcribe and process as text."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Voice message from user_id={user_id}")
    if not is_allowed(update):
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    chat_id = update.effective_chat.id

    await update.message.reply_text(random.choice(DOG_VOICE_ACKS))

    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        text = transcribe_audio(tmp_path)
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        await update.message.reply_text("Couldn't transcribe that voice message.")
        return
    finally:
        # Save audio to uploads, then clean up temp file
        try:
            audio_name = f"voice_{voice.file_unique_id}.ogg"
            _save_upload(audio_name, Path(tmp_path).read_bytes())
        except Exception:
            pass  # don't fail if save fails — transcription already succeeded
        Path(tmp_path).unlink(missing_ok=True)

    logger.info(f"Transcribed: {text[:100]}")

    # If this is a reply to a den notify, route directly to that session
    den_session = _is_den_reply(update)
    if den_session:
        human_name = "human"
        voice_chat_id = update.effective_chat.id
        den_msg = (
            f"[telegram voice from {human_name}, chat_id={voice_chat_id}]: {text}\n"
            f"Reply back to them with: notify --telegram {voice_chat_id} \"your message\""
        )
        result = message_session(den_session, den_msg)
        _bot_log("den_reply_voice", {"session": den_session, "text": text[:200], "result": result})
        if result.get("ok"):
            session_link = result.get("url") or den_session
            if result.get("status") == "revived":
                await update.message.reply_text(f"🪵 session had exited — revived and delivered\n{session_link}")
            else:
                await update.message.reply_text(f"🪵 sent\n{session_link}")
        else:
            error = result.get("error", "unknown error")
            await update.message.reply_text(f"couldn't deliver: {error}")
        return

    # Process as a normal text message
    user_message = f"[voice message] {text}"

    chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    routing = {"adapter": "telegram", "chat_id": chat_id}
    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing)
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    # Store history
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    for msg in result["history_messages"]:
        _append_message(chat_id, msg)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await _send_result(update, result)


def _photo_content(image_bytes: bytes, mime_type: str = "image/jpeg", caption: str = "") -> list:
    """Build a multimodal content list for an image (litellm image_url format)."""
    b64 = base64.b64encode(image_bytes).decode()
    content = [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}]
    content.append({"type": "text", "text": caption if caption else "What's in this image?"})
    return content


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photos and image documents — pass to Haiku for analysis."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Photo from user_id={user_id}")
    if not is_allowed(update):
        return

    # Compressed photo array vs. original image sent as document
    if update.message.photo:
        photo = update.message.photo[-1]  # highest resolution
        mime_type = "image/jpeg"
    elif update.message.document and (update.message.document.mime_type or "").startswith("image/"):
        photo = update.message.document
        mime_type = update.message.document.mime_type or "image/jpeg"
    else:
        return

    caption = update.message.caption or ""

    try:
        file = await context.bot.get_file(photo.file_id)
        image_bytes = bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Photo download failed: {e}")
        await update.message.reply_text("Couldn't download that image.")
        return

    # Save image to disk
    ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"
    if ext == "jpeg":
        ext = "jpg"
    file_name = f"photo_{photo.file_unique_id}.{ext}"
    saved_path = _save_upload(file_name, image_bytes)
    logger.info(f"Saved photo: {saved_path} ({mime_type}, {len(image_bytes)} bytes)")

    chat_id = update.effective_chat.id
    user_message = f"[image] saved at {saved_path}"
    if caption:
        user_message += f" — {caption}"
    user_content = _photo_content(image_bytes, mime_type, caption)

    chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    routing = {"adapter": "telegram", "chat_id": chat_id}

    # Immediate ack — let the user know we're looking at their photo
    await _dog_ack(update)

    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing, user_content=user_content)
    except Exception as e:
        logger.error(f"Error getting response for photo: {e}")
        await update.message.reply_text(f"photo error: {e}")
        return

    # Store text-only in history — no point persisting base64 blobs
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    for msg in result["history_messages"]:
        _append_message(chat_id, msg)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await _send_result(update, result)


def _get_uploads_dir() -> Path:
    """Resolve uploads dir from current WOLT_NAME (respects switch_wolt)."""
    wolt = os.environ.get("WOLT_NAME", _WOLT_NAME)
    return wolt_uploads_dir(wolt, _WOLTS_DIR)


def _save_upload(file_name: str, data: bytes) -> Path:
    """Save uploaded file to the active wolt's uploads directory. Returns the saved path."""
    uploads = _get_uploads_dir()
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / file_name
    # Avoid overwriting — append counter if file exists
    if dest.exists():
        stem = dest.stem
        suffix = dest.suffix
        counter = 1
        while dest.exists():
            dest = uploads / f"{stem}_{counter}{suffix}"
            counter += 1
    dest.write_bytes(data)
    return dest


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming documents/files — download to disk and pass path to wolt."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Document from user_id={user_id}")
    if not is_allowed(update):
        return

    doc = update.message.document
    if not doc:
        return

    file_name = doc.file_name or f"file_{doc.file_unique_id}"
    mime_type = doc.mime_type or "application/octet-stream"
    caption = update.message.caption or ""

    try:
        file = await context.bot.get_file(doc.file_id)
        file_bytes = bytes(await file.download_as_bytearray())
    except Exception as e:
        logger.error(f"Document download failed: {e}")
        await update.message.reply_text("Couldn't download that file.")
        return

    saved_path = _save_upload(file_name, file_bytes)
    logger.info(f"Saved document: {saved_path} ({mime_type}, {len(file_bytes)} bytes)")

    chat_id = update.effective_chat.id
    user_message = (
        f"[file received] {file_name} ({mime_type}, {len(file_bytes)} bytes) "
        f"saved at {saved_path}"
    )
    if caption:
        user_message += f"\nCaption: {caption}"

    # Den reply — route file info directly to session
    den_session = _is_den_reply(update)
    if den_session:
        human_name = "human"
        den_msg = (
            f"[telegram file from {human_name}, chat_id={chat_id}]: {user_message}\n"
            f"Reply back to them with: notify --telegram {chat_id} \"your message\""
        )
        result = message_session(den_session, den_msg)
        _bot_log("den_reply_file", {"session": den_session, "file": file_name, "path": str(saved_path), "result": result})
        if result.get("ok"):
            session_link = result.get("url") or den_session
            if result.get("status") == "revived":
                await update.message.reply_text(f"🪵 session had exited — revived and delivered\n{session_link}")
            else:
                await update.message.reply_text(f"🪵 sent\n{session_link}")
        else:
            error = result.get("error", "unknown error")
            await update.message.reply_text(f"couldn't deliver: {error}")
        return

    chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    routing = {"adapter": "telegram", "chat_id": chat_id}

    await _dog_ack(update)

    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing)
    except Exception as e:
        logger.error(f"Error getting response for document: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    for msg in result["history_messages"]:
        _append_message(chat_id, msg)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await _send_result(update, result)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_allowed(update):
        return
    await update.message.reply_text(f"Hey. I'm {_dog_name()}. Talk to me.")


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sessions — list active Claude Code sessions."""
    if not is_allowed(update):
        return
    sessions = list_sessions()
    if not sessions:
        await update.message.reply_text("No active sessions.")
        return
    tunnel_url = get_tunnel_url()
    lines = []
    for s in sessions:
        link = f"{tunnel_url}/tui?session={s['name']}" if tunnel_url else s["name"]
        lines.append(f"• {s['name']}\n  {link}")
    await update.message.reply_text("\n\n".join(lines))


async def handle_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /kill <session-name> — kill a stale session."""
    if not is_allowed(update):
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /kill <session-name>")
        return
    name = args[0]
    if kill_session(name):
        await update.message.reply_text(f"Killed {name}.")
    else:
        await update.message.reply_text(f"Couldn't kill {name}.")


async def handle_wolt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wolt [name] — switch active wolt or list available."""
    if not is_allowed(update):
        return
    args = context.args
    if not args:
        wolts = list_wolts()
        active = os.environ.get("WOLT_NAME", "?")
        lines = [f"active: {active}", "", "available:"]
        for w in wolts:
            marker = " ←" if w == active else ""
            lines.append(f"  • {w}{marker}")
        lines.append("\n/wolt <name> to switch")
        await update.message.reply_text("\n".join(lines))
        return
    name = args[0]
    result = switch_wolt(name)
    if result:
        # Clear in-memory history so the new wolt starts fresh
        chat_id = update.effective_chat.id
        chat_histories[chat_id] = []
        await update.message.reply_text(f"Switched to {name}.")
    else:
        await update.message.reply_text(f"No wolt named '{name}' found.")




def run():
    """Start the Telegram bot."""
    load_allowed_users()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("kill", handle_kill))
    app.add_handler(CommandHandler("wolt", handle_wolt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, handle_document))

    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} telegram bot starting...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        async with app:
            await app.start()
            await app.updater.start_polling()
            await asyncio.Event().wait()

    loop.run_until_complete(_run())


if __name__ == "__main__":
    run()
