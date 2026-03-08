"""
Telegram adapter — thin layer over core.
Platform default. Wolt can override by placing wolt/bot/telegram_adapter.py in their repo.
"""

import os
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
from bot.core import get_response, list_sessions, kill_session, get_tunnel_url

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

    chat_id = update.effective_chat.id
    user_message = update.message.text
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


def run():
    """Start the Telegram bot."""
    load_allowed_users()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("kill", handle_kill))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} telegram bot starting...")
    app.run_polling()


if __name__ == "__main__":
    run()
