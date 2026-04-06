"""
Telegram adapter v2 — chat-per-wolt model.

Each Telegram chat (group or private) has an active wolt + session.
Messages go directly to the active session. Dog only handles @mentions
or chats with no active wolt.

Chat state stored globally at wolts/.space/telegram/chats/{chat_id}.json
Uploads stored at wolts/.space/telegram/uploads/
History stored at wolts/.space/telegram/history/{chat_id}.jsonl
"""

import os
import json
import base64
import logging
import asyncio
import random
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from telegram import Update
from telegram.constants import ChatAction
from telegram.error import TimedOut
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, filters, ContextTypes
import re
import sys
from bot.core import (
    get_response, transcribe_audio, message_session, list_sessions,
    kill_session, get_tunnel_url, list_wolts, _bot_log, build_ack_text,
    _sanitize_history, start_claude_session,
)
from wolts import get_active_creature

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from paths import space_dir

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

# Global telegram state directories
TELEGRAM_DIR = space_dir(_WOLTS_DIR) / "telegram"
CHATS_DIR = TELEGRAM_DIR / "chats"
HISTORY_DIR = TELEGRAM_DIR / "history"
UPLOADS_DIR = TELEGRAM_DIR / "uploads"

MAX_HISTORY = 20

CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶", "wolf": "🐺"}

# --- Dog personality ---

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


def _dog_name() -> str:
    name = get_active_creature("dog")
    return name or os.environ.get("WOLT_NAME", "wolt")


async def _reply(update: Update, text: str, **kwargs):
    """reply_text with a single retry on TimedOut."""
    try:
        return await update.message.reply_text(text, **kwargs)
    except TimedOut:
        logger.warning("reply_text timed out, retrying once...")
        await asyncio.sleep(1)
        return await update.message.reply_text(text, **kwargs)


async def _dog_ack(update: Update):
    try:
        await _reply(update, random.choice(DOG_ACK_MESSAGES))
    except Exception:
        pass


# --- Allowed users ---

ALLOWED_USERS: set[int] = set()


def load_allowed_users():
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "")
    for uid in raw.split(","):
        uid = uid.strip()
        if uid.isdigit():
            ALLOWED_USERS.add(int(uid))


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return True
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Chat state — per chat_id, stored globally
# ---------------------------------------------------------------------------

def _chat_state_file(chat_id: int) -> Path:
    return CHATS_DIR / f"{chat_id}.json"


