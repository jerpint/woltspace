"""The lodge's side of a chat connector: one ChatLodge per connector (RFC #617).

A connector process sends typed events over its socket (connector_broker);
this module decides what they mean. It checks the sender against the lodge's
own owner list, runs commands and selections (lodge_commands), routes
messages and attachments (lodge_messages), asks the dog, and keeps each
chat's state and history. Nothing here is specific to one provider: Telegram
is just the first connector that uses it.

Services are this module's names, looked up at call time, so tests replace
them here (list_wolts, message_session, start_claude_session, ...).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.core import (
    _bot_log, _sanitize_history, build_ack_text, dog_available, get_response,
    get_tunnel_url, kill_session, list_sessions, list_wolts, message_session,
    start_claude_session, transcribe_audio,
)
import chat_state
import lodge_commands as commands
import lodge_messages as messages
import outbox
from connector_contract import Ack, Reply
from creature_types import emoji_from
from env_compat import get_env
from harnesses import platform_skill_invoke
from notify_prompt import notify_reply_instruction
from paths import space_dir
from sessions import resolve_active_session, wolt_harness
from skills_sync import wolt_skills_delivery
from wolts import get_active_creature, wolt_display_name, wolt_exists

logger = logging.getLogger(__name__)

MAX_HISTORY = 20
CREATURE_EMOJIS = {"raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "dog": "🐶", "wolf": "🐺"}

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

# Footer format from notify:
#   ---
#   ↩️ reply to this message to talk to this session directly
#   {tunnel_url}/tui?session={session_name}
REPLY_FOOTER_MARKER = "↩️ reply to this message to talk to this session directly"

# What each kind of attachment is called in the message a session reads.
_MEDIA_LABEL = {"image": "image received", "video": "video received", "file": "file received"}


def wolts_dir() -> Path:
    return Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))


# --- reply footers (the lodge writes them, so the lodge reads them) -------------

def parse_session_from_reply(reply_text: str) -> str | None:
    """The session named in a notification's footer, or None."""
    if not reply_text or REPLY_FOOTER_MARKER not in reply_text:
        return None
    lines = reply_text.split("\n")
    for i, line in enumerate(lines):
        if REPLY_FOOTER_MARKER in line and i + 1 < len(lines):
            url_line = lines[i + 1].strip()
            try:
                qs = parse_qs(urlparse(url_line).query)
                if "session" in qs:
                    return qs["session"][0]
            except Exception:
                pass
            if url_line.startswith("session="):
                return url_line.split("=", 1)[1]
    return None


def quote_reply(body: str, file_name: str | None, message: str, voice: bool = False) -> str:
    """Prefix a routed reply with what it replies to, without the reply footer."""
    footer = "---\n" + REPLY_FOOTER_MARKER
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


