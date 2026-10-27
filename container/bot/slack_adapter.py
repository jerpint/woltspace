"""Owner-only Slack DM adapter over Socket Mode.

Thread ownership model:
  - owner starts a DM thread → dog responds
  - Dog spawns a session → thread becomes session-owned
  - Messages in session-owned thread → routed directly to Claude Code session
  - Dead session → ownership clears so the dog can recover in that thread
"""

import os
import re
import json
import base64
import logging
import asyncio
import random
import time
import urllib.request
import urllib.parse
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
import sys
from bot.core import (
    message_session, start_claude_session, _bot_log, build_ack_text, registry,
    _sanitize_history,
)
from wolts import get_active_creature, list_wolts, wolt_exists
from creature_types import base_type, emoji_from

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from env_compat import get_env
from paths import wolt_state_dir, wolt_chat_dir
from notify_prompt import notify_reply_instruction
import chat_state
import lodge_db

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

os.environ["BOT_ADAPTER"] = "slack"

WOLT_DIR = Path(get_env("WOLTSPACE_WOLT_DIR", "/workspace/wolt"))
_WOLT_NAME = get_env("WOLTSPACE_WOLT_NAME", WOLT_DIR.name)
_WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", str(WOLT_DIR.parent)))
STATE_DIR = wolt_state_dir(_WOLT_NAME, _WOLTS_DIR)
CHAT_DIR = wolt_chat_dir(_WOLT_NAME, _WOLTS_DIR) / "slack"

MAX_HISTORY = 20

CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶"}

# --- Thread ownership and the pending queue live in lodge.db ---
# chat_state rows for adapter "slack": a thread ("channel:root_ts") names the
# session that owns it; a user id names the wolt that owner last chose.

PENDING_TTL_SECONDS = 600
PENDING_MAX_PER_OWNER = 5
PENDING_MAX_BYTES = 32 * 1024


def _dog_name() -> str:
    """Get the dog's display name — from dog-wolt if available, else the active-wolt env var."""
    name = get_active_creature("dog")
    return name or get_env("WOLTSPACE_WOLT_NAME", "wolt")


RODENT_WOLT_TYPES = {"raccoon", "beaver", "otter", "rodent"}


def _remember_selection(user: str, name: str) -> None:
    chat_state.put(_WOLTS_DIR, "slack", user, {"active_wolt": name})


def _eligible_wolts() -> dict[str, dict]:
    eligible = {}
    for wolt in sorted(list_wolts(), key=lambda item: item.get("name", "").casefold()):
        name = wolt.get("name", "")
        if name and base_type(wolt.get("type")) in RODENT_WOLT_TYPES:
            eligible[name] = wolt
    return eligible


def _selected_wolt(user: str) -> dict | None:
    name = chat_state.get(_WOLTS_DIR, "slack", user).get("active_wolt")
    if not name:
        return None
    selected = _eligible_wolts().get(name)
    if selected is not None:
        return selected
    chat_state.delete(_WOLTS_DIR, "slack", user)
    return None


def _picker_text(wolts: dict[str, dict], current: str = "") -> str:
    if not wolts:
        return "No eligible wolts are available. Create a rodent wolt in the lodge first."
    lines = ["Choose your wolt:"]
    if current:
        lines.append(f"Current selection: `{current}`")
    lines.extend(f"{index}. {name}" for index, name in enumerate(wolts, 1))
    lines.append("\nIf the menu is unavailable, send `wolt <exact-name>`.")
    return "\n".join(lines)


def _picker_blocks(wolts: dict[str, dict]) -> list[dict]:
    # Slack static_select accepts at most 100 options. Text fallback above lists
    # the complete roster, so every eligible wolt remains reachable.
    options = [
        {"text": {"type": "plain_text", "text": name[:75]}, "value": name}
        for name in list(wolts)[:100]
    ]
    if not options:
        return []
    return [{
        "type": "actions",
        "elements": [{
            "type": "static_select",
            "action_id": "select_wolt",
            "placeholder": {"type": "plain_text", "text": "Choose your wolt"},
            "options": options,
        }],
    }]