def _load_chat_state(chat_id: int) -> dict:
    """Load chat state: {active_wolt, active_session, updated_at}"""
    path = _chat_state_file(chat_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_chat_state(chat_id: int, state: dict):
    CHATS_DIR.mkdir(parents=True, exist_ok=True)
    state["chat_id"] = chat_id
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    _chat_state_file(chat_id).write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# Chat history — global, per chat_id
# ---------------------------------------------------------------------------

def _history_file(chat_id: int) -> Path:
    return HISTORY_DIR / f"{chat_id}.jsonl"


def _load_history(chat_id: int) -> list:
    path = _history_file(chat_id)
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
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(_history_file(chat_id), "a") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def _append_message(chat_id: int, msg: dict):
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(_history_file(chat_id), "a") as f:
        f.write(json.dumps({**msg, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


# ---------------------------------------------------------------------------
# File uploads — global
# ---------------------------------------------------------------------------

def _save_upload(file_name: str, data: bytes) -> Path:
    """Save uploaded file to global telegram uploads dir. Returns path."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    # Prefix with timestamp to avoid collisions
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    safe_name = f"{ts}_{file_name}"
    dest = UPLOADS_DIR / safe_name
    # Still handle unlikely collision
    if dest.exists():
        counter = 1
        stem = dest.stem
        suffix = dest.suffix
        while dest.exists():
            dest = UPLOADS_DIR / f"{stem}_{counter}{suffix}"
            counter += 1
    dest.write_bytes(data)
    return dest


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------

def format_response(result: dict) -> str:
    if result["type"] == "session":
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


def _format_tool_log(tc: dict) -> str:
    name = tc["tool"]
    creature = tc.get("creature") or tc.get("args", {}).get("creature", "")
    emoji = CREATURE_EMOJIS.get(creature, "")
    wolt = tc.get("args", {}).get("wolt", "")
    recipient = f"{emoji} {wolt}".strip()
    line = f"🪵 {recipient} — {name}" if recipient else f"🪵 {name}"
    url = tc.get("url", "")
    if url:
        line += f"\n{url}"
    return line


async def _send_result(update: Update, result: dict):
    """Send a result to the user — tool call logs first, then response."""
    for tc in result.get("tool_calls_log", []):
        try:
            await _reply(update, _format_tool_log(tc))
        except Exception:
            pass

    if result["type"] == "image":
        caption = result.get("text") or result.get("caption") or None
        with open(result["path"], "rb") as f:
            await update.message.reply_photo(photo=f, caption=caption)
    else:
        await _reply(update, format_response(result))


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

def _is_session_alive(session_name: str) -> bool:
    """Check if a session is still alive in tmux."""
    import subprocess
    try:
        subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _spawn_session(wolt: str, chat_id: int, prompt: str = "") -> dict:
    """Spawn a new Claude Code session for a wolt, return session info."""
    routing = {"adapter": "telegram", "chat_id": chat_id}
    session = start_claude_session(
        prompt=prompt or f"/woltspace-start-chat telegram {wolt}",
        wolt=wolt,
        routing=routing,
    )
    return session


async def _route_to_session(update: Update, session_name: str, wolt: str, text: str, chat_id: int):
    """Route a message directly to a Claude Code session."""
    creature = ""  # will be filled from wolt.json by message_session
    emoji = "🐾"

    # Try to get creature emoji from wolt.json
    wolt_json = _WOLTS_DIR / wolt / "wolt" / "wolt.json"
    if wolt_json.exists():
        try:
            data = json.loads(wolt_json.read_text())
            creature = data.get("type", "")
            emoji = CREATURE_EMOJIS.get(creature, "🐾")
        except (json.JSONDecodeError, OSError):
            pass

    session_msg = (
        f"[telegram message from human, chat_id={chat_id}]: {text}\n"
        f"Reply back to them with: notify --telegram {chat_id} \"your message\""
    )
    result = message_session(session_name, session_msg)
    _bot_log("telegram_v2_session_route", {
        "session": session_name, "wolt": wolt,
        "text": text[:200], "result": result,
    })

    _append_history(chat_id, "user", text)

    if result.get("ok"):
        session_link = result.get("url") or session_name
        if result.get("status") == "revived":
            await _reply(update, f"🪵 session had exited — revived and delivered\n{session_link}")
        else:
            await _reply(update, f"🪵 sent to {session_name}\n{session_link}")
        _append_message(chat_id, {
            "role": "assistant",
            "content": f"[delivered to session {session_name}]",
        })
    else:
        # Session truly dead — need to respawn
        return False

    return True


# ---------------------------------------------------------------------------
# Bot mention detection
# ---------------------------------------------------------------------------

def _is_dog_mention(text: str, bot_username: str) -> bool:
    """Check if the message explicitly @mentions the bot (i.e. asking for dog)."""
    if not bot_username:
        return False
    return f"@{bot_username}" in (text or "")


def _strip_mention(text: str, bot_username: str) -> str:
    if not bot_username:
        return text
    return (text or "").replace(f"@{bot_username}", "").strip()


# ---------------------------------------------------------------------------
# Multimodal helpers
# ---------------------------------------------------------------------------

def _photo_content(image_bytes: bytes, mime_type: str = "image/jpeg", caption: str = "") -> list:
    b64 = base64.b64encode(image_bytes).decode()
    content = [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}]
    content.append({"type": "text", "text": caption if caption else "What's in this image?"})
    return content


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main message handler — routes based on chat state."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Message from user_id={user_id}")
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id
    text = update.message.text or ""
    bot_username = context.bot.username or ""

    # In group chats, only respond when @mentioned or replied to
    chat_type = update.effective_chat.type
    if chat_type in ("group", "supergroup"):
        bot_id = context.bot.id
        is_mention = _is_dog_mention(text, bot_username)
        is_reply = (
            update.message.reply_to_message
            and update.message.reply_to_message.from_user
            and update.message.reply_to_message.from_user.id == bot_id
        )
        if not is_mention and not is_reply:
            # Check if there's an active session — if so, route to it
            state = _load_chat_state(chat_id)
            if state.get("active_wolt") and state.get("active_session"):
                # Active session exists — route to it even without mention
                pass  # fall through to routing logic below
            else:
                return  # no active wolt, not mentioned — ignore

    # --- @dog mention: always goes to dog ---
    if _is_dog_mention(text, bot_username):
        user_message = _strip_mention(text, bot_username)
        await _handle_dog(update, context, chat_id, user_message)
        return

    # --- Load chat state ---
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    # --- No active wolt: dog handles ---
    if not active_wolt:
        await _handle_dog(update, context, chat_id, text)
        return

    # --- Has active wolt + session: route to session ---
    if active_session:
        if _is_session_alive(active_session):
            success = await _route_to_session(update, active_session, active_wolt, text, chat_id)
            if success:
                return
        # Session is dead — respawn
        await _reply(update, f"🪵 session expired — spawning new one for {active_wolt}")
        try:
            session = _spawn_session(active_wolt, chat_id)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {active_wolt}\n{session_link}")
            # Send the original message to the new session
            await _route_to_session(update, session["name"], active_wolt, text, chat_id)
        except Exception as e:
            logger.error(f"Failed to spawn session for {active_wolt}: {e}")
            await _reply(update, f"couldn't start session for {active_wolt}: {e}")
        return

    # --- Has active wolt but no session: spawn one ---
    try:
        session = _spawn_session(active_wolt, chat_id)
        state["active_session"] = session["name"]
        _save_chat_state(chat_id, state)
        tunnel_url = get_tunnel_url()
        session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
        await _reply(update, f"🪵 new session for {active_wolt}\n{session_link}")
        # Send the message to the new session
        await _route_to_session(update, session["name"], active_wolt, text, chat_id)
    except Exception as e:
        logger.error(f"Failed to spawn session for {active_wolt}: {e}")
        await _reply(update, f"couldn't start session for {active_wolt}: {e}")


async def _handle_dog(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_message: str):
    """Route a message to the dog (Haiku) for routing/admin."""
    await _dog_ack(update)

    history = _load_history(chat_id)
    routing = {"adapter": "telegram", "chat_id": chat_id}

    try:
        result = get_response(user_message, conversation_history=list(history), routing=routing)
    except Exception as e:
        logger.error(f"Error getting response: {e}")
        await _reply(update, "Something broke on my end. Try again in a sec.")
        return

    # Store history
    _append_history(chat_id, "user", user_message)
    for msg in result["history_messages"]:
        _append_message(chat_id, msg)

    await _send_result(update, result)

    # If dog spawned a session, update chat state
    if result.get("type") == "session":
        session = result.get("session", {})
        session_name = session.get("name")
        wolt_name = session.get("wolt") or ""
        if session_name and wolt_name:
            state = _load_chat_state(chat_id)
            state["active_wolt"] = wolt_name
            state["active_session"] = session_name
            _save_chat_state(chat_id, state)

            emoji = CREATURE_EMOJIS.get(session.get("creature", ""), "🐾")
            session_link = session.get("url") or ""
            handoff = f"{emoji} {wolt_name} is now active in this chat — messages go directly to this session"
            if session_link:
                handoff += f"\n{session_link}"
            await _reply(update, handoff)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice/audio — transcribe, save, and route like text."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Voice message from user_id={user_id}")
    if not is_allowed(update):
        return

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    chat_id = update.effective_chat.id
    await _reply(update, random.choice(DOG_VOICE_ACKS))

    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name

    try:
        text = transcribe_audio(tmp_path)
    except Exception as e:
        logger.error(f"Transcription failed: {e}")
        await _reply(update, "Couldn't transcribe that voice message.")
        return
    finally:
        # Save audio to uploads
        try:
            audio_name = f"voice_{voice.file_unique_id}.ogg"
            _save_upload(audio_name, Path(tmp_path).read_bytes())
        except Exception:
            pass
        Path(tmp_path).unlink(missing_ok=True)

    logger.info(f"Transcribed: {text[:100]}")

    # Route transcribed text through the same logic as handle_message
    # Synthesize the message text and route it
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    voice_message = f"[voice message] {text}"

    if active_wolt and active_session and _is_session_alive(active_session):
        await _route_to_session(update, active_session, active_wolt, voice_message, chat_id)
    elif active_wolt:
        # Session dead or missing — spawn new one
        try:
            session = _spawn_session(active_wolt, chat_id)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {active_wolt}\n{session_link}")
            await _route_to_session(update, session["name"], active_wolt, voice_message, chat_id)
        except Exception as e:
            await _reply(update, f"couldn't start session: {e}")
    else:
        # No active wolt — dog handles
        await _handle_dog(update, context, chat_id, voice_message)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photos/image documents — save to disk and route to session."""
    user_id = update.effective_user.id if update.effective_user else None
    logger.info(f"Photo from user_id={user_id}")
    if not is_allowed(update):
        return

    if update.message.photo:
        photo = update.message.photo[-1]
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
        await _reply(update, "Couldn't download that image.")
        return

    # Save to disk
    ext = mime_type.split("/")[-1] if "/" in mime_type else "jpg"
    if ext == "jpeg":
        ext = "jpg"
    file_name = f"photo_{photo.file_unique_id}.{ext}"
    saved_path = _save_upload(file_name, image_bytes)
    logger.info(f"Saved photo: {saved_path} ({mime_type}, {len(image_bytes)} bytes)")

    chat_id = update.effective_chat.id
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    file_msg = f"[image received] {file_name} ({mime_type}, {len(image_bytes)} bytes) saved at {saved_path}"
    if caption:
        file_msg += f"\nCaption: {caption}"

    if active_wolt and active_session and _is_session_alive(active_session):
        await _route_to_session(update, active_session, active_wolt, file_msg, chat_id)
    elif active_wolt:
        # Spawn session, send file info
        try:
            session = _spawn_session(active_wolt, chat_id)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {active_wolt}\n{session_link}")
            await _route_to_session(update, session["name"], active_wolt, file_msg, chat_id)
        except Exception as e:
            await _reply(update, f"couldn't start session: {e}")
    else:
        # No active wolt — dog handles with vision
        user_content = _photo_content(image_bytes, mime_type, caption)
        user_message = f"[image] {caption}" if caption else "[image]"
        await _dog_ack(update)
        history = _load_history(chat_id)
        routing = {"adapter": "telegram", "chat_id": chat_id}
        try:
            result = get_response(user_message, conversation_history=list(history), routing=routing, user_content=user_content)
        except Exception as e:
            await _reply(update, f"photo error: {e}")
            return
        _append_history(chat_id, "user", user_message)
        for msg in result["history_messages"]:
            _append_message(chat_id, msg)
        await _send_result(update, result)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle non-image documents — download to disk and route to session."""
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
        await _reply(update, "Couldn't download that file.")
        return

    saved_path = _save_upload(file_name, file_bytes)
    logger.info(f"Saved document: {saved_path} ({mime_type}, {len(file_bytes)} bytes)")

    chat_id = update.effective_chat.id
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    file_msg = (
        f"[file received] {file_name} ({mime_type}, {len(file_bytes)} bytes) "
        f"saved at {saved_path}"
    )
    if caption:
        file_msg += f"\nCaption: {caption}"

    if active_wolt and active_session and _is_session_alive(active_session):
        await _route_to_session(update, active_session, active_wolt, file_msg, chat_id)
    elif active_wolt:
        try:
            session = _spawn_session(active_wolt, chat_id)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {active_wolt}\n{session_link}")
            await _route_to_session(update, session["name"], active_wolt, file_msg, chat_id)
        except Exception as e:
            await _reply(update, f"couldn't start session: {e}")
    else:
        # No active wolt — dog handles
        await _handle_dog(update, context, chat_id, file_msg)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await _reply(update, f"Hey. I'm {_dog_name()}. Talk to me and I'll connect you to a wolt.")


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    sessions = list_sessions()
    alive = [s for s in sessions if s.get("alive")]
    if not alive:
        await _reply(update, "No active sessions.")
        return
    tunnel_url = get_tunnel_url()
    lines = []
    for s in alive:
        wolt = s["name"].rsplit("-", 2)[0] if "-" in s["name"] else s["name"]
        link = f"{tunnel_url}/tui?session={s['name']}" if tunnel_url else s["name"]
        lines.append(f"• {s['name']}\n  {link}")
    await _reply(update, "\n\n".join(lines))


async def handle_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    args = context.args
    if not args:
        await _reply(update, "Usage: /kill <session-name>")
        return
    name = args[0]
    if kill_session(name):
        await _reply(update, f"Killed {name}.")
        # Clear from any chat state that references this session
        if CHATS_DIR.exists():
            for f in CHATS_DIR.glob("*.json"):
                try:
                    state = json.loads(f.read_text())
                    if state.get("active_session") == name:
                        state["active_session"] = None
                        f.write_text(json.dumps(state, indent=2))
                except (json.JSONDecodeError, OSError):
                    pass
    else:
        await _reply(update, f"Couldn't kill {name}.")


async def handle_setwolt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wolt <name> — set the active wolt for this chat."""
    if not is_allowed(update):
        return
    args = context.args
    chat_id = update.effective_chat.id

    if not args:
        # Show current state + available wolts
        state = _load_chat_state(chat_id)
        active = state.get("active_wolt", "none")
        session = state.get("active_session", "none")
        wolts = list_wolts()
        lines = [f"active wolt: {active}", f"active session: {session}", "", "available:"]
        for w in wolts:
            marker = " ←" if w == active else ""
            lines.append(f"  • {w}{marker}")
        lines.append("\n/wolt <name> to set active wolt")
        await _reply(update, "\n".join(lines))
        return

    name = args[0]
    # Verify wolt exists
    wolt_dir = _WOLTS_DIR / name / "wolt"
    if not wolt_dir.is_dir():
        await _reply(update, f"No wolt named '{name}' found.")
        return

    state = _load_chat_state(chat_id)
    state["active_wolt"] = name
    state["active_session"] = None  # will spawn on next message
    _save_chat_state(chat_id, state)
    await _reply(update, f"Set {name} as the active wolt for this chat. Next message will start a session.")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — log and swallow TimedOut, re-raise others."""
    err = context.error
    if isinstance(err, TimedOut):
        logger.warning(f"Telegram TimedOut (swallowed): {err}")
        return
    logger.error(f"Unhandled error: {err}", exc_info=err)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def run():
    """Start the Telegram bot (v2 — chat-per-wolt model)."""
    load_allowed_users()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()
    app.add_error_handler(_error_handler)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("kill", handle_kill))
    app.add_handler(CommandHandler("wolt", handle_setwolt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, handle_document))

    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} telegram v2 bot starting (chat-per-wolt model)...")

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
