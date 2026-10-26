"""
Telegram adapter v2 — chat-per-wolt model.

Each Telegram chat (group or private) has an active wolt + session.
Messages go directly to the active session. Dog only handles @mentions
or chats with no active wolt.

Chat state stored in the lodge database (chat_state, adapter "telegram")
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
from telegram import Update, BotCommand, MenuButtonCommands, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import NetworkError, TimedOut
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, filters, ContextTypes
import re
import sys
from bot.core import (
    dog_available, get_response, transcribe_audio, message_session, list_sessions,
    kill_session, get_tunnel_url, list_wolts, _bot_log, build_ack_text,
    _sanitize_history, start_claude_session,
)
from urllib.parse import urlparse, parse_qs
from wolts import get_active_creature, wolt_display_name, wolt_exists

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import outbox
from env_compat import get_env
from paths import space_dir
import chat_state
from session_runtime import RuntimeHandle, get_runtime
from sessions import resolve_active_session, wolt_harness
from harnesses import platform_skill_invoke
from skills_sync import wolt_skills_delivery
from notify_prompt import notify_reply_instruction
import lodge_commands as commands
from connector_contract import Ack, Manifest, Reply

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

_WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))

# Global telegram state directories
TELEGRAM_DIR = space_dir(_WOLTS_DIR) / "telegram"
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
    return name or get_env("WOLTSPACE_WOLT_NAME", "wolt")


async def _reply(update: Update, text: str, **kwargs):
    """reply_text with a single retry on TimedOut."""
    try:
        return await update.message.reply_text(text, **kwargs)
    except TimedOut:
        logger.warning("reply_text timed out, retrying once...")
        await asyncio.sleep(1)
        return await update.message.reply_text(text, **kwargs)


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
                file_id,
                read_timeout=DOWNLOAD_READ_TIMEOUT,
                connect_timeout=DOWNLOAD_CONNECT_TIMEOUT,
            )
            return bytes(await file.download_as_bytearray(
                read_timeout=DOWNLOAD_READ_TIMEOUT,
                connect_timeout=DOWNLOAD_CONNECT_TIMEOUT,
            ))
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
    if not ALLOWED_USERS:
        logger.warning("TELEGRAM_ALLOWED_USERS is empty — bot will reject all messages. Set it to a comma-separated list of Telegram user IDs.")


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USERS:
        return False
    user_id = update.effective_user.id if update.effective_user else None
    return user_id in ALLOWED_USERS


# ---------------------------------------------------------------------------
# Chat state — per chat_id, stored globally
# ---------------------------------------------------------------------------

def _wolt_exists(name: str) -> bool:
    return wolt_exists(name, _WOLTS_DIR)


def _load_chat_state(chat_id: int) -> dict:
    """Load chat state: {active_wolt, active_session}

    A chosen wolt that is gone (archived, or removed by hand) reads as
    no choice, so every handler goes to the picker instead of a spawn that fails.
    """
    state = chat_state.get(_WOLTS_DIR, "telegram", chat_id)
    if state.get("active_wolt") and not _wolt_exists(state["active_wolt"]):
        state.pop("active_wolt")
        state.pop("active_session", None)
    return state


def _save_chat_state(chat_id: int, state: dict):
    chat_state.put(_WOLTS_DIR, "telegram", chat_id, state)


def _forget_session(name: str):
    """A killed session stops being any chat's active session."""
    chat_state.forget_session(_WOLTS_DIR, "telegram", name)


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
    """Check if a session is still alive in tmux. Diagnostic only — not used for routing.

    Goes straight to the runtime with a bare handle rather than through the
    registry: liveness is session-level, so the persisted pane would not change
    the answer, and this runs inside an async handler where a per-check scan of
    every wolt directory is not worth paying for.
    """
    try:
        runtime = get_runtime()
        alive = runtime.is_alive(RuntimeHandle(session_name, session_name))
        if not alive:
            _bot_log("session_alive_check_false", {
                "session": session_name,
                "panes": 0,
                "live_sessions": sorted(runtime.list_session_names())[:20],
            })
        return alive
    except Exception as e:
        _bot_log("session_alive_check_error", {"session": session_name, "error": str(e)})
        return False