def photo_content(image_bytes: bytes, mime_type: str = "image/jpeg", caption: str = "") -> list:
    b64 = base64.b64encode(image_bytes).decode()
    content = [{"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}}]
    content.append({"type": "text", "text": caption if caption else "What's in this image?"})
    return content


def format_tool_log(tc: dict) -> str:
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



# The dog's tools change process-wide state (switch_wolt rewrites the active
# wolt's directories and environment), so two dogs must never run at once,
# even for different connectors. The call runs off the dispatch loop, one at
# a time.
_DOG_LOCK = threading.Lock()


def _dog_response(*args, **kwargs):
    with _DOG_LOCK:
        return get_response(*args, **kwargs)

class ChatLodge:
    """Everything the lodge does for one connector's chats."""

    def __init__(self, spec, owners, *, help_tips: tuple[str, ...] = ()):
        self.spec = spec
        self.name = spec.name
        self.owners = frozenset(str(o) for o in owners)
        self.help_tips = tuple(help_tips)
        self.wolts_dir = wolts_dir()
        self.data_dir = space_dir(self.wolts_dir) / self.name
        self.history_dir = self.data_dir / "history"
        # Where the connector saves what it downloads; nothing else it hands in is read.
        self.uploads_dir = self.data_dir / "uploads"

    # --- the event entry point ----------------------------------------------------

    async def handle(self, event: dict, out) -> None:
        """One event from the connector. Runs on the broker's dispatch thread."""
        if str(event.get("sender")) not in self.owners:
            # The connector filters first; this catches its mistakes, not its lies.
            _bot_log(f"{self.name}_not_owner", {"kind": event.get("kind")})
            return
        route = dict(event["route"])
        body = event["body"]
        kind = event["kind"]
        if kind == "command":
            reply = await commands.run_command(
                self.ports(route), self.chat(route), body["name"], list(body.get("args") or []),
                tips=self.help_tips,
            )
            await out.say(reply)
        elif kind == "selection":
            outcome = await commands.run_selection(self.ports(route), self.chat(route), body["action"])
            await out.ack(outcome.ack or Ack())
            if outcome.edit is not None:
                await out.edit(outcome.edit)
        elif kind == "message":
            await self.on_message(route, body, out)
        elif kind == "attachment":
            await self.on_attachment(route, body, out)

    async def on_message(self, route: dict, body: dict, out) -> None:
        chat = self.chat(route)
        text = body.get("text") or ""
        _bot_log("message_received", {"chat_id": chat, "text": text[:100],
                                      "has_reply": body.get("reply_to") is not None})
        # In a group, only when addressed (mentioned, replied to) or already talking.
        if body.get("group") and not body.get("addressed"):
            state = self._load_chat_state(chat)
            if not (state.get("active_wolt") and state.get("active_session")):
                return
        # A mention asks the dog when there is one; otherwise it is routed as usual.
        if body.get("mention") and dog_available():
            await self._handle_dog(route, out, text)
            return
        await self._route(route, out, text, body.get("reply_to"))

    async def on_attachment(self, route: dict, body: dict, out) -> None:
        media, path = body["media"], body["path"]
        if not self._inside_uploads(path):
            logger.error(f"{self.name}: attachment outside its uploads dir refused: {path}")
            await out.say(Reply("That file did not arrive where the lodge expects it, so it was not passed on."))
            return
        caption = body.get("caption") or ""
        if media == "voice":
            try:
                text = await asyncio.to_thread(transcribe_audio, path)
            except Exception as e:
                logger.error(f"Transcription failed: {e}")
                await out.say(Reply("Couldn't transcribe that voice message."))
                return
            logger.info(f"Transcribed: {text[:100]}")
            await self._route(route, out, f"[voice message] {text}", body.get("reply_to"))
            return
        file_msg = (f"[{_MEDIA_LABEL[media]}] {body['name']} ({body['mime']}, {body['size']} bytes)"
                    f" saved at {path}")
        if caption:
            file_msg += f"\nCaption: {caption}"
        dog_content = None
        if media == "image":
            # With no wolt chosen, the dog sees the image itself.
            dog_content = {
                "message": f"[image] {caption}" if caption else "[image]",
                "content": photo_content(self._read_upload(path), body["mime"], caption),
            }
        await self._route(route, out, file_msg, body.get("reply_to"), dog_content)

    # --- chats ----------------------------------------------------------------------

    def chat(self, route: dict) -> str:
        """The chat key a route is stored under: its fields, written as text."""
        return self.spec.codec.format(route)

    def route_of(self, chat) -> dict:
        return self.spec.codec.parse(str(chat))

    def _inside_uploads(self, path: str) -> bool:
        """Only files the connector saved in its uploads dir reach a session.

        The connector is trusted code: this catches its mistakes (a path into
        the chat history beside it, a symlink out), it does not contain it.
        """
        try:
            resolved = Path(path).resolve(strict=True)
            return resolved.is_file() and resolved.is_relative_to(self.uploads_dir.resolve())
        except (OSError, ValueError):
            return False

    def _read_upload(self, path: str) -> bytes:
        return Path(path).read_bytes()

    def _wolt_exists(self, name: str) -> bool:
        return wolt_exists(name, self.wolts_dir)

    def _load_chat_state(self, chat) -> dict:
        """{active_wolt, active_session}; a wolt that is gone reads as no choice."""
        state = chat_state.get(self.wolts_dir, self.name, chat)
        if state.get("active_wolt") and not self._wolt_exists(state["active_wolt"]):
            state.pop("active_wolt")
            state.pop("active_session", None)
        return state

    def _save_chat_state(self, chat, state: dict) -> None:
        chat_state.put(self.wolts_dir, self.name, chat, state)

    def _forget_session(self, name: str) -> None:
        chat_state.forget_session(self.wolts_dir, self.name, name)

    # --- history --------------------------------------------------------------------

    def _history_file(self, chat) -> Path:
        return self.history_dir / f"{chat}.jsonl"

    def _load_history(self, chat) -> list:
        path = self._history_file(chat)
        if not path.exists():
            return []
        found = []
        for line in path.read_text().strip().split("\n")[-MAX_HISTORY * 2:]:
            try:
                found.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return _sanitize_history(found)

    def _append_history(self, chat, role: str, content: str) -> None:
        self._append_message(chat, {"role": role, "content": content})

    def _append_message(self, chat, msg: dict) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True)
        with open(self._history_file(chat), "a") as f:
            f.write(json.dumps({**msg, "ts": datetime.now(timezone.utc).isoformat()}) + "\n")

    # --- wolts and sessions ---------------------------------------------------------

    def _dog_name(self) -> str:
        return get_active_creature("dog") or get_env("WOLTSPACE_WOLT_NAME", "wolt")

    def _shown(self, wolt: str) -> str:
        return wolt_display_name(wolt, self.wolts_dir) if wolt else wolt

    def _creature_emoji(self, wolt: str) -> str:
        """The wolt's creature emoji from its wolt.json; 🦝 when it has none."""
        emoji = CREATURE_EMOJIS.get("raccoon", "🐾")
        wolt_json = self.wolts_dir / wolt / "wolt" / "wolt.json"
        if wolt_json.exists():
            try:
                emoji = emoji_from(CREATURE_EMOJIS, json.loads(wolt_json.read_text()).get("type", ""), "🐾")
            except (json.JSONDecodeError, OSError):
                pass
        return emoji

    def _session_emoji(self, session_name: str) -> str:
        wolt = commands.wolt_of(session_name)
        types_by_wolt = {commands.wolt_name(w): w.get("type", "") for w in list_wolts()}
        return emoji_from(commands.WOLT_TYPE_EMOJI, types_by_wolt.get(wolt, ""), "🦫")

    def _start_chat_prompt(self, wolt: str) -> str:
        """The bare boot prompt: start-chat in this connector's mode."""
        invoke = platform_skill_invoke(
            wolt_harness(wolt), "start-chat",
            delivery=wolt_skills_delivery(self.wolts_dir / wolt))
        return f"{invoke} {self.name} {wolt}"

    def _routing(self, chat) -> dict:
        return {"adapter": self.name, **self.route_of(chat)}

    def _spawn_session(self, wolt: str, chat, prompt: str = "") -> dict:
        return start_claude_session(
            prompt=prompt or self._start_chat_prompt(wolt), wolt=wolt, routing=self._routing(chat))

    def _session_message(self, text: str, chat) -> str:
        """A chat message as its session reads it, pasted or in the opening prompt."""
        fields = ", ".join(f"{k}={v}" for k, v in self.route_of(chat).items())
        return f"[{self.name} message from human, {fields}]: {text}"

    def _reply_instruction(self, chat) -> str:
        return notify_reply_instruction(*self.spec.reply_args(self.route_of(chat)))

    def _spawn_session_with_message(self, wolt: str, chat, text: str) -> dict:
        """A session for an incoming message, the message in its opening prompt (#555)."""
        session = self._spawn_session(
            wolt, chat, f"{self._start_chat_prompt(wolt)}\n\n{self._session_message(text, chat)}")
        _bot_log(f"{self.name}_spawn_with_message", {
            "session": session["name"], "wolt": wolt, "text": text[:200]})
        self._append_history(chat, "user", text)
        self._append_message(chat, {"role": "assistant",
                                    "content": f"[delivered to session {session['name']}]"})
        return session

    # --- the lodge's services, over its API -----------------------------------------

    def _api(self) -> str:
        return get_env("WOLTSPACE_API", "http://localhost:7777")

    @staticmethod
    def _lodge_client():
        """An HTTP client for the lodge's own API, carrying the owner key: the
        chat lodge acts for the owner, whatever shell started the lodge."""
        import httpx
        import lodge_auth
        return httpx.AsyncClient(headers=lodge_auth.owner_headers(get_env("WOLTSPACE_WOLTS_DIR")))

    async def _fetch_apps(self) -> list[dict]:
        async with self._lodge_client() as client:
            resp = await client.get(f"{self._api()}/apps", timeout=5)
            resp.raise_for_status()
            return resp.json()

    async def _app_action(self, name: str, action: str) -> bool:
        async with self._lodge_client() as client:
            try:
                resp = await client.post(f"{self._api()}/apps/{name}/{action}", timeout=15)
                return resp.status_code < 400
            except Exception:
                return False

    async def _release_hold(self, name: str) -> bool:
        """The session rests at the next sweep only if nobody is using it."""
        async with self._lodge_client() as client:
            try:
                resp = await client.delete(f"{self._api()}/sessions/{name}/hold", timeout=15)
                return resp.status_code < 400
            except Exception:
                return False

    async def _idle_setting(self, body: dict | None = None) -> tuple[int, dict]:
        url = f"{self._api()}/settings/session-expiry"
        async with self._lodge_client() as client:
            resp = await (client.get(url, timeout=10) if body is None
                          else client.post(url, json=body, timeout=10))
        try:
            data = resp.json()
        except Exception:
            data = {}
        return resp.status_code, data if isinstance(data, dict) else {}

    def ports(self, route: dict) -> commands.Ports:
        """The lodge as the commands see it. Each name resolves at call time."""
        return commands.Ports(
            list_wolts=lambda: list_wolts(),
            list_sessions=lambda: list_sessions(),
            tunnel_url=lambda: get_tunnel_url(),
            wolt_exists=lambda name: (self.wolts_dir / name / "wolt").is_dir(),
            shown=lambda name: self._shown(name),
            session_emoji=lambda name: self._session_emoji(name),
            load_chat=lambda chat: self._load_chat_state(chat),
            save_chat=lambda chat, state: self._save_chat_state(chat, state),
            forget_session=lambda name: self._forget_session(name),
            kill_session=lambda name: kill_session(name),
            spawn_session=lambda wolt, chat: self._spawn_session(wolt, chat),
            dog_available=lambda: dog_available(),
            dog_name=lambda: self._dog_name(),
            fetch_apps=lambda: self._fetch_apps(),
            app_action=lambda name, action: self._app_action(name, action),
            release_hold=lambda name: self._release_hold(name),
            idle_setting=lambda body=None: self._idle_setting(body),
            log_error=lambda message: logger.error(message),
        )

    # --- routing ----------------------------------------------------------------------

    def _message_ports(self, route: dict, out, wrapped: bool = True) -> messages.MessagePorts:
        chat = self.chat(route)
        return messages.MessagePorts(
            lodge=self.ports(route),
            say=lambda reply: out.say(reply),
            message_session=lambda name, text: message_session(name, text),
            frame=lambda text: f"{self._session_message(text, chat)}\n{self._reply_instruction(chat)}",
            resolve_live=lambda wolt: resolve_active_session(wolt),
            spawn_with_message=lambda wolt, text: self._spawn_session_with_message(wolt, chat, text),
            wolt_exists=lambda wolt: self._wolt_exists(wolt),
            creature_emoji=lambda wolt: self._creature_emoji(wolt),
            dog_available=lambda: dog_available(),
            ask_dog=lambda text, content: self._ask_dog(route, out, text, content),
            remember_user=lambda text: self._append_history(chat, "user", text),
            remember_delivery=lambda name: self._append_message(chat, {
                "role": "assistant", "content": f"[delivered to session {name}]"}),
            deliver=(lambda name, wolt, text: self._route_to_session(route, out, name, wolt, text))
            if wrapped else None,
            log=lambda event, data: _bot_log(f"{self.name}_{event}", data),
            log_error=lambda message: logger.error(message),
        )

    async def _route_to_session(self, route: dict, out, session_name: str, wolt: str, text: str):
        return await messages.deliver(self._message_ports(route, out, wrapped=False),
                                      session_name, wolt, text)

    def _reply_target(self, reply_to: dict | None, text: str) -> tuple[str | None, str | None]:
        """The session whose notification this replies to, and the message as it should read it.

        Only the bot's own messages count: in a group anyone can paste a footer.
        """
        if not reply_to or not reply_to.get("from_bot"):
            return None, None
        body = reply_to.get("text") or ""
        session = parse_session_from_reply(body)
        if not session:
            return None, None
        file_name = reply_to.get("file_name")
        # Cleaned as an outgoing name is: anyone can send a file under any name.
        file_name = outbox.display_name(file_name) if file_name else None
        return session, quote_reply(body, file_name, text, bool(reply_to.get("voice")))

    async def _route(self, route: dict, out, text: str, reply_to: dict | None = None,
                     dog_content=None) -> None:
        reply_session, reply_text = self._reply_target(reply_to, text)
        await messages.route_message(
            self._message_ports(route, out), self.chat(route), text,
            reply_session=reply_session, reply_text=reply_text, dog_content=dog_content,
        )

    # --- the dog ------------------------------------------------------------------------

    def format_response(self, result: dict) -> str:
        if result["type"] == "session":
            if result.get("text"):
                text = result["text"]
            else:
                s = result["session"]
                text = build_ack_text(s.get("url"), s.get("name"), self.name, creature=s.get("creature"))
        elif result["type"] == "image":
            text = result.get("text", "") or result.get("caption", "") or result.get("filename", "image")
        else:
            text = result["text"]
        return f"🐶 {self._dog_name()}: {text}"

    async def _send_result(self, out, result: dict) -> None:
        """Tool call logs first, then the dog's answer."""
        for tc in result.get("tool_calls_log", []):
            try:
                await out.say(Reply(format_tool_log(tc)))
            except Exception:
                pass
        if result["type"] == "image":
            caption = result.get("text") or result.get("caption") or ""
            await out.say(Reply(caption), photo=result["path"])
        else:
            await out.say(Reply(self.format_response(result)))

    async def _dog_ack(self, out) -> None:
        try:
            await out.say(Reply(random.choice(DOG_ACK_MESSAGES)))
        except Exception:
            pass

    async def _ask_dog(self, route: dict, out, text: str, content=None) -> None:
        if content is not None:
            await self._photo_dog(route, out, content)
        else:
            await self._handle_dog(route, out, text)

    async def _photo_dog(self, route: dict, out, content: dict) -> None:
        """The dog looks at an image sent while no wolt is chosen."""
        chat = self.chat(route)
        await self._dog_ack(out)
        history = self._load_history(chat)
        try:
            result = await asyncio.to_thread(
                _dog_response, content["message"], conversation_history=list(history),
                routing=self._routing(chat), user_content=content["content"])
        except Exception as e:
            await out.say(Reply(f"photo error: {e}"))
            return
        self._append_history(chat, "user", content["message"])
        for msg in result["history_messages"]:
            self._append_message(chat, msg)
        await self._send_result(out, result)

    async def _handle_dog(self, route: dict, out, user_message: str) -> None:
        """The dog answers, for routing and admin."""
        chat = self.chat(route)
        if not dog_available():
            await messages.route_message(self._message_ports(route, out), chat, user_message)
            return
        await self._dog_ack(out)
        history = self._load_history(chat)
        try:
            result = await asyncio.to_thread(
                _dog_response, user_message, conversation_history=list(history),
                routing=self._routing(chat))
        except Exception as e:
            logger.error(f"Error getting response: {e}")
            await out.say(Reply("Something broke on my end. Try again in a sec."))
            return
        self._append_history(chat, "user", user_message)
        for msg in result["history_messages"]:
            self._append_message(chat, msg)
        await self._send_result(out, result)

        # If the dog spawned a session, this chat now talks to it.
        if result.get("type") == "session":
            session = result.get("session", {})
            session_name = session.get("name")
            wolt_name = session.get("wolt") or ""
            if session_name and wolt_name:
                state = self._load_chat_state(chat)
                state["active_wolt"] = wolt_name
                state["active_session"] = session_name
                self._save_chat_state(chat, state)
                emoji = emoji_from(CREATURE_EMOJIS, session.get("creature", ""), "🐾")
                handoff = f"{emoji} {wolt_name} is now active in this chat — messages go directly to this session"
                if session.get("url"):
                    handoff += f"\n{session['url']}"
                await out.say(Reply(handoff))
