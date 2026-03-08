"""
Telegram adapter — thin layer over core.
Platform default. Wolt can override by placing wolt/bot/telegram_adapter.py in their repo.
"""

import os
import json
import logging
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from bot.core import get_response, transcribe_audio, list_sessions, kill_session, get_tunnel_url, switch_wolt, list_wolts

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
    """Load last N message pairs from disk."""
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
    return messages


def _append_history(chat_id: int, role: str, content: str):
    """Append a single message to disk."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_chat_file(chat_id), "a") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def load_allowed_users():
    """Load allowed user IDs from env. Comma-separated."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    for uid in raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))


def format_response(result: dict) -> str:
    """Format a core response dict for Telegram."""
    if result["type"] == "session":
        s = result["session"]
        if s["url"]:
            return f"On it — started a session.\n\n{s['url']}\n\n({s['name']})"
        return f"Started session {s['name']} but couldn't find tunnel URL."
    return result["text"]


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

    # In group chats, only respond when @mentioned by bot username
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        bot_username = context.bot.username
        text = update.message.text or ""
        if f"@{bot_username}" not in text:
            return
        # Strip the @mention from the message
        text = text.replace(f"@{bot_username}", "").strip()
    else:
        text = update.message.text

    chat_id = update.effective_chat.id
    user_message = text
    if update.message.reply_to_message and update.message.reply_to_message.text:
        user_message = f"[replying to: \"{update.message.reply_to_message.text}\"]\n{user_message}"

    # Load from disk on first access
    if chat_id not in chat_histories:
        chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    try:
        result = get_response(user_message, conversation_history=list(history))
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    response = format_response(result)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": response})
    _append_history(chat_id, "user", user_message)
    _append_history(chat_id, "assistant", response)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await update.message.reply_text(response)


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

    if chat_id not in chat_histories:
        chat_histories[chat_id] = _load_history(chat_id)
    history = chat_histories[chat_id]

    try:
        result = get_response(user_message, conversation_history=list(history))
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await update.message.reply_text("Something broke on my end. Try again in a sec.")
        return

    response = format_response(result)
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": response})
    _append_history(chat_id, "user", user_message)
    _append_history(chat_id, "assistant", response)

    if len(history) > MAX_HISTORY * 2:
        chat_histories[chat_id] = history[-MAX_HISTORY * 2:]

    await update.message.reply_text(response)


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


RESULTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts")) / ".state" / "task-results"
_notified_sessions: set[str] = set()


async def _watch_task_results(app):
    """Background task: watch for completed sessions and notify via Telegram."""
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
    chat_id = int(allowed[0].strip()) if allowed and allowed[0].strip().isdigit() else None
    if not chat_id:
        return

    while True:
        await asyncio.sleep(10)
        if not RESULTS_DIR.exists():
            continue
        for f in RESULTS_DIR.glob("*.json"):
            if f.stem in _notified_sessions:
                continue
            try:
                data = json.loads(f.read_text())
                session = data.get("session", f.stem)
                output = data.get("output", "")
                # Trim output for telegram
                if len(output) > 2000:
                    output = output[-2000:]
                tunnel_url = data.get("url", "")
                msg = f"session {session} finished.\n\n{output}"
                if tunnel_url:
                    msg += f"\n\n{tunnel_url}"
                await app.bot.send_message(chat_id=chat_id, text=msg)
                _notified_sessions.add(f.stem)
            except Exception as e:
                logger.error(f"Error reading task result {f}: {e}")


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

    # Start background watcher for task completions
    app.job_queue.run_once(lambda ctx: asyncio.ensure_future(_watch_task_results(app)), when=5)

    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} telegram bot starting...")
    app.run_polling()


if __name__ == "__main__":
    run()