def _shown(wolt: str) -> str:
    """The name a person reads for a wolt; routing and commands keep the slug."""
    return wolt_display_name(wolt, _WOLTS_DIR) if wolt else wolt


async def _notify_switch(update: Update, old_state: dict, new_wolt: str, new_session: str):
    """Send a brief message if active wolt or session changed."""
    old_wolt = old_state.get("active_wolt")
    old_session = old_state.get("active_session")
    emoji = CREATURE_EMOJIS.get("raccoon", "🐾")  # fallback, wolt.json lookup below
    wolt_json = _WOLTS_DIR / new_wolt / "wolt" / "wolt.json"
    if wolt_json.exists():
        try:
            data = json.loads(wolt_json.read_text())
            emoji = CREATURE_EMOJIS.get(data.get("type", ""), "🐾")
        except (json.JSONDecodeError, OSError):
            pass
    if new_wolt != old_wolt:
        await _reply(update, f"🪵 now talking to {emoji} {_shown(new_wolt)} ({new_session})")
    elif new_session != old_session:
        await _reply(update, f"🪵 now talking to {emoji} {_shown(new_wolt)} ({new_session})")


def _start_chat_prompt(wolt: str) -> str:
    """The bare boot prompt: start-chat, spelled for this wolt's engine AND
    its skills delivery — the two together decide the name."""
    invoke = platform_skill_invoke(
        wolt_harness(wolt), "start-chat",
        delivery=wolt_skills_delivery(_WOLTS_DIR / wolt))
    return f"{invoke} telegram {wolt}"


def _spawn_session(wolt: str, chat_id: int, prompt: str = "") -> dict:
    """Spawn a new Claude Code session for a wolt, return session info."""
    routing = {"adapter": "telegram", "chat_id": chat_id}
    session = start_claude_session(
        prompt=prompt or _start_chat_prompt(wolt),
        wolt=wolt,
        routing=routing,
    )
    return session


def _session_message(text: str, chat_id: int) -> str:
    """A Telegram message as its session reads it, pasted or in the opening prompt."""
    return f"[telegram message from human, chat_id={chat_id}]: {text}"


def _spawn_session_with_message(wolt: str, chat_id: int, text: str) -> dict:
    """Spawn a session for an incoming message, the message in its opening prompt.

    Pasted into the new pane after the spawn, the message was lost: the agent
    isn't reading input a second in (#555). Start-chat leads, so the spawn
    doesn't add a second one, and the spawn's Telegram context carries the
    reply instruction. Records what _route_to_session would have.
    """
    session = _spawn_session(
        wolt, chat_id, f"{_start_chat_prompt(wolt)}\n\n{_session_message(text, chat_id)}")
    _bot_log("telegram_v2_spawn_with_message", {
        "session": session["name"], "wolt": wolt, "text": text[:200],
    })
    _append_history(chat_id, "user", text)
    _append_message(chat_id, {
        "role": "assistant",
        "content": f"[delivered to session {session['name']}]",
    })
    return session


async def _route_to_session(update: Update, session_name: str, wolt: str, text: str, chat_id: int):
    """Route a message directly to a Claude Code session.

    Returns the name of the session the message was actually delivered to
    (which may differ from session_name if it fell back to the wolt's live
    session), or None if nothing could be delivered — callers save the
    returned name as the chat's active session.
    """
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
        f"{_session_message(text, chat_id)}\n"
        f"{notify_reply_instruction('--telegram', chat_id)}"
    )
    # message_session → resume_session blocks for as long as the agent takes
    # to come up (up to 12s). On the bot's event loop that stalls every other
    # chat, so the wait happens in a thread.
    result = await asyncio.to_thread(message_session, session_name, session_msg)
    _bot_log("telegram_v2_session_route", {
        "session": session_name, "wolt": wolt,
        "text": text[:200], "result": result,
    })

    _append_history(chat_id, "user", text)

    if not result.get("ok"):
        # Session unroutable — not in the registry, or a foreign-runtime
        # record left behind by a container↔native migration. Before giving
        # up (caller spawns fresh), prefer the wolt's live session: the
        # message still gets delivered, and the human is told where.
        live = resolve_active_session(wolt)
        fallback = (
            await asyncio.to_thread(message_session, live, session_msg)
            if live and live != session_name else None
        )
        _bot_log("telegram_v2_fallback_route", {
            "from": session_name, "to": live, "wolt": wolt,
            "result": fallback,
        })
        if fallback and fallback.get("ok"):
            session_link = fallback.get("url") or live
            await _reply(update, f"🪵 {session_name} is gone — delivered to {wolt}'s live session instead\n{session_link}")
            _append_message(chat_id, {
                "role": "assistant",
                "content": f"[delivered to session {live}]",
            })
            return live
        # Nothing live — caller respawns
        return None

    session_link = result.get("url") or session_name
    if result.get("status") == "revived":
        await _reply(update, f"🪵 session had exited — revived and delivered\n{session_link}")
    else:
        await _reply(update, f"🪵 sent to {session_name}\n{session_link}")
    _append_message(chat_id, {
        "role": "assistant",
        "content": f"[delivered to session {session_name}]",
    })
    return session_name


