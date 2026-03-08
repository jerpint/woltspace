"""
Slack adapter — thin layer over core.
Responds to @mentions, reads full thread context, follows up in threads.
Uses Socket Mode (no public HTTP endpoint needed).
"""

import os
import re
import json
import logging
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from bot.core import get_response, list_sessions, kill_session, get_tunnel_url, switch_wolt, list_wolts, read_session_routing, _bot_log

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.environ["BOT_ADAPTER"] = "slack"

WOLT_DIR = Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))
STATE_DIR = WOLT_DIR / ".state"
CHAT_DIR = STATE_DIR / "chat" / "slack"

MAX_HISTORY = 20

ACTIVE_THREADS_FILE = CHAT_DIR / "_active_threads.json"


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


_active_threads: set[str] = _load_active_threads()


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
    return messages


def _strip_mention(text: str, bot_user_id: str) -> str:
    """Remove <@BOT_ID> from message text."""
    return re.sub(rf"<@{bot_user_id}>", "", text).strip()


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


def format_response(result: dict) -> str:
    """Format a core response dict for Slack."""
    if result["type"] == "session":
        s = result["session"]
        if s["url"]:
            return f"On it — started a session.\n\n{s['url']}\n\n(`{s['name']}`)"
        return f"Started session `{s['name']}` but couldn't find tunnel URL."
    return result["text"]


def create_app():
    """Create and configure the Slack Bolt app."""
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.event("app_mention")
    async def handle_mention(event, client, context):
        """Handle @mentions — respond in thread, read full thread context."""
        channel = event["channel"]
        user = event.get("user", "unknown")
        text = event.get("text", "")
        # Thread is either existing or starts from this message
        thread_ts = event.get("thread_ts", event["ts"])
        msg_ts = event["ts"]

        bot_user_id = context.get("bot_user_id", "")
        user_message = _strip_mention(text, bot_user_id)

        if not user_message:
            return

        logger.info(f"Mention from user={user} in channel={channel} thread={thread_ts}")

        # Mark thread as active
        _active_threads.add(_thread_key(channel, thread_ts))
        _save_active_threads()

        # Build context from full thread
        history = await _build_thread_context(client, channel, thread_ts, bot_user_id)
        # Drop the last user message from thread context — we'll add it explicitly
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        routing = {"adapter": "slack", "channel": channel, "thread_ts": thread_ts}
        try:
            result = get_response(user_message, conversation_history=list(history), routing=routing)
        except Exception as e:
            logger.error(f"Error getting response: {e}")
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="Something broke on my end. Try again in a sec.")
            return

        response = format_response(result)

        # Persist to disk
        _append_history(channel, thread_ts, "user", user_message)
        _append_history(channel, thread_ts, "assistant", response)

        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=response)

    @app.event("message")
    async def handle_message(event, client, context):
        """Handle follow-up messages in active threads (no @mention needed)."""
        # Skip bot messages, edits, and subtypes we don't care about
        if event.get("bot_id") or event.get("subtype"):
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts")

        # Only respond in threads where we're already active
        if not thread_ts:
            return
        if _thread_key(channel, thread_ts) not in _active_threads:
            return

        user = event.get("user", "unknown")
        text = event.get("text", "")
        bot_user_id = context.get("bot_user_id", "")
        user_message = _strip_mention(text, bot_user_id)

        if not user_message:
            return

        # Skip if this is an @mention (already handled by handle_mention)
        if bot_user_id and f"<@{bot_user_id}>" in (event.get("text", "")):
            return

        logger.info(f"Thread follow-up from user={user} in channel={channel} thread={thread_ts}")

        # Build context from full thread
        history = await _build_thread_context(client, channel, thread_ts, bot_user_id)
        if history and history[-1]["role"] == "user":
            history = history[:-1]

        routing = {"adapter": "slack", "channel": channel, "thread_ts": thread_ts}
        try:
            result = get_response(user_message, conversation_history=list(history), routing=routing)
        except Exception as e:
            logger.error(f"Error getting response: {e}")
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                          text="Something broke on my end. Try again in a sec.")
            return

        response = format_response(result)
        if result["type"] == "session":
            _session_thread_map[result["session"]["name"]] = (channel, thread_ts)

        _append_history(channel, thread_ts, "user", user_message)
        _append_history(channel, thread_ts, "assistant", response)

        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=response)

    return app


RESULTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts")) / ".state" / "task-results"


async def _watch_task_results(client):
    """Background task: watch for completed sessions and notify via Slack."""
    notified: set[str] = set()
    # Fallback channel from env
    fallback_channel = os.environ.get("SLACK_NOTIFY_CHANNEL", "")

    while True:
        await asyncio.sleep(5)
        if not RESULTS_DIR.exists():
            continue
        for f in RESULTS_DIR.glob("*.json"):
            if f.stem in notified:
                continue
            try:
                data = json.loads(f.read_text())
                session = data.get("session", f.stem)
                event_type = data.get("type", "done")

                # Check routing — only handle slack sessions
                routing = read_session_routing(session)
                if routing and routing.get("adapter") != "slack":
                    continue

                # Find where to notify
                if routing and routing.get("channel"):
                    channel = routing["channel"]
                    thread_ts = routing.get("thread_ts")
                elif fallback_channel:
                    channel, thread_ts = fallback_channel, None
                else:
                    notified.add(f.stem)
                    continue

                message = data.get("message", data.get("output", ""))
                if len(message) > 2000:
                    message = message[:2000] + "..."

                if event_type == "notification":
                    msg = f"[`{session}`] {message}"
                else:
                    tunnel_url = get_tunnel_url()
                    msg = f"[`{session}`] {message}"
                    if tunnel_url:
                        msg += f"\n\n{tunnel_url}/tui?session={session}"

                kwargs = {"channel": channel, "text": msg}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                await client.chat_postMessage(**kwargs)
                _bot_log("notify_sent", {"session": session, "type": event_type, "channel": channel})

                notified.add(f.stem)
                f.unlink(missing_ok=True)
            except Exception as e:
                logger.error(f"Error reading task result {f}: {e}")


def run():
    """Start the Slack bot with Socket Mode."""
    app = create_app()
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} slack bot starting (socket mode)...")

    async def _run():
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        # Start task result watcher
        asyncio.create_task(_watch_task_results(app.client))
        await handler.start_async()

    asyncio.run(_run())


if __name__ == "__main__":
    run()