def _text_selection(text: str) -> str | None:
    match = re.fullmatch(r"/?wolt\s+(\S+)\s*", text, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _picker_request(text: str) -> bool:
    return re.fullmatch(r"/?wolt\s*", text, flags=re.IGNORECASE) is not None


def _pending_create(user: str, channel: str, root_ts: str, event_id: str, text: str,
                    *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not text or len(text.encode("utf-8")) > PENDING_MAX_BYTES:
        raise ValueError("message must be 1..32768 UTF-8 bytes")
    record = {"user": user, "channel": channel, "root_ts": root_ts,
              "event_id": event_id, "text": text, "picker_ts": "",
              "state": "pending", "created_at": now,
              "expires_at": now + PENDING_TTL_SECONDS}
    with lodge_db.write(_WOLTS_DIR) as conn:
        (active,) = conn.execute(
            "SELECT count(*) FROM pending_messages WHERE adapter='slack' AND user=?"
            " AND state='pending' AND expires_at>?", (user, now)).fetchone()
        if active >= PENDING_MAX_PER_OWNER:
            raise ValueError("too many pending conversations")
        conn.execute(
            "INSERT OR REPLACE INTO pending_messages (id, adapter, user, channel, root_ts,"
            " picker_ts, event_id, text, state, created_at, expires_at)"
            " VALUES (:id, 'slack', :user, :channel, :root_ts, :picker_ts, :event_id,"
            " :text, :state, :created_at, :expires_at)",
            {"id": _thread_key(channel, root_ts), **record})
    return record


def _pending_set_picker(channel: str, root_ts: str, picker_ts: str) -> None:
    with lodge_db.write(_WOLTS_DIR) as conn:
        conn.execute("UPDATE pending_messages SET picker_ts=? WHERE id=? AND state='pending'",
                     (picker_ts, _thread_key(channel, root_ts)))


def _pending_claim(user: str, channel: str, picker_ts: str, wolt: str,
                   *, now: float | None = None) -> dict | None:
    now = time.time() if now is None else now
    with lodge_db.write(_WOLTS_DIR) as conn:
        row = conn.execute(
            "SELECT id, user, channel, root_ts, event_id, text, picker_ts, state,"
            " created_at, expires_at FROM pending_messages"
            " WHERE adapter='slack' AND user=? AND channel=? AND ? IN (picker_ts, root_ts)"
            " AND state='pending' ORDER BY created_at LIMIT 1",
            (user, channel, picker_ts)).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= now:
            conn.execute("UPDATE pending_messages SET state='expired', text=NULL"
                         " WHERE id=?", (row["id"],))
            return None
        # Clearing the text is the durable at-most-once boundary.
        conn.execute("UPDATE pending_messages SET state='claimed', wolt=?, text=NULL"
                     " WHERE id=?", (wolt, row["id"]))
    claimed = dict(row)
    del claimed["id"]
    return claimed


def _pending_finish(channel: str, root_ts: str, state: str, session: str = "") -> None:
    with lodge_db.write(_WOLTS_DIR) as conn:
        conn.execute("UPDATE pending_messages SET state=?, session=?, text=NULL WHERE id=?",
                     (state, session, _thread_key(channel, root_ts)))


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

SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{8,}$")
_SEEN_EVENT_LIMIT = 2048
_seen_event_ids: set[str] = set()
_seen_event_order: deque[str] = deque()


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


def _owner_user_id() -> str | None:
    """Return the explicitly configured owner, never an inferred user."""
    value = os.environ.get("SLACK_OWNER_USER", "").strip()
    return value if SLACK_USER_ID_RE.fullmatch(value) else None


def _event_identity(body: dict, event: dict) -> str:
    return str(
        body.get("event_id")
        or event.get("client_msg_id")
        or event.get("event_ts")
        or event.get("ts")
        or ""
    )


def _accept_owner_dm(event: dict, body: dict) -> tuple[bool, str]:
    """Fail closed before history, downloads, model calls, or session routing."""
    owner = _owner_user_id()
    if owner is None:
        return False, "owner_not_configured"
    if event.get("bot_id") or event.get("subtype"):
        return False, "non_human_event"
    if event.get("channel_type") != "im":
        return False, "not_dm"
    if event.get("user") != owner:
        return False, "not_owner"
    event_id = _event_identity(body, event)
    if not event_id:
        return False, "missing_event_id"
    if event_id in _seen_event_ids:
        return False, "duplicate"
    _seen_event_ids.add(event_id)
    _seen_event_order.append(event_id)
    if len(_seen_event_order) > _SEEN_EVENT_LIMIT:
        _seen_event_ids.discard(_seen_event_order.popleft())
    return True, "accepted"


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
    emoji = emoji_from(CREATURE_EMOJIS, creature, "")
    wolt = tc.get("args", {}).get("wolt", "")
    recipient = f"{emoji} {wolt}".strip()

    line = f"🪵 {recipient} — {name}" if recipient else f"🪵 {name}"

    url = tc.get("url", "")
    if url:
        line += f"\n{url}"

    return line


# --- Session ownership ---

def _get_session_owner(channel: str, thread_ts: str) -> dict | None:
    """Get session ownership info for a thread, or None if dog-owned.

    A thread whose wolt is gone (archived, or removed by hand) is unowned.
    """
    state = chat_state.get(_WOLTS_DIR, "slack", _thread_key(channel, thread_ts))
    if not state.get("active_session") or not wolt_exists(state.get("active_wolt", ""), _WOLTS_DIR):
        return None
    owner = {"session": state.pop("active_session"), "wolt": state.pop("active_wolt")}
    return {**owner, **state}


def _set_session_owner(channel: str, thread_ts: str, session_name: str, wolt_name: str,
                       creature: str, session_link: str = ""):
    """Mark a thread as owned by a session."""
    chat_state.put(_WOLTS_DIR, "slack", _thread_key(channel, thread_ts), {
        "active_session": session_name,
        "active_wolt": wolt_name,
        "creature": creature,
        "session_link": session_link,
    })


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

async def _post_text(client, channel: str, thread_ts: str, user: str, text: str):
    """Prefer Slack's agent stream surface, with a plain-message fallback."""
    try:
        started = await client.chat_startStream(
            channel=channel,
            thread_ts=thread_ts,
            recipient_user_id=user,
            markdown_text=text,
        )
        stream_ts = started.get("ts")
        if not stream_ts:
            raise RuntimeError("chat.startStream returned no message timestamp")
        await client.chat_stopStream(channel=channel, ts=stream_ts)
        return
    except Exception as exc:
        logger.info("Slack streaming unavailable; using chat.postMessage: %s", exc)
    await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _session_link(session: dict) -> str:
    """Return only an exact platform-created HTTPS link for this session."""
    url = session.get("url") or ""
    name = session.get("name") or ""
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    except (TypeError, ValueError):
        return ""
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path != "/tui" or parsed.fragment or
            query != {"session": [name]}):
        return ""
    return url


async def _set_agent_status(client, channel: str, thread_ts: str,
                            status: str, *, title: str = "") -> bool:
    """Set the supported native Slack Agent Session status."""
    try:
        arguments = {
            "channel_id": channel,
            "thread_ts": thread_ts,
            "status": status,
        }
        if title:
            arguments["title"] = title
        response = await client.agents_sessions_setStatus(
            **arguments,
        )
        return bool(response.get("ok", True))
    except Exception as exc:
        logger.error("Slack native agent status unavailable: %s", exc)
        return False


async def _spawn_pending(client, selected: dict, claimed: dict) -> None:
    channel, root_ts = claimed["channel"], claimed["root_ts"]
    picker_ts, user = claimed["picker_ts"], claimed["user"]
    await client.chat_update(
        channel=channel, ts=picker_ts,
        text=f"✅ Accepted. 🌱 Starting {selected['name']}…", blocks=[],
    )
    routing = {"adapter": "slack", "chat_id": channel, "thread_ts": root_ts}
    try:
        session = await asyncio.to_thread(
            start_claude_session, claimed["text"], wolt=selected["name"],
            creature=selected.get("type", ""), routing=routing,
        )
    except Exception:
        _pending_finish(channel, root_ts, "failed")
        await client.chat_delete(channel=channel, ts=picker_ts)
        logger.exception("Error starting selected Slack wolt")
        return
    session_link = _session_link(session)
    _set_session_owner(
        channel, root_ts, session["name"], selected["name"], selected.get("type", ""),
        session_link,
    )
    _pending_finish(channel, root_ts, "consumed", session["name"])
    link_suffix = f"  <{session_link}|Open session>" if session_link else ""
    if await _set_agent_status(
        client, channel, root_ts, "processing", title=session["name"]
    ):
        await asyncio.to_thread(
            registry.update, session["name"], wolt=selected["name"],
            slack_progress_mode="agent", slack_progress_ts="",
            slack_session_link=session_link,
        )
        await client.chat_update(
            channel=channel, ts=picker_ts,
            text=f"🌱 {selected['name']} session ready.{link_suffix}", blocks=[],
        )
        return
    await asyncio.to_thread(
        registry.update, session["name"], wolt=selected["name"],
        slack_progress_mode="", slack_progress_ts="",
        slack_session_link=session_link,
    )
    await client.chat_update(
        channel=channel, ts=picker_ts,
        text=(f"🌱 {selected['name']} session ready, but Slack Agent View is not "
              f"authorized. Reinstall with `assistant:write`.{link_suffix}"),
        blocks=[],
    )


async def _post_result(client, channel: str, thread_ts: str, user: str, result: dict):
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
        await _post_text(client, channel, thread_ts, user, format_response(result))

    # If a session was spawned, take ownership of the thread
    session_info = _extract_session_info(result)
    if session_info:
        _set_session_owner(channel, thread_ts, session_info["session"], session_info["wolt"], session_info["creature"])
        emoji = emoji_from(CREATURE_EMOJIS, session_info["creature"], "🐾")
        wolt = session_info["wolt"]
        session_link = result.get("session", {}).get("url") or ""
        handoff_text = f"{emoji} {wolt} is now active in this thread — messages here go directly to this session"
        if session_link:
            handoff_text += f"\n{session_link}"
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=handoff_text,
        )