# ---------------------------------------------------------------------------
# Bot mention detection
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reply-to routing — parse session from notify footer
# ---------------------------------------------------------------------------

# Footer format from notify.py:
#   ---
#   ↩️ reply to this message to talk to this session directly
#   {tunnel_url}/tui?session={session_name}
_REPLY_FOOTER_MARKER = "↩️ reply to this message to talk to this session directly"


def _parse_session_from_reply(reply_text: str) -> str | None:
    """Extract session name from a notify message's footer URL."""
    if not reply_text or _REPLY_FOOTER_MARKER not in reply_text:
        return None
    # The session URL is on the line after the footer marker
    lines = reply_text.split("\n")
    for i, line in enumerate(lines):
        if _REPLY_FOOTER_MARKER in line:
            # Next line should be the session URL
            if i + 1 < len(lines):
                url_line = lines[i + 1].strip()
                # Parse ?session=NAME from the URL
                try:
                    parsed = urlparse(url_line)
                    qs = parse_qs(parsed.query)
                    if "session" in qs:
                        return qs["session"][0]
                except Exception:
                    pass
                # Fallback: might be just "session=NAME" without full URL
                if url_line.startswith("session="):
                    return url_line.split("=", 1)[1]
    return None


def _reply_source(reply_to) -> tuple[str, str | None]:
    """The text that may carry the reply footer, and the replied-to file's name if any."""
    # A file or a voice note carries the footer in its caption; a text message
    # in its text.
    body = next(
        (value for value in (reply_to.text, reply_to.caption) if isinstance(value, str) and value),
        "",
    )
    # Telegram keeps a file as a document, and as an audio when it is one it plays.
    for part in ("document", "audio"):
        name = getattr(getattr(reply_to, part, None), "file_name", None)
        if isinstance(name, str) and name:
            # Cleaned as an outgoing name is: anyone can send a file under any
            # name, and this one goes into the session's prompt.
            return body, outbox.display_name(name)
    return body, None


def _reply_is_voice(reply_to) -> bool:
    """Whether the replied-to message is a voice note."""
    # Judged by type, as the file name above is: a stand-in has every attribute.
    voice = getattr(reply_to, "voice", None)
    return isinstance(getattr(voice, "file_unique_id", None), str)


def _quote_reply(body: str, file_name: str | None, message: str, voice: bool = False) -> str:
    """Prefix a routed reply with what it replies to, without the reply footer."""
    footer = "---\n" + _REPLY_FOOTER_MARKER
    quoted = body
    if quoted.startswith(footer):
        # A file or a voice note sent after its text carries the footer alone as its caption.
        quoted = ""
    else:
        footer_idx = quoted.find("\n" + footer)
        if footer_idx >= 0:
            quoted = quoted[:footer_idx]
    what = f"file {file_name}" if file_name else "voice note" if voice else ""
    if not quoted.strip():
        return f"[replying to {what}]\n{message}" if what else message
    if what:
        return f"[replying to {what}: {quoted[:200]}]\n{message}"
    return f"[replying to: {quoted[:200]}]\n{message}"


