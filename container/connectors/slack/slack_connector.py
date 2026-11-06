"""Slack, as a connector the lodge ships (RFC #617).

This process speaks Slack (Socket Mode, through slack_bolt) and nothing of the
lodge but its socket (connector_client). It takes only the owner's direct
messages, downloads their files into its own data dir, and hands each event to
the lodge, which decides what happens and answers with neutral replies; this
process renders them as Block Kit in the same DM. The chat is flat, as on
Telegram: one DM, one active wolt. A reply in a thread under one of this bot's
messages carries that message, so the lodge can route it to its wolt.
Notifications from wolts come back as `send` frames.

Slack runs no custom slash command inside a thread, so every command can also be
typed with "!" (`!wolt alpha`), anywhere; buttons cover the pickers.

Environment (set by the lodge when it starts this program):
  SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_OWNER_USER
  WOLTSPACE_CONNECTOR_SOCKET, WOLTSPACE_CONNECTOR_TOKEN
  WOLTSPACE_CONNECTOR_DATA     where uploads go (the lodge reads them from there)
  WOLTSPACE_CONNECTOR_OUTBOX   the only directory files are sent from
"""

import asyncio
import collections
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import connector_client
import connector_protocol as wire
import httpx
from connector_contract import Reply, parse_command
from slack_sdk.errors import SlackApiError, SlackClientError

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("slack_connector")

NAME = "slack"
DATA_DIR = Path(os.environ.get("WOLTSPACE_CONNECTOR_DATA")
                or Path(os.environ.get("WOLTSPACE_WOLTS_DIR", "/workspace/wolts")) / ".space" / NAME)
UPLOADS_DIR = DATA_DIR / "uploads"
MAX_FILE_BYTES = 50 * 1024 * 1024
PREFIX = "!"
FILE_HOST = "files.slack.com"  # where url_private points

# The link to the lodge (a connector_client.Client, or a stand-in in tests), and to Slack (a SlackAPI).
CLIENT: Any = None
API: Any = None
# This bot's own ids, from auth.test: only its messages carry a footer worth honoring.
BOT = {"user_id": "", "bot_id": ""}
# The commands the lodge offers (its welcome), typed with PREFIX or registered as slash commands.
COMMANDS: tuple[str, ...] = ()
# Where each event's answers go: its DM, and for a click the message holding the button. Bounded.
PENDING: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
MAX_PENDING = 1000
# What a call to Slack can fail with: its answer, the network, a timeout.
SLACK_ERRORS = (SlackClientError, httpx.HTTPError, OSError, TimeoutError, ValueError)
# Slack resends an event it thinks was not received: each is handed in once.
_SEEN: "collections.OrderedDict[str, None]" = collections.OrderedDict()


def owner() -> str:
    return os.environ.get("SLACK_OWNER_USER", "").strip()


def _owner_dm(user, channel) -> bool:
    """The configured owner, in a direct message (D...): nothing else reaches the lodge."""
    return bool(owner()) and user == owner() and isinstance(channel, str) and channel.startswith("D")


class SlackAPI:
    """The few Web API calls this connector makes; tests give a fake with the same methods."""

    def __init__(self, client, token: str):
        self.client, self.token = client, token

    async def post(self, channel: str, text: str, blocks=None) -> str:
        sent = await self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)
        return str(sent.get("ts") or "")

    async def update(self, channel: str, ts: str, text: str, blocks=None) -> None:
        await self.client.chat_update(channel=channel, ts=ts, text=text, blocks=blocks or [])

    async def ephemeral(self, channel: str, user: str, text: str) -> None:
        await self.client.chat_postEphemeral(channel=channel, user=user, text=text)

    async def parent(self, channel: str, ts: str) -> dict | None:
        found = await self.client.conversations_replies(channel=channel, ts=ts, limit=1)
        messages = found.get("messages") or []
        return messages[0] if messages else None

    async def download(self, url: str) -> bytes:
        # The bot token goes to Slack's own file host and nowhere else (httpx drops it on a redirect elsewhere).
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != FILE_HOST:
            raise ValueError(f"not a Slack file link: {parsed.scheme}://{parsed.hostname}")
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as http:
            got = await http.get(url, headers={"Authorization": f"Bearer {self.token}"})
            got.raise_for_status()
            return got.content

    async def upload(self, channel: str, path: str, filename: str, comment: str) -> None:
        await self.client.files_upload_v2(channel=channel, file=path, filename=filename,
                                          initial_comment=comment or None)