async def _route_to_session(client, channel: str, thread_ts: str, owner: dict, text: str):
    """Route a message directly to a Claude Code session."""
    session_name = owner["session"]
    wolt = owner["wolt"]
    creature = owner["creature"]
    session_link = owner.get("session_link", "")
    link_suffix = f"  <{session_link}|Open session>" if session_link else ""

    native_agent_status = await _set_agent_status(
        client, channel, thread_ts, "processing"
    )
    if native_agent_status:
        await asyncio.to_thread(
            registry.update, session_name, wolt=wolt,
            slack_progress_mode="agent", slack_progress_ts="",
            slack_session_link=session_link,
        )
    else:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=("Slack Agent View is not authorized. Reinstall with "
                  f"`assistant:write` before retrying.{link_suffix}"),
        )
        return

    session_msg = (
        f"[slack message from human, channel={channel}, thread={thread_ts}]: {text}\n"
        f"{notify_reply_instruction('--slack', channel, thread_ts)}"
    )
    # Blocking resume wait — off the event loop, or the whole Slack app stops
    # answering while one session boots.
    result = await asyncio.to_thread(message_session, session_name, session_msg)
    _bot_log("slack_session_route", {"session": session_name, "text": text[:200], "result": result})

    _append_history(channel, thread_ts, "user", text)

    if result.get("ok"):
        refreshed_link = _session_link({
            "name": session_name,
            "url": result.get("url") or "",
        })
        if refreshed_link and refreshed_link != session_link:
            session_link = refreshed_link
            _set_session_owner(
                channel, thread_ts, session_name, wolt, creature, session_link
            )
            await asyncio.to_thread(
                registry.update, session_name, wolt=wolt,
                slack_session_link=session_link,
            )
        _append_message(channel, thread_ts, {
            "role": "assistant",
            "content": f"[delivered to session {session_name}]",
        })
    else:
        error = result.get("error", "unknown error")
        await _set_agent_status(client, channel, thread_ts, "active")
        await asyncio.to_thread(
            registry.update, session_name, wolt=wolt,
            slack_progress_mode="", slack_progress_ts="",
        )
        # Return the thread to the dog so the next owner message can recover.
        chat_state.delete(_WOLTS_DIR, "slack", _thread_key(channel, thread_ts))
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"session {session_name} is no longer active — send again to start a new conversation",
        )
        _append_message(channel, thread_ts, {
            "role": "assistant",
            "content": f"[session {session_name} dead: {error}]",
        })


