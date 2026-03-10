"""
Telegram adapter — thin layer over core.
Platform default. Wolt can override by placing wolt/bot/telegram_adapter.py in their repo.
"""

import os
import json
import base64
import logging
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from bot.core import get_response, transcribe_audio, list_sessions, kill_session, get_tunnel_url, switch_wolt, list_wolts, read_session_routing, _bot_log, build_ack_text, _sanitize_history

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
STATE_DIR = WOLT_DIR / ".state"
CHAT_DIR = STATE_DIR / "chat"

ALLOWED_USERS: set[int] = set()
chat_histories: dict[int, list] = defaultdict(list)
MAX_HISTORY = 20


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
            return result["text"]
        s = result["session"]
        return build_ack_text(s.get("url"), s.get("name"), "telegram")
    if result["type"] == "image":
        return result.get("caption", "") or result.get("filename", "image")
    return result["text"]


async def _send_result(update: Update, result: dict):
    """Send a result to the user — handles text, session, and image types."""
    if result["type"] == "image":
        caption = result.get("caption") or None
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
    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing)
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    response = format_response(result)
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"] if result["type"] == "session" else [{"role": "assistant", "content": response}]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    if result["type"] == "session":
        for msg in result["history_messages"]:
            _append_message(chat_id, msg)
    else:
        _append_history(chat_id, "assistant", response)

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
        Path(tmp_path).unlink(missing_ok=True)

    logger.info(f"Transcribed: {text[:100]}")

    # Process as a normal text message
    chat_id = update.effective_chat.id
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

    response = format_response(result)
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"] if result["type"] == "session" else [{"role": "assistant", "content": response}]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    if result["type"] == "session":
        for msg in result["history_messages"]:
            _append_message(chat_id, msg)
    else:
        _append_history(chat_id, "assistant", response)

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

    chat_id = update.effective_chat.id
    user_message = f"[image] {caption}" if caption else "[image]"
    user_content = _photo_content(image_bytes, mime_type, caption)

    chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    routing = {"adapter": "telegram", "chat_id": chat_id}
    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing, user_content=user_content)
    except Exception as e:
        logger.error(f"Error getting response for photo: {e}")
        await update.message.reply_text(f"photo error: {e}")
        return

    response = format_response(result)
    # Store text-only in history — no point persisting base64 blobs
    history.append({"role": "user", "content": user_message})
    for msg in result["history_messages"] if result["type"] == "session" else [{"role": "assistant", "content": response}]:
        history.append(msg)
    _append_history(chat_id, "user", user_message)
    if result["type"] == "session":
        for msg in result["history_messages"]:
            _append_message(chat_id, msg)
    else:
        _append_history(chat_id, "assistant", response)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await _send_result(update, result)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    if not is_allowed(update):
        return
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    await update.message.reply_text(f"Hey. I'm {wolt_name}. Talk to me.")


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