def _wolt_from_session_name(session_name: str) -> str | None:
    """Extract wolt name from session name (e.g. 'nunu-swift-marsh-abc123' -> 'nunu')."""
    if not session_name:
        return None
    # Session names are {wolt}-{word}-{word}-{hex}
    # Wolt name is everything before the last 3 segments
    parts = session_name.rsplit("-", 3)
    if len(parts) == 4:
        return parts[0]
    return None


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
    has_reply = bool(update.message.reply_to_message)
    _bot_log("message_received", {"chat_id": chat_id, "text": text[:100], "has_reply": has_reply})

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

    # --- Bot mention: dog when available, otherwise normal routing ---
    if _is_dog_mention(text, bot_username):
        text = _strip_mention(text, bot_username)
        if dog_available():
            await _handle_dog(update, context, chat_id, text)
            return

    # --- Reply-to routing: check if replying to a specific wolt's message ---
    reply_to = update.message.reply_to_message
    reply_body, reply_file = _reply_source(reply_to) if reply_to else ("", None)
    if reply_body:
        reply_session = _parse_session_from_reply(reply_body)
        if reply_session:
            reply_wolt = _wolt_from_session_name(reply_session)
            # A reply to a gone wolt's old message is routed like any message.
            if reply_wolt and _wolt_exists(reply_wolt):
                # Build message with reply context, without the footer
                reply_text = _quote_reply(
                    reply_body, reply_file, text, _reply_is_voice(reply_to),
                )

                # Try routing directly — message_session handles revive internally
                prev_state = _load_chat_state(chat_id)
                delivered = await _route_to_session(update, reply_session, reply_wolt, reply_text, chat_id)
                if delivered:
                    new_state = dict(prev_state)
                    new_state["active_wolt"] = reply_wolt
                    new_state["active_session"] = delivered
                    _save_chat_state(chat_id, new_state)
                    await _notify_switch(update, prev_state, reply_wolt, delivered)
                    return
                # Route failed (session truly gone) — spawn new one for that wolt
                try:
                    session = _spawn_session_with_message(reply_wolt, chat_id, reply_text)
                    state = _load_chat_state(chat_id)
                    state["active_wolt"] = reply_wolt
                    state["active_session"] = session["name"]
                    _save_chat_state(chat_id, state)
                    tunnel_url = get_tunnel_url()
                    session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                    await _reply(update, f"🪵 session expired — new one for {reply_wolt}\n{session_link}")
                except Exception as e:
                    logger.error(f"Failed to spawn session for {reply_wolt}: {e}")
                    await _reply(update, f"couldn't start session for {_shown(reply_wolt)}: {e}")
                return
            # else: couldn't parse wolt from session name — fall through to regular path

    await _route_message(update, context, chat_id, text)


async def _select_wolt_without_dog(update: Update, chat_id: int, state: dict) -> bool:
    """Select the only builder, or explain how to choose without a model call."""
    wolts = [w for w in list_wolts() if _is_wolt(w)]
    if len(wolts) == 1:
        state["active_wolt"] = wolts[0].get("name") or Path(wolts[0].get("dir", "")).name
        state.pop("active_session", None)
        _save_chat_state(chat_id, state)
        return True
    if wolts:
        await _reply(
            update,
            _wolt_picker_header(wolts, None),
            reply_markup=_wolt_picker_keyboard(wolts, None),
        )
        await _reply(update, _PICK_THEN_RESEND)
    else:
        await _reply(update, _NO_WOLTS_MESSAGE)
    return False


