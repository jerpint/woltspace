"""
Slack adapter — thin layer over core.
Responds to @mentions, reads full thread context, follows up in threads.
Uses Socket Mode (no public HTTP endpoint needed).

Thread ownership model:
  - @bot in channel → new thread, dog (Haiku) responds
  - Dog spawns a session → thread becomes session-owned
  - Messages in session-owned thread → routed directly to Claude Code session
  - @bot in session-owned thread → escape hatch back to dog
  - Dead session → error message, @bot to recover
"""

import os
import re
import json
import base64
import logging
import asyncio
import random
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
import sys
from bot.core import get_response, message_session, list_sessions, kill_session, get_tunnel_url, switch_wolt, list_wolts, _bot_log, build_ack_text, _sanitize_history
from wolts import get_active_creature

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from paths import wolt_state_dir, wolt_chat_dir

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.environ["BOT_ADAPTER"] = "slack"

WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
_WOLT_NAME = os.environ.get("WOLT_NAME", WOLT_DIR.name)
_WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", str(WOLT_DIR.parent)))
STATE_DIR = wolt_state_dir(_WOLT_NAME, _WOLTS_DIR)
CHAT_DIR = wolt_chat_dir(_WOLT_NAME, _WOLTS_DIR) / "slack"

MAX_HISTORY = 20

CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶"}

# --- Thread ownership persistence ---

ACTIVE_THREADS_FILE = CHAT_DIR / "_active_threads.json"
THREAD_SESSIONS_FILE = CHAT_DIR / "_thread_sessions.json"


def _dog_name() -> str:
    """Get the dog's display name — from dog-wolt if available, else WOLT_NAME."""
    name = get_active_creature("dog")
    return name or os.environ.get("WOLT_NAME", "wolt")