# --- from Slack -----------------------------------------------------------------------

def accept(event: dict, body: dict) -> str | None:
    """The event id of an owner DM to hand in, or None (logged): fail closed."""
    if not owner():
        reason = "owner_not_configured"
    elif event.get("bot_id") or event.get("subtype") not in (None, "file_share"):
        reason = "not_a_person"
    elif event.get("channel_type") != "im" or not _owner_dm(event.get("user"), event.get("channel")):
        reason = "not_the_owner_dm"
    else:
        event_id = str(body.get("event_id") or event.get("client_msg_id") or event.get("ts") or "")
        if not event_id:
            reason = "no_event_id"
        elif event_id in _SEEN:
            reason = "duplicate"
        else:
            _SEEN[event_id] = None
            while len(_SEEN) > MAX_PENDING:
                _SEEN.popitem(last=False)
            return event_id
    logger.info("Ignoring Slack event: %s", reason)
    return None


async def _send_event(event_id: str, kind: str, channel: str, body: dict, where: dict | None = None) -> str:
    PENDING.setdefault(event_id, {"channel": channel, **(where or {})})
    while len(PENDING) > MAX_PENDING:
        PENDING.popitem(last=False)
    status = await CLIENT.event(kind, {"chat_id": channel}, owner(), body, event_id)
    if status == "failed":
        try:
            await API.post(channel, "The lodge couldn't take that message right now. Please send it again.")
        except SLACK_ERRORS as exc:
            logger.warning(f"could not tell the owner the lodge refused the message: {exc}")
    return status