# --- Main app ---

def create_app():
    """Create the owner-only DM app; channel mentions are intentionally absent."""
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])

    @app.action("select_wolt")
    async def handle_select_wolt(ack, body, client):
        await ack()
        owner = _owner_user_id()
        user = (body.get("user") or {}).get("id")
        channel = (body.get("channel") or {}).get("id", "")
        actions = body.get("actions")
        action = actions[0] if isinstance(actions, list) and len(actions) == 1 else {}
        option = action.get("selected_option") if isinstance(action, dict) else None
        name = option.get("value") if isinstance(option, dict) else None
        picker_ts = (body.get("message") or {}).get("ts", "")
        if user != owner or not channel.startswith("D") or not isinstance(name, str):
            logger.info("Ignoring unauthorized or malformed Slack wolt selection")
            return
        selected = _eligible_wolts().get(name)
        if selected is None:
            logger.info("Ignoring stale Slack wolt selection")
            await client.chat_postMessage(
                channel=channel,
                text="That wolt is no longer available. Send any DM to choose again.",
            )
            return
        claimed = _pending_claim(user, channel, picker_ts, name)
        if claimed is None:
            logger.info("Ignoring expired, duplicate, or unbound Slack selection")
            return
        _remember_selection(user, name)
        await _spawn_pending(client, selected, claimed)

    @app.event("message")
    async def handle_message(event, client, context, body):
        """Start or continue one owner DM thread."""
        accepted, reason = _accept_owner_dm(event, body)
        if not accepted:
            logger.info("Ignoring Slack event: %s", reason)
            return

        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        user = event["user"]
        text = event.get("text", "")
        bot_user_id = context.get("bot_user_id", "")
        user_message = _strip_mention(text, bot_user_id)

        # --- Session-owned thread: route directly to session ---
        owner = _get_session_owner(channel, thread_ts)
        if owner:
            logger.info(f"Session route: user={user} → {owner['session']} in thread={thread_ts}")
            await _route_to_session(client, channel, thread_ts, owner, user_message)
            return

        requested = _text_selection(user_message)
        if event.get("thread_ts") and requested is not None:
            selected = _eligible_wolts().get(requested)
            if selected:
                claimed = _pending_claim(user, channel, thread_ts, requested)
                if claimed:
                    await _spawn_pending(client, selected, claimed)
            return

        if event.get("thread_ts"):
            return  # unowned/stale thread never becomes a fresh conversation

        if event.get("files"):
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text="Attachments are not supported by the wolt picker yet. Send a text-only message.",
            )
            return

        wolts = _eligible_wolts()
        current = _selected_wolt(user)
        if _picker_request(user_message):
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=_picker_text(wolts, current.get("name", "") if current else ""),
                blocks=_picker_blocks(wolts),
            )
            return
        try:
            _pending_create(
                user, channel, thread_ts,
                str(body.get("event_id") or event.get("event_ts") or event["ts"]),
                user_message,
            )
        except ValueError as exc:
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=str(exc))
            return
        posted = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_picker_text(wolts, current.get("name", "") if current else ""),
            blocks=_picker_blocks(wolts),
        )
        if posted.get("ts"):
            _pending_set_picker(channel, thread_ts, posted["ts"])

    return app


def run():
    """Start the Slack bot with Socket Mode."""
    app = create_app()
    wolt_name = get_env("WOLTSPACE_WOLT_NAME", "wolt")
    logger.info(f"{wolt_name} slack bot starting (socket mode)...")

    async def _run():
        handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
        await handler.start_async()

    asyncio.run(_run())


if __name__ == "__main__":
    run()