async def _route_message(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """Share the existing active-session/spawn path across message types."""
    # --- Load chat state ---
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    if not active_wolt:
        if dog_available():
            await _handle_dog(update, context, chat_id, text)
            return
        if not await _select_wolt_without_dog(update, chat_id, state):
            return
        active_wolt = state["active_wolt"]
        active_session = None

    # --- Has active wolt + session: route to session ---
    if active_session:
        # Route directly — message_session handles revive (Claude exited, tmux dead, etc.)
        delivered = await _route_to_session(update, active_session, active_wolt, text, chat_id)
        if delivered:
            if delivered != active_session:
                state["active_session"] = delivered
                _save_chat_state(chat_id, state)
            return
        # message_session failed (session not in registry) — spawn new
        await _reply(update, f"🪵 session expired — spawning new one for {_shown(active_wolt)}")
        try:
            session = _spawn_session_with_message(active_wolt, chat_id, text)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
        except Exception as e:
            logger.error(f"Failed to spawn session for {active_wolt}: {e}")
            await _reply(update, f"couldn't start session for {_shown(active_wolt)}: {e}")
        return

    # --- Has active wolt but no session: spawn one ---
    try:
        session = _spawn_session_with_message(active_wolt, chat_id, text)
        state["active_session"] = session["name"]
        _save_chat_state(chat_id, state)
        tunnel_url = get_tunnel_url()
        session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
        await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
    except Exception as e:
        logger.error(f"Failed to spawn session for {active_wolt}: {e}")
        await _reply(update, f"couldn't start session for {_shown(active_wolt)}: {e}")


async def _handle_dog(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_message: str):
    """Route a message to the optional dog for routing/admin."""
    if not dog_available():
        await _route_message(update, context, chat_id, user_message)
        return
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
    try:
        await _reply(update, random.choice(DOG_VOICE_ACKS))
    except Exception as exc:
        logger.warning(f"Voice ack failed (chat_id={chat_id}): {exc}")

    try:
        audio_bytes = await _download_bytes(context, voice.file_id, "voice message")
    except Exception:
        await _say_download_failed(update, "voice message")
        return
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp.write(audio_bytes)
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
    voice_message = f"[voice message] {text}"

    # --- Reply-to routing: check if replying to a specific wolt's message ---
    reply_to = update.message.reply_to_message
    reply_body, reply_file = _reply_source(reply_to) if reply_to else ("", None)
    if reply_body:
        reply_session = _parse_session_from_reply(reply_body)
        if reply_session:
            reply_wolt = _wolt_from_session_name(reply_session)
            # A reply to a gone wolt's old message is routed like any message.
            if reply_wolt and _wolt_exists(reply_wolt):
                reply_text = _quote_reply(
                    reply_body, reply_file, voice_message, _reply_is_voice(reply_to),
                )

                # Try routing directly — message_session handles revive internally
                prev_state = _load_chat_state(chat_id)
                delivered = await _route_to_session(update, reply_session, reply_wolt, reply_text, chat_id)
                if delivered:
                    new_state = dict(prev_state)
                    new_state["active_wolt"] = reply_wolt
                    new_state["active_session"] = delivered
                    _save_chat_state(chat_id, new_state)
                    await _notify_switch(update, prev_state, reply_wolt, delivered)
                    return
                # Route failed (session truly gone) — spawn new one for that wolt
                try:
                    session = _spawn_session_with_message(reply_wolt, chat_id, reply_text)
                    new_state = dict(prev_state)
                    new_state["active_wolt"] = reply_wolt
                    new_state["active_session"] = session["name"]
                    _save_chat_state(chat_id, new_state)
                    tunnel_url = get_tunnel_url()
                    session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                    await _reply(update, f"🪵 session expired — new one for {reply_wolt}\n{session_link}")
                except Exception as e:
                    logger.error(f"Failed to spawn session for {reply_wolt}: {e}")
                    await _reply(update, f"couldn't start session for {_shown(reply_wolt)}: {e}")
                return

    if not dog_available():
        await _route_message(update, context, chat_id, voice_message)
        return

    # --- Regular routing ---
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    if active_wolt and active_session:
        delivered = await _route_to_session(update, active_session, active_wolt, voice_message, chat_id)
        if delivered and delivered != active_session:
            state["active_session"] = delivered
            _save_chat_state(chat_id, state)
        if not delivered:
            try:
                session = _spawn_session_with_message(active_wolt, chat_id, voice_message)
                state["active_session"] = session["name"]
                _save_chat_state(chat_id, state)
                tunnel_url = get_tunnel_url()
                session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
            except Exception as e:
                await _reply(update, f"couldn't start session: {e}")
    elif active_wolt:
        # No active session — spawn one
        try:
            session = _spawn_session_with_message(active_wolt, chat_id, voice_message)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
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
        image_bytes = await _download_bytes(context, photo.file_id, "image")
    except Exception:
        await _say_download_failed(update, "image")
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

    if not dog_available():
        await _route_message(update, context, chat_id, file_msg)
        return

    if active_wolt and active_session:
        delivered = await _route_to_session(update, active_session, active_wolt, file_msg, chat_id)
        if delivered and delivered != active_session:
            state["active_session"] = delivered
            _save_chat_state(chat_id, state)
        if not delivered:
            try:
                session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
                state["active_session"] = session["name"]
                _save_chat_state(chat_id, state)
                tunnel_url = get_tunnel_url()
                session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
            except Exception as e:
                await _reply(update, f"couldn't start session: {e}")
    elif active_wolt:
        # No active session — spawn one
        try:
            session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
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


async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video/animation/video_note — save to disk and route to session."""
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
    caption = msg.caption or ""

    try:
        file_bytes = await _download_bytes(context, media.file_id, "video")
    except Exception:
        await _say_download_failed(update, "video")
        return

    saved_path = _save_upload(file_name, file_bytes)
    logger.info(f"Saved video: {saved_path} ({mime_type}, {len(file_bytes)} bytes)")

    chat_id = update.effective_chat.id
    state = _load_chat_state(chat_id)
    active_wolt = state.get("active_wolt")
    active_session = state.get("active_session")

    file_msg = (
        f"[video received] {file_name} ({mime_type}, {len(file_bytes)} bytes) "
        f"saved at {saved_path}"
    )
    if caption:
        file_msg += f"\nCaption: {caption}"

    if active_wolt and active_session:
        delivered = await _route_to_session(update, active_session, active_wolt, file_msg, chat_id)
        if delivered and delivered != active_session:
            state["active_session"] = delivered
            _save_chat_state(chat_id, state)
        if not delivered:
            try:
                session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
                state["active_session"] = session["name"]
                _save_chat_state(chat_id, state)
                tunnel_url = get_tunnel_url()
                session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
            except Exception as e:
                await _reply(update, f"couldn't start session: {e}")
    elif active_wolt:
        try:
            session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
        except Exception as e:
            await _reply(update, f"couldn't start session: {e}")
    else:
        await _reply(update, "no active wolt — set one with /wolt first.")


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
        file_bytes = await _download_bytes(context, doc.file_id, "file")
    except Exception:
        await _say_download_failed(update, "file")
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

    if not dog_available():
        await _route_message(update, context, chat_id, file_msg)
        return

    if active_wolt and active_session:
        delivered = await _route_to_session(update, active_session, active_wolt, file_msg, chat_id)
        if delivered and delivered != active_session:
            state["active_session"] = delivered
            _save_chat_state(chat_id, state)
        if not delivered:
            try:
                session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
                state["active_session"] = session["name"]
                _save_chat_state(chat_id, state)
                tunnel_url = get_tunnel_url()
                session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
                await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
            except Exception as e:
                await _reply(update, f"couldn't start session: {e}")
    elif active_wolt:
        # No active session — spawn one
        try:
            session = _spawn_session_with_message(active_wolt, chat_id, file_msg)
            state["active_session"] = session["name"]
            _save_chat_state(chat_id, state)
            tunnel_url = get_tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session['name']}" if tunnel_url else session["name"]
            await _reply(update, f"🪵 new session for {_shown(active_wolt)}\n{session_link}")
        except Exception as e:
            await _reply(update, f"couldn't start session: {e}")
    else:
        # No active wolt — dog handles
        await _handle_dog(update, context, chat_id, file_msg)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# The owner's commands are the lodge's (lodge_commands): one implementation for
# every connector. Telegram verifies the sender, hands the command or button to
# the lodge, and renders the neutral reply as text and inline keyboards.

MANIFEST = Manifest(
    name="telegram",
    capabilities=frozenset({"text", "files", "voice_in", "voice_out", "images", "edit", "reply_to", "status"}),
    command_input=frozenset({"native", "buttons"}),
    events=frozenset({"message", "command", "selection", "attachment"}),
    route_fields=("chat_id",),
    secrets=("TELEGRAM_BOT_TOKEN",),
    owner_env="TELEGRAM_ALLOWED_USERS",
)

# Telegram's own ways to talk to a wolt, listed under /help.
HELP_TIPS = (
    "• Send a message → goes to active session (or starts one)",
    "• Reply to a wolt's message → routes to that wolt",
    "• @dog → ask the dog for help",
)

WOLT_TYPE_EMOJI = commands.WOLT_TYPE_EMOJI
RODENT_WOLT_TYPES = commands.RODENT_WOLT_TYPES
_NO_WOLTS_MESSAGE = commands.NO_WOLTS_MESSAGE
_PICK_THEN_RESEND = (
    "your message was not delivered: pick a wolt above (or send /wolt <name>), "
    "then send it again."
)
_IDLE_CHOICES = commands.IDLE_CHOICES
_IDLE_LABELS = commands.IDLE_LABELS
_wolt_of = commands.wolt_of
_clock = commands.clock
_is_wolt = commands.is_chat_wolt
_idle_text = commands.idle_text


def _ports() -> commands.Ports:
    """The lodge as the commands see it. Each name resolves at call time."""
    return commands.Ports(
        list_wolts=lambda: list_wolts(),
        list_sessions=lambda: list_sessions(),
        tunnel_url=lambda: get_tunnel_url(),
        wolt_exists=lambda name: (_WOLTS_DIR / name / "wolt").is_dir(),
        shown=lambda name: _shown(name),
        session_emoji=lambda name: _session_emoji(name),
        load_chat=lambda chat: _load_chat_state(chat),
        save_chat=lambda chat, state: _save_chat_state(chat, state),
        forget_session=lambda name: _forget_session(name),
        kill_session=lambda name: kill_session(name),
        spawn_session=lambda wolt, chat: _spawn_session(wolt, chat),
        dog_available=lambda: dog_available(),
        dog_name=lambda: _dog_name(),
        fetch_apps=lambda: _fetch_apps(),
        app_action=lambda name, action: _app_action(name, action),
        release_hold=lambda name: _release_hold(name),
        idle_setting=lambda body=None: _idle_setting(body),
        log_error=lambda message: logger.error(message),
    )


# --- rendering ---------------------------------------------------------------

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


def _rendered(reply: Reply) -> tuple[str, InlineKeyboardMarkup | None]:
    text, kwargs = _render(reply)
    return text, kwargs.get("reply_markup")


# The builders, as Telegram renders them (kept for callers and tests).

def _wolt_picker_keyboard(wolts: list, active: str | None) -> InlineKeyboardMarkup | None:
    return _keyboard(commands.wolt_picker_choices(_ports(), wolts, active))


def _wolt_picker_header(wolts: list, active: str | None) -> str:
    return commands.wolt_picker(_ports(), wolts, active).text


def _sessions_picker_message(sessions: list, tunnel_url: str | None):
    return _rendered(commands.sessions_picker(_ports(), sessions, tunnel_url))


def _holds_message(sessions: list, tunnel_url: str | None):
    return _rendered(commands.holds_list(_ports(), sessions, tunnel_url))


def _apps_picker_message(apps: list):
    return _rendered(commands.apps_list(apps))


def _idle_keyboard(current) -> InlineKeyboardMarkup:
    return _keyboard(commands.idle_choices(current))


def _set_active_wolt(chat_id: int, name: str) -> None:
    commands.set_active_wolt(_ports(), chat_id, name)


def _session_emoji(session_name: str) -> str:
    wolt = _wolt_of(session_name)
    types_by_wolt = {commands.wolt_name(w): w.get("type", "") for w in list_wolts()}
    return WOLT_TYPE_EMOJI.get(types_by_wolt.get(wolt, ""), "🦫")


# --- the lodge's services, over its API ----------------------------------------

def _api() -> str:
    """The control plane that spawned this adapter — see WolfConnector/notify."""
    return os.environ.get("WOLTSPACE_API", "http://localhost:7777")


async def _fetch_apps() -> list[dict]:
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{_api()}/apps", timeout=5)
        resp.raise_for_status()
        return resp.json()


async def _app_action(name: str, action: str) -> bool:
    """POST /apps/{name}/{start|stop} via the local server."""
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(f"{_api()}/apps/{name}/{action}", timeout=15)
            return resp.status_code < 400
        except Exception:
            return False


async def _release_hold(name: str) -> bool:
    """DELETE /sessions/{name}/hold on the lodge. The session rests at the next
    sweep only if nobody is using it; nothing is stopped now."""
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.delete(f"{_api()}/sessions/{name}/hold", timeout=15)
            return resp.status_code < 400
        except Exception:
            return False


async def _idle_setting(body: dict | None = None) -> tuple[int, dict]:
    """GET, or with a body POST, /settings/session-expiry on the lodge."""
    import httpx
    url = f"{_api()}/settings/session-expiry"
    async with httpx.AsyncClient() as client:
        resp = await (client.get(url, timeout=10) if body is None
                      else client.post(url, json=body, timeout=10))
    try:
        data = resp.json()
    except Exception:
        data = {}
    return resp.status_code, data if isinstance(data, dict) else {}


# --- handlers ----------------------------------------------------------------

async def _command(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    """One owner command: the lodge runs it, Telegram says what it answered."""
    if not is_allowed(update):
        return
    reply = await commands.run_command(
        _ports(), update.effective_chat.id, name, list(context.args or []), tips=HELP_TIPS,
    )
    text, kwargs = _render(reply)
    await _reply(update, text, **kwargs)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command(update, context, "start")


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command(update, context, "help")


async def handle_setwolt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wolt [name] — show picker or set the active wolt for this chat."""
    await _command(update, context, "wolt")


async def handle_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /new [wolt-name] — spawn a fresh session immediately."""
    await _command(update, context, "new")


async def handle_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command(update, context, "sessions")


async def handle_holds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command(update, context, "holds")


async def handle_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /idle — show the lodge's idle window and pick another."""
    await _command(update, context, "idle")


async def handle_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _command(update, context, "kill")


async def handle_apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /apps — inline keyboard with open / start / stop per app."""
    await _command(update, context, "apps")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A button press: the lodge runs its action, Telegram acks and edits the message."""
    if not is_allowed(update):
        return
    query = update.callback_query
    data = query.data or ""
    chat_id = query.message.chat.id if query.message else update.effective_chat.id
    outcome = await commands.run_selection(_ports(), chat_id, data)
    ack = outcome.ack or Ack()
    kwargs = {} if ack.alert is None else {"show_alert": ack.alert}
    if ack.text is None:
        await query.answer(**kwargs)
    else:
        await query.answer(ack.text, **kwargs)
    if outcome.edit is not None:
        text, kwargs = _render(outcome.edit)
        try:
            await query.edit_message_text(text, **kwargs)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------

async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Global error handler — log and swallow TimedOut, re-raise others."""
    err = context.error
    if isinstance(err, TimedOut):
        chat = getattr(update, "effective_chat", None)
        logger.warning(f"Telegram TimedOut (swallowed, chat_id={getattr(chat, 'id', None)}): {err}")
        return
    logger.error(f"Unhandled error: {err}", exc_info=err)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

BOT_COMMANDS = [BotCommand(c.name, c.description) for c in commands.COMMANDS]


async def _register_commands(app):
    """Publish the slash-command menu so Telegram's '/' autocomplete is populated."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        logger.info(f"registered {len(BOT_COMMANDS)} bot commands + commands menu button")
    except Exception as e:
        logger.warning(f"failed to register bot commands: {e}")


def run():
    """Start the Telegram bot (v2 — chat-per-wolt model)."""
    load_allowed_users()
    token = os.environ["TELEGRAM_BOT_TOKEN"]

    app = ApplicationBuilder().token(token).build()
    app.add_error_handler(_error_handler)
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("sessions", handle_sessions))
    app.add_handler(CommandHandler("holds", handle_holds))
    app.add_handler(CommandHandler("idle", handle_idle))
    app.add_handler(CommandHandler("kill", handle_kill))
    app.add_handler(CommandHandler("wolt", handle_setwolt))
    app.add_handler(CommandHandler("new", handle_new))
    app.add_handler(CommandHandler("apps", handle_apps))
    app.add_handler(CommandHandler("help", handle_help))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION | filters.VIDEO_NOTE | filters.Document.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE & ~filters.Document.VIDEO, handle_document))

    wolt_name = get_env("WOLTSPACE_WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} telegram v2 bot starting (chat-per-wolt model)...")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def _run():
        async with app:
            await _register_commands(app)
            await app.start()
            await app.updater.start_polling()
            await asyncio.Event().wait()

    loop.run_until_complete(_run())


if __name__ == "__main__":
    run()