def plain(text: str) -> str:
    """Slack's stored mrkdwn as it was written: <url> and <url|label> as the url, then &lt; &gt; &amp;."""
    text = re.sub(r"<([^<>|]+)(?:\|[^<>]*)?>", r"\1", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def _reply_to(event: dict) -> dict | None:
    """In a thread: the message it hangs under, with whether this bot wrote it."""
    thread = event.get("thread_ts")
    if not thread or thread == event.get("ts"):
        return None
    try:
        parent = await API.parent(event["channel"], thread)
    except SLACK_ERRORS as exc:
        logger.warning(f"thread parent not read: {exc}")
        return None
    if not parent:
        return None
    from_bot = bool(BOT["bot_id"]) and parent.get("bot_id") == BOT["bot_id"] or (
        bool(BOT["user_id"]) and parent.get("user") == BOT["user_id"])
    found = {"from_bot": from_bot, "text": plain(str(parent.get("text") or ""))[:wire.MAX_TEXT], "voice": False}
    files = parent.get("files") or []
    if files and files[0].get("name"):
        found["file_name"] = str(files[0]["name"])[:512]
        found["voice"] = str(files[0].get("mimetype", "")).startswith("audio/")
    return found


def _media(mime: str) -> str:
    kind = mime.split("/", 1)[0]
    return {"audio": "voice", "image": "image", "video": "video"}.get(kind, "file")


def _save_upload(file_name: str, data: bytes) -> Path:
    """Save an upload in this connector's data dir, never over another one."""
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe = Path(file_name).name.replace("\x00", "") or "file"  # a file name is anyone's to choose
    dest = UPLOADS_DIR / f"{ts}_{safe}"
    stem, suffix, counter = dest.stem, dest.suffix, 1
    while dest.exists():
        dest = UPLOADS_DIR / f"{stem}_{counter}{suffix}"
        counter += 1
    dest.write_bytes(data)
    return dest


async def handle_message(event: dict, body: dict) -> None:
    """An owner DM: a typed command, files, or a message for the lodge to route."""
    event_id = accept(event, body)
    if event_id is None:
        return
    channel, text = event["channel"], event.get("text") or ""
    reply_to = await _reply_to(event)
    command = parse_command(text, COMMANDS, PREFIX) if not event.get("files") else None
    if command is not None:
        await _send_event(event_id, "command", channel,
                          {"name": command.name, "args": list(command.args)[:wire.MAX_ARGS]})
        return
    if not event.get("files"):
        await _send_event(event_id, "message", channel, {"text": text, "reply_to": reply_to})
        return
    for i, info in enumerate(event["files"]):
        name = str(info.get("name") or f"file_{info.get('id', i)}")
        mime = str(info.get("mimetype") or "application/octet-stream")
        url = info.get("url_private_download") or info.get("url_private")
        try:
            if not url or int(info.get("size") or 0) > MAX_FILE_BYTES:
                raise ValueError("no download link, or larger than 50 MB")
            data = await API.download(url)
        except SLACK_ERRORS as exc:
            logger.error(f"Slack file not downloaded: {exc}")
            await API.post(channel, f"I couldn't get {name} from Slack. Please send it again.")
            continue
        saved = _save_upload(name, data)
        await _send_event(event_id if i == 0 else f"{event_id}-{i}", "attachment", channel, {
            "media": _media(mime), "path": str(saved), "name": name, "mime": mime, "size": len(data),
            "caption": text if i == 0 else "", "reply_to": reply_to,
        })


async def handle_command(command: dict) -> None:
    """A slash command (acknowledged by the caller first: Slack waits 3 seconds at most)."""
    channel = command.get("channel_id")
    if not _owner_dm(command.get("user_id"), channel):
        return
    name = str(command.get("command") or "").lstrip("/").lower()
    if name not in COMMANDS:
        return
    args = str(command.get("text") or "").split()[:wire.MAX_ARGS]
    await _send_event(f"cmd-{command.get('trigger_id') or os.urandom(8).hex()}", "command", str(channel),
                      {"name": name, "args": args})


async def handle_action(body: dict) -> None:
    """A button press (acknowledged by the caller first): the lodge runs its action."""
    channel = (body.get("channel") or {}).get("id")
    actions = body.get("actions") or []
    if not _owner_dm((body.get("user") or {}).get("id"), channel) or len(actions) != 1:
        return
    value = actions[0].get("value")
    if not isinstance(value, str) or not value:
        return  # a link button: Slack opened it
    message_ts = (body.get("message") or {}).get("ts", "")
    event_id = f"act-{actions[0].get('action_ts') or body.get('trigger_id') or os.urandom(8).hex()}"
    await _send_event(event_id, "selection", str(channel), {"action": value}, {"ts": message_ts})


# Bolt's listeners. A command or a click is acknowledged before anything else: Slack gives up after 3 s.

async def on_message(event, body):
    await handle_message(event, body)


async def on_command(ack, command):
    await ack()
    await handle_command(command)


async def on_action(ack, body):
    await ack()
    await handle_action(body)


# --- to Slack ---------------------------------------------------------------------------

def render(reply: Reply) -> tuple[str, list | None]:
    """Text and Block Kit: one section, then one actions row per row of choices."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", reply.text) if reply.markdown else reply.text
    if not reply.choices:
        return text, None
    blocks: list[dict] = [{"type": "section", "text": {"type": "mrkdwn" if reply.markdown else "plain_text",
                                           "text": text[:3000]}}] if text else []
    for i, row in enumerate(reply.choices):
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": c.label[:75]}, "action_id": f"c{i}-{j}",
             **({"url": c.url} if c.url else {"value": c.action})}
            for j, c in enumerate(row)]})
    return text, blocks


async def on_say(frame: dict) -> None:
    reply = wire.reply_from_wire(frame["reply"])
    channel = frame["route"]["chat_id"]
    if frame.get("photo"):
        await API.upload(channel, frame["photo"], Path(frame["photo"]).name, reply.text)
        return
    await API.post(channel, *render(reply))


async def on_edit(frame: dict) -> None:
    where = PENDING.get(frame["event"]) or {}
    if where.get("ts"):
        await API.update(where["channel"], where["ts"], *render(wire.reply_from_wire(frame["reply"])))


async def on_ack(frame: dict) -> None:
    """Slack has no toast: an ack with text is a message only the owner sees."""
    where = PENDING.get(frame["event"]) or {}
    ack = wire.ack_from_wire(frame["ack"])
    if where.get("channel") and ack.text:
        await API.ephemeral(where["channel"], owner(), ack.text)


def _outbox_file(path: str) -> Path:
    """A file the lodge staged for sending; nothing else on this machine goes out."""
    root = os.environ.get("WOLTSPACE_CONNECTOR_OUTBOX", "")
    if not root:
        raise PermissionError("no outbox configured: files cannot be sent")
    resolved = Path(path).resolve(strict=True)
    if not resolved.is_relative_to(Path(root).resolve()) or not resolved.is_file():
        raise PermissionError("not a staged file")
    if resolved.stat().st_size > MAX_FILE_BYTES:
        raise PermissionError(f"larger than the {MAX_FILE_BYTES} byte limit")
    return resolved


async def on_send(frame: dict) -> dict:
    """A notification from a wolt: text, or one file, to a DM or channel."""
    channel = frame["route"]["chat_id"]
    try:
        if frame["method"] == "message":
            return {"status": "delivered", "message_id": await API.post(channel, frame.get("text", ""))}
        await API.upload(channel, str(_outbox_file(frame["path"])), frame["filename"], frame.get("text") or "")
    except PermissionError as exc:
        return {"status": "refused", "error": str(exc)}
    except SlackApiError as exc:
        # Slack answered, and not ok: missing_scope names the scope to add (files:write).
        error = exc.response.get("error", "error")
        needed = exc.response.get("needed")
        return {"status": "refused", "error": f"{error} ({needed})" if needed else error}
    except TimeoutError as exc:
        return {"status": "ambiguous", "error": str(exc) or "timed out"}  # it may have arrived
    except SLACK_ERRORS as exc:
        return {"status": "failed", "error": str(exc) or type(exc).__name__}
    return {"status": "delivered", "message_id": ""}


# --- run ----------------------------------------------------------------------------------

async def _connect(client) -> dict:
    delay = 0.5
    while True:
        try:
            return await client.connect()
        except OSError as exc:
            logger.warning(f"lodge not reachable ({exc}); retrying in {delay:.1f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def build_app(token: str):
    from slack_bolt.async_app import AsyncApp

    app = AsyncApp(token=token)

    app.event("message")(on_message)
    for name in COMMANDS:
        app.command(f"/{name}")(on_command)
    app.action(re.compile(".*"))(on_action)
    return app


async def main() -> None:
    global CLIENT, API, COMMANDS
    from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

    if not owner():
        logger.warning("SLACK_OWNER_USER is empty: every Slack message will be ignored.")
    CLIENT = connector_client.Client(NAME, on_say=on_say, on_edit=on_edit, on_ack=on_ack, on_send=on_send)
    welcome = await _connect(CLIENT)
    COMMANDS = tuple(name for name, _ in welcome["commands"] if name != "start")
    token = os.environ["SLACK_BOT_TOKEN"]
    app = build_app(token)
    API = SlackAPI(app.client, token)
    auth = await app.client.auth_test()
    BOT.update(user_id=str(auth.get("user_id") or ""), bot_id=str(auth.get("bot_id") or ""))
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logger.info("slack connector starting (socket mode)")
    await handler.connect_async()
    try:
        await CLIENT.run_forever()
    finally:
        await handler.close_async()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except connector_client.Refused as exc:
        # A newer launch holds the socket: this one has nothing left to do.
        logger.warning(f"the lodge closed this connector: {exc}")