def _load_active_threads() -> set[str]:
    """Load active threads from disk."""
    if ACTIVE_THREADS_FILE.exists():
        try:
            return set(json.loads(ACTIVE_THREADS_FILE.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return set()


def _save_active_threads():
    """Persist active threads to disk."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_THREADS_FILE.write_text(json.dumps(list(_active_threads)))


def _load_thread_sessions() -> dict[str, dict]:
    """Load thread → session ownership map from disk.

    Each entry: thread_key → {"session": name, "wolt": wolt_name, "creature": type}
    """
    if THREAD_SESSIONS_FILE.exists():
        try:
            return json.loads(THREAD_SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_thread_sessions():
    """Persist thread → session map to disk."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    THREAD_SESSIONS_FILE.write_text(json.dumps(_thread_sessions))


_active_threads: set[str] = _load_active_threads()
_thread_sessions: dict[str, dict] = _load_thread_sessions()


# --- Dog ack messages ---

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


# --- Helpers ---

def _thread_key(channel: str, thread_ts: str) -> str:
    return f"{channel}:{thread_ts}"


def _thread_file(channel: str, thread_ts: str) -> Path:
    safe_ts = thread_ts.replace(".", "_")
    return CHAT_DIR / f"{channel}_{safe_ts}.jsonl"


def _append_history(channel: str, thread_ts: str, role: str, content: str):
    """Append a single message to thread history on disk."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_thread_file(channel, thread_ts), "a") as f:
        f.write(json.dumps({"role": role, "content": content, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def _append_message(channel: str, thread_ts: str, msg: dict):
    """Append any message dict to disk (e.g. tool call entries with full structure)."""
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    with open(_thread_file(channel, thread_ts), "a") as f:
        f.write(json.dumps({**msg, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")


def _load_history(channel: str, thread_ts: str) -> list:
    """Load history from disk for a thread."""
    path = _thread_file(channel, thread_ts)
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


def _strip_mention(text: str, bot_user_id: str) -> str:
    """Remove <@BOT_ID> from message text."""
    return re.sub(rf"<@{bot_user_id}>", "", text).strip()


def _has_mention(text: str, bot_user_id: str) -> bool:
    """Check if text contains an @mention of the bot."""
    return bool(bot_user_id) and f"<@{bot_user_id}>" in text


def _extract_image(event: dict) -> tuple[bytes, str] | None:
    """Download the first image file attached to a Slack event, if any."""
    files = event.get("files", [])
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    for f in files:
        mime = f.get("mimetype", "")
        if not mime.startswith("image/"):
            continue
        url = f.get("url_private")
        if not url:
            continue
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read(), mime
        except Exception as e:
            logger.error(f"Slack image download failed: {e}")
    return None


def _image_content(image_bytes: bytes, mime_type: str, caption: str = "") -> list:
    """Build multimodal content list for an image."""
    b64 = base64.b64encode(image_bytes).decode()
    content = [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}]
    content.append({"type": "text", "text": caption if caption else "What's in this image?"})
    return content


async def _build_thread_context(client, channel: str, thread_ts: str, bot_user_id: str) -> list:
    """Fetch full thread from Slack API and build conversation history."""
    try:
        result = await client.conversations_replies(channel=channel, ts=thread_ts, limit=50)
        messages = result.get("messages", [])
    except Exception as e:
        logger.error(f"Failed to fetch thread: {e}")
        return []

    history = []
    for msg in messages:
        text = msg.get("text", "")
        if not text:
            continue
        text = _strip_mention(text, bot_user_id)
        if not text:
            continue

        if msg.get("user") == bot_user_id or msg.get("bot_id"):
            history.append({"role": "assistant", "content": text})
        else:
            history.append({"role": "user", "content": text})

    return history[-MAX_HISTORY * 2:]


# --- Response formatting ---

def format_response(result: dict) -> str:
    """Format a core response dict for Slack with emoji+name identity prefix."""
    if result["type"] == "session":
        if result.get("text"):
            text = result["text"]
        else:
            s = result["session"]
            text = build_ack_text(s.get("url"), s.get("name"), "slack", creature=s.get("creature"))
    elif result["type"] == "image":
        text = result.get("text", "") or result.get("caption", "") or result.get("filename", "image")
    else:
        text = result["text"]
    return f"🐶 {_dog_name()}: {text}"


def _format_tool_log(tc: dict) -> str:
    """Format a single tool call as a compact log line."""
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


# --- Session ownership ---

def _get_session_owner(channel: str, thread_ts: str) -> dict | None:
    """Get session ownership info for a thread, or None if dog-owned."""
    key = _thread_key(channel, thread_ts)
    return _thread_sessions.get(key)


def _set_session_owner(channel: str, thread_ts: str, session_name: str, wolt_name: str, creature: str):
    """Mark a thread as owned by a session."""
    key = _thread_key(channel, thread_ts)
    _thread_sessions[key] = {
        "session": session_name,
        "wolt": wolt_name,
        "creature": creature,
    }
    _save_thread_sessions()


def _clear_session_owner(channel: str, thread_ts: str):
    """Release thread ownership (back to dog)."""
    key = _thread_key(channel, thread_ts)
    _thread_sessions.pop(key, None)
    _save_thread_sessions()


def _extract_session_info(result: dict) -> dict | None:
    """Extract session name, wolt, and creature from a get_response result."""
    if result.get("type") != "session":
        return None
    session = result.get("session", {})
    name = session.get("name")
    if not name:
        return None
    wolt = session.get("wolt") or name.rsplit("-", 1)[0] if "-" in name else name
    creature = session.get("creature", "")
    return {"session": name, "wolt": wolt, "creature": creature}


# --- Posting results ---

async def _post_result(client, channel: str, thread_ts: str, result: dict):
    """Post a result to Slack — handles text, session, and image types.
    Sends tool call logs before the final response."""
    for tc in result.get("tool_calls_log", []):
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=_format_tool_log(tc),
            )
        except Exception:
            pass

    if result["type"] == "image":
        caption = result.get("text", "") or result.get("caption", "")
        with open(result["path"], "rb") as f:
            await client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                filename=result.get("filename", "image.png"),
                content=f.read(),
                initial_comment=caption or None,
            )
    else:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=format_response(result),
        )

    # If a session was spawned, take ownership of the thread
    session_info = _extract_session_info(result)
    if session_info:
        _set_session_owner(channel, thread_ts, session_info["session"], session_info["wolt"], session_info["creature"])
        emoji = CREATURE_EMOJIS.get(session_info["creature"], "🐾")
        wolt = session_info["wolt"]
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"{emoji} {wolt} is now active in this thread — messages here go directly to this session",
        )


async def _route_to_session(client, channel: str, thread_ts: str, owner: dict, text: str):
    """Route a message directly to a Claude Code session."""
    session_name = owner["session"]
    wolt = owner["wolt"]
    creature = owner["creature"]
    emoji = CREATURE_EMOJIS.get(creature, "🐾")

    session_msg = (
        f"[slack message from human]: {text}\n"
        f"Reply back to them with: notify \"your message\""
    )
    result = message_session(session_name, session_msg)
    _bot_log("slack_session_route", {"session": session_name, "text": text[:200], "result": result})

    _append_history(channel, thread_ts, "user", text)

    if result.get("ok"):
        session_link = result.get("url") or session_name
        if result.get("status") == "revived":
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"{emoji} {wolt}: → session had exited — revived and delivered\n{session_link}",
            )
        else:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"{emoji} {wolt}: → delivered to session {session_name}",
            )
        _append_message(channel, thread_ts, {
            "role": "assistant",
            "content": f"[delivered to session {session_name}]",
        })
    else:
        error = result.get("error", "unknown error")
        # Session is dead — tell user how to recover
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"session {session_name} is no longer active — @mention me to start a new conversation",
        )
        _append_message(channel, thread_ts, {
            "role": "assistant",
            "content": f"[session {session_name} dead: {error}]",
        })


# --- Main app ---

def create_app():
    """Create and configure the Slack Bolt app."""
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def handle_mention(event, client, context):
        """Handle @mentions — always goes to dog (Haiku).

        This is the entry point for new conversations and the escape hatch
        from session-owned threads.
        """
        channel = event["channel"]
        user = event.get("user", "unknown")
        text = event.get("text", "")
        thread_ts = event.get("thread_ts", event["ts"])

        bot_user_id = context.get("bot_user_id", "")
        user_message = _strip_mention(text, bot_user_id)

        # Check for image attachment
        image_result = await asyncio.get_event_loop().run_in_executor(None, _extract_image, event)
        user_content = None
        if image_result:
            image_bytes, mime_type = image_result
            user_content = _image_content(image_bytes, mime_type, user_message)
            user_message = f"[image] {user_message}" if user_message else "[image]"

        if not user_message and user_content is None:
            return

        logger.info(f"Mention from user={user} in channel={channel} thread={thread_ts}")

        # If this thread was session-owned, release it back to dog
        owner = _get_session_owner(channel, thread_ts)
        if owner:
            _clear_session_owner(channel, thread_ts)
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=f"🐶 dog is back in this thread",
            )

        # Mark thread as active
        _active_threads.add(_thread_key(channel, thread_ts))
        _save_active_threads()

        # Immediate ack
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=random.choice(DOG_ACK_MESSAGES),
            )
        except Exception:
            pass

        # Build context from full thread
        history = await _build_thread_context(client, channel, thread_ts, bot_user_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        routing = {"adapter": "slack", "channel": channel, "thread_ts": thread_ts}
        try:
            result = get_response(user_message, conversation_history=list(history), routing=routing, user_content=user_content)
        except Exception as e:
            logger.error(f"Error getting response: {e}")
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="Something broke on my end. Try again in a sec.")
            return

        _append_history(channel, thread_ts, "user", user_message)
        for msg in result["history_messages"]:
            _append_message(channel, thread_ts, msg)

        await _post_result(client, channel, thread_ts, result)

    @app.event("message")
    async def handle_message(event, client, context):
        """Handle follow-up messages in active threads.

        If thread is session-owned → route directly to Claude Code session.
        If thread is dog-owned → route to dog (Haiku).
        Skip if message contains @mention (handled by handle_mention).
        """
        if event.get("bot_id") or event.get("subtype"):
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts")

        if not thread_ts:
            return
        if _thread_key(channel, thread_ts) not in _active_threads:
            return

        user = event.get("user", "unknown")
        text = event.get("text", "")
        bot_user_id = context.get("bot_user_id", "")

        # Skip if this is an @mention (handled by handle_mention)
        if _has_mention(text, bot_user_id):
            return

        user_message = _strip_mention(text, bot_user_id)

        # Check for image attachment
        image_result = await asyncio.get_event_loop().run_in_executor(None, _extract_image, event)
        user_content = None
        if image_result:
            image_bytes, mime_type = image_result
            user_content = _image_content(image_bytes, mime_type, user_message)
            user_message = f"[image] {user_message}" if user_message else "[image]"

        if not user_message and user_content is None:
            return

        # --- Session-owned thread: route directly to session ---
        owner = _get_session_owner(channel, thread_ts)
        if owner:
            logger.info(f"Session route: user={user} → {owner['session']} in thread={thread_ts}")
            await _route_to_session(client, channel, thread_ts, owner, user_message)
            return

        # --- Dog-owned thread: route to Haiku ---
        logger.info(f"Thread follow-up from user={user} in channel={channel} thread={thread_ts}")

        # Immediate ack
        try:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=random.choice(DOG_ACK_MESSAGES),
            )
        except Exception:
            pass

        history = await _build_thread_context(client, channel, thread_ts, bot_user_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        routing = {"adapter": "slack", "channel": channel, "thread_ts": thread_ts}
        try:
            result = get_response(user_message, conversation_history=list(history), routing=routing, user_content=user_content)
        except Exception as e:
            logger.error(f"Error getting response: {e}")
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="Something broke on my end. Try again in a sec.")
            return

        _append_history(channel, thread_ts, "user", user_message)
        for msg in result["history_messages"]:
            _append_message(channel, thread_ts, msg)

        await _post_result(client, channel, thread_ts, result)

    return app


def run():
    """Start the Slack bot with Socket Mode."""
    app = create_app()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} slack bot starting (socket mode)...")

    async def _run():
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        await handler.start_async()

    asyncio.run(_run())


if __name__ == "__main__":
    run()
