"""Notification system — send messages to Telegram/Slack.

Routing is explicit: the caller (rodent) tells us where to send via adapter flags.
Falls back to session registry lookup, then Telegram default.
"""

import json
import logging
import mimetypes
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from secrets import token_hex

from .notification_senders import (
    slack_send, slack_sender, slack_set_agent_status, telegram_send, telegram_sender,
    TELEGRAM_MAX_ATTACHMENT_BYTES, telegram_send_audio, telegram_send_document,
    telegram_send_voice,
    SLACK_MAX_ATTACHMENT_BYTES, slack_upload_file,
)
from .notification_types import Attachment, OutboundMessage, Plugin, SendContext
from .speech import (
    DEFAULT_SPEECH_MODEL, DEFAULT_SPEECH_VOICE, SPEECH_FILES, SpeechError, synthesize,
    write_failure,
)
import outbox
import voice_notes
from sessions import SessionRegistry

from .config import (
    STATE_DIR,
    SPACE_DIR,
    SPACE_PLATFORM_DIR,
    WOLTS_DIR,
    DEN_REPLY_FOOTER,
    dotenv_env,
)
from .state import sanitize_session

logger = logging.getLogger(__name__)

MAX_ATTACHMENTS = 1
# Where `notify --file` stages what it wants sent, and where core writes the
# audio of a message it has spoken. Core is the only server code that touches
# it, and it opens entries by id, never a path a caller names.
OUTBOX_DIR = SPACE_DIR / "outbox"


class AttachmentError(RuntimeError):
    """A file the request named, or the words it asked to have spoken, cannot be sent.

    `reason` is machine-readable.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class NoNotificationTarget(RuntimeError):
    """Nowhere to deliver to — a configuration gap, not a platform failure.

    Its own type so the API can answer 409 instead of 500: an unconnected chat
    is the user's next step, and a 500 tells them woltspace is broken. The
    agent that hit this had to guess which it was.
    """


def _slack_session_link(session: str, routing: dict | None) -> str:
    """Return only the adapter-validated link bound to this exact session."""
    url = (routing or {}).get("slack_session_link", "")
    try:
        parsed = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parsed.query, strict_parsing=True)
    except (TypeError, ValueError):
        return ""
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
            parsed.password or parsed.path != "/tui" or parsed.fragment or
            query != {"session": [session]}):
        return ""
    return url



def read_session_registry(session: str) -> dict | None:
    """The session's registry record, or None."""
    return SessionRegistry(WOLTS_DIR).get(sanitize_session(session), check_alive=False)
    return None


def append_chat_history(adapter: str, chat_id: str, content: str):
    """Write notify messages into the bot's chat history."""
    from datetime import datetime, timezone

    outbound = _outbound(adapter)
    subdir = STATE_DIR / "chat"
    if outbound and outbound.history_subdir:
        subdir = subdir / outbound.history_subdir
    subdir.mkdir(parents=True, exist_ok=True)
    chat_file = subdir / f"{chat_id}.jsonl"

    clean = content.replace(DEN_REPLY_FOOTER, "")
    entry = {
        "role": "user",
        "content": f"<system>This message was sent by a Claude Code session directly to the user. It is context only — do not respond to it.</system>\n{clean}",
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with open(chat_file, "a") as f:
        f.write(json.dumps(entry) + "\n")


# Transport aliases are temporary compatibility patch points for existing tests.
# The providers own each implementation; these lambdas resolve patched names at call time.
SENDERS = {
    "telegram": Plugin(
        "telegram",
        telegram_sender(
            lambda *args: telegram_send(*args),
            lambda *args: telegram_send_document(*args),
            lambda *args: telegram_send_voice(*args),
            lambda *args: telegram_send_audio(*args),
        ),
        # Sent by the Telegram connector, over its socket: no secret here.
        secrets=(),
        max_attachment_bytes=TELEGRAM_MAX_ATTACHMENT_BYTES,
        speech_format="opus",
    ),
    "slack": Plugin(
        "slack", slack_sender(
            lambda *args: slack_send(*args),
            lambda *args: slack_set_agent_status(*args),
            lambda *args: slack_upload_file(*args),
        ),
        secrets=("SLACK_BOT_TOKEN",),
        route_state=("slack_progress_mode", "slack_progress_ts"),
        max_attachment_bytes=SLACK_MAX_ATTACHMENT_BYTES,
        speech_format="mp3",
    ),
}


# --- how core reaches each connector's chats (RFC #617) -----------------------
# Everything that differs between connectors on the way out lives in one
# _Outbound per connector; _send and _route below only loop over them. Hooks
# read this module's names (dotenv_env, SPACE_PLATFORM_DIR) at call time.

@dataclass(frozen=True)
class _Outbound:
    # The key naming the destination in a send's result ("chat_id", "channel").
    route_key: str
    # (session, chat_id, thread_ts, routing, secrets) -> (destination, chat_id, session_link)
    destination: Callable
    # The chats the lodge is set up with, where files and voice may go.
    owner_chats: Callable[[], set]
    # (session, explicit, routing) -> {"session", "chat_id", "thread_ts", "routing"} or None
    explicit: Callable
    # (routing) -> (chat_id, thread_ts) when the session's record routes here, or None
    from_session: Callable
    history_subdir: str = ""
    # A last-resort destination when nothing else routes the message.
    default_chat: Callable[[], str | None] | None = None
    # The session record's own destination, in the form `destination` returns
    # it; None means its record's chat_id, as for the built-ins.
    bound_chat: Callable[[dict], str | None] | None = None


def _tunnel_url() -> str:
    try:
        state = json.loads((SPACE_PLATFORM_DIR / "tunnel.json").read_text())
        return state.get("url", "").strip()
    except Exception:
        return ""


def _connector_up(name: str) -> bool:
    """Whether a folder connector is connected to this lodge's broker right now."""
    import connector_broker

    broker = connector_broker.current()
    return broker is not None and broker.connected(name)


def _telegram_destination(session, chat_id, thread_ts, routing, secrets):
    # Asked first, before a file is checked or words are spoken (and billed).
    if not _connector_up("telegram"):
        raise RuntimeError("telegram is not connected to the lodge")
    session_link = ""
    if session:
        tunnel_url = _tunnel_url()
        session_link = f"{tunnel_url}/tui?session={session}" if tunnel_url else f"session={session}"
    return {"chat_id": chat_id}, chat_id, session_link


def _telegram_allowed() -> list[str]:
    return [s.strip() for s in dotenv_env("TELEGRAM_ALLOWED_USERS").split(",") if s.strip()]


def _telegram_explicit(session, explicit, routing):
    chat_id = explicit.get("chat_id", "")
    if not chat_id:
        return None
    return {"session": session, "chat_id": str(chat_id), "thread_ts": None, "routing": None}


def _telegram_from_session(routing):
    chat_id = routing.get("chat_id", "")
    return (str(chat_id), routing.get("thread_ts")) if chat_id else None


def _telegram_default() -> str | None:
    allowed = _telegram_allowed()
    if dotenv_env("TELEGRAM_BOT_TOKEN") and allowed:
        return allowed[0]
    return None


def _slack_destination(session, chat_id, thread_ts, routing, secrets):
    if not secrets.get("SLACK_BOT_TOKEN"):
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    if not chat_id:
        chat_id = dotenv_env("SLACK_NOTIFY_CHANNEL")
    if not chat_id:
        raise RuntimeError("no slack channel provided and SLACK_NOTIFY_CHANNEL not set")
    destination = {"channel": chat_id}
    if thread_ts is not None:
        destination["thread_ts"] = thread_ts
    return destination, chat_id, _slack_session_link(session, routing)


def _slack_explicit(session, explicit, routing):
    """A progress surface is usable only when the route is exactly the session's own."""
    channel = explicit.get("channel", "")
    thread_ts = explicit.get("thread_ts")
    exact = bool(
        routing
        and routing.get("adapter") == "slack"
        and routing.get("chat_id") == channel
        and routing.get("thread_ts") == thread_ts
    )
    return {"session": session if exact else "", "chat_id": channel, "thread_ts": thread_ts,
            "routing": routing if exact else None}


# Keyed by connector name; a connector with a manifest must match it.
OUTBOUND = {
    "telegram": _Outbound(
        route_key="chat_id",
        destination=_telegram_destination,
        owner_chats=lambda: {e.strip() for e in dotenv_env("TELEGRAM_ALLOWED_USERS").split(",")},
        explicit=_telegram_explicit,
        from_session=_telegram_from_session,
        default_chat=_telegram_default,
    ),
    "slack": _Outbound(
        route_key="channel",
        destination=_slack_destination,
        owner_chats=lambda: {dotenv_env("SLACK_NOTIFY_CHANNEL")},
        explicit=_slack_explicit,
        from_session=lambda routing: (routing.get("chat_id", ""), routing.get("thread_ts")),
        history_subdir="slack",
    ),
}


# --- connectors that run from a folder (container/lib/connector_registry) -------
# A folder connector needs no entry above: its route codec builds its _Outbound,
# and it sends through its socket (connector_broker). Nothing of it is imported.

def _declared(adapter: str):
    import connector_registry

    if adapter in OUTBOUND:
        return None
    spec = connector_registry.spec(adapter, WOLTS_DIR)
    return spec if spec is not None and spec.launchable else None


def _declared_outbound(spec) -> _Outbound:
    codec = spec.codec

    def destination(session, route_text, thread_ts, routing, secrets):
        if not _connector_up(spec.name):
            raise RuntimeError(f"{spec.name} is not connected to the lodge")
        route = codec.canonical(route_text)
        session_link = ""
        if session:
            tunnel_url = _tunnel_url()
            session_link = f"{tunnel_url}/tui?session={session}" if tunnel_url else f"session={session}"
        return dict(route), route_text, session_link

    def explicit(session, explicit, routing):
        text = str(explicit.get("route", ""))
        route = codec.canonical(text)
        # Exact binding: the session's own route keeps the session (and its link).
        exact = codec.matches(route, routing)
        return {"session": session if exact else "", "chat_id": text, "thread_ts": None,
                "routing": routing if exact else None}

    def from_session(routing):
        route = codec.from_record(routing)
        return (codec.format(route), None) if route else None

    return _Outbound(
        route_key="route",
        destination=destination,
        owner_chats=lambda: set(),
        explicit=explicit,
        from_session=from_session,
        history_subdir=spec.name,
        bound_chat=lambda record: (codec.format(codec.from_record(record))
                                   if codec.from_record(record) else None),
    )


SOCKET_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024


def _socket_sender(spec) -> Plugin:
    """Send through a folder connector's socket: text, or one file or voice note."""
    import connector_broker

    async def send(message: OutboundMessage, destination, context: SendContext) -> None:
        broker = connector_broker.current()
        if broker is None:
            raise RuntimeError(f"{spec.name} is not connected to the lodge")
        text = message.text
        if message.session_link:
            text += f"\n\n---{DEN_REPLY_FOOTER}\n{message.session_link}"
        if len(message.attachments) > 1:
            raise RuntimeError(f"{spec.name} sender carries one file per message")
        extra, method = {}, "message"
        if message.attachments:
            attachment = message.attachments[0]
            method = "voice" if attachment.kind == "voice" else "document"
            extra = dict(path=str(attachment.path), filename=attachment.filename,
                         content_type=attachment.content_type)
        try:
            await broker.send(spec.name, destination, method, text, **extra)
        except connector_broker.SendFailed as exc:
            raise RuntimeError(f"{spec.name} {exc.status}: {exc}") from None
        except connector_broker.ConnectorUnavailable as exc:
            raise RuntimeError(str(exc)) from None

    manifest = spec.manifest
    return Plugin(
        spec.name, send, secrets=(),
        max_attachment_bytes=SOCKET_MAX_ATTACHMENT_BYTES if manifest.can("files") else 0,
        speech_format="opus" if manifest.can("voice_out") else "",
    )


def _outbound(adapter: str) -> _Outbound | None:
    if adapter in OUTBOUND:
        return OUTBOUND[adapter]
    spec = _declared(adapter)
    return _declared_outbound(spec) if spec else None


def _sender(adapter: str) -> Plugin:
    if adapter in SENDERS:
        return SENDERS[adapter]
    spec = _declared(adapter)
    if spec is None:
        raise KeyError(adapter)
    return _socket_sender(spec)


def _resolve_attachments(attachments) -> tuple[Attachment, ...]:
    """Turn the request's outbox ids into attachments, measured and named by core."""
    if (
        not isinstance(attachments, (list, tuple))
        or len(attachments) > MAX_ATTACHMENTS
        or not all(
            isinstance(entry, dict) and isinstance(entry.get("id"), str)
            for entry in attachments
        )
    ):
        raise AttachmentError(
            "attachment_invalid",
            "attachments must be a list with at most one {id, name} entry",
        )
    resolved = []
    for entry in attachments:
        try:
            path, size = outbox.resolve(OUTBOX_DIR, entry["id"])
        except outbox.OutboxError as exc:
            raise AttachmentError(exc.reason, str(exc)) from None
        filename = outbox.display_name(entry.get("name"))
        kind = entry.get("kind", "file")
        if kind not in ("file", "voice"):
            raise AttachmentError("attachment_invalid", 'attachment kind must be "file" or "voice"')
        if kind == "voice":
            # Read from the name alone: a voice note is one of a short list of types.
            content_type = voice_notes.voice_content_type(filename)
            if content_type is None:
                raise AttachmentError(
                    "voice_format_unsupported",
                    f"{filename} cannot be a voice note; "
                    f"send audio ending in {voice_notes.VOICE_SUFFIXES_TEXT}",
                )
        else:
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        resolved.append(Attachment(path, filename, size, content_type, kind))
    return tuple(resolved)


@dataclass(frozen=True)
class _Speech:
    """Words to say, and the outbox entry set aside for the audio."""

    text: str
    entry_id: str


def _speech_request(speak, attachments) -> _Speech | None:
    """The request's `speak` as words to say; AttachmentError says why not."""
    if speak is None:
        return None
    text = speak.get("text") if isinstance(speak, dict) else None
    if not isinstance(text, str) or not text.strip():
        raise AttachmentError("speak_invalid", "speak must be {text} with the words to say")
    if attachments:
        raise AttachmentError("speak_invalid", "a spoken message cannot also carry a file")
    if len(text) > voice_notes.SPEECH_MAX_CHARS:
        raise AttachmentError(
            "speak_too_long",
            f"speech reads at most {voice_notes.SPEECH_MAX_CHARS} characters; "
            f"this text has {len(text)}",
        )
    return _Speech(text, token_hex(16))


def _wolt_speech(wolt: str) -> dict:
    """The sending wolt's own `speech` in its wolt.json; {} when it has none to read.

    `wolt` is the session record's, never the request's. A wolt.json that is
    missing or broken is not a speech problem: the lodge's settings stand.
    """
    if not wolt or wolt in (".", "..") or Path(wolt).name != wolt:
        return {}
    try:
        config = json.loads((WOLTS_DIR / wolt / "wolt" / "wolt.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(config, dict):
        return {}
    import speech_settings

    try:
        return speech_settings.parse_wolt_speech(config.get("speech"))
    except speech_settings.SettingsInvalid as exc:
        raise SpeechError(
            "speech_not_configured",
            f"the speech settings in wolt.json are unusable ({exc}); send the message as text",
        ) from None


async def _spoken(speech: _Speech, sender: Plugin, adapter: str, wolt: str = "") -> Attachment:
    """Have the speech service say the words, into the outbox entry set aside for them.

    `wolt`, the sending session's, may pick its own voice and a hint on how to speak.
    """
    if not sender.speech_format or not sender.max_attachment_bytes:
        # Asked before the words are spoken: the audio travels as an attachment.
        raise AttachmentError("speech_unsupported", f"{adapter} cannot carry a spoken message")
    import speech_settings

    try:
        # Read on every spoken message. The older .env names still pick the
        # model and voice when the settings leave them out.
        settings = speech_settings.load_settings(
            speech_settings.config_path(WOLTS_DIR),
            model=dotenv_env("OPENAI_TTS_MODEL") or DEFAULT_SPEECH_MODEL,
            voice=dotenv_env("OPENAI_TTS_VOICE") or DEFAULT_SPEECH_VOICE,
        )
    except speech_settings.SettingsInvalid as exc:
        raise SpeechError(
            "speech_not_configured",
            f"the speech settings in woltspace.json are unusable ({exc}); send the message as text",
        ) from None
    # A wolt's own voice goes to any server; its hint is an OpenAI field.
    wolt_speech = _wolt_speech(wolt)
    if settings.base_url != speech_settings.DEFAULT_BASE_URL:
        wolt_speech.pop("instructions", None)
    api_key = dotenv_env(settings.key_env) if settings.key_env else ""
    if settings.key_env and not api_key:
        raise SpeechError(
            "speech_not_configured",
            f"speaking needs {settings.key_env} in the lodge's .env; send the message as text",
        )
    try:
        OUTBOX_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # Before the words are spoken: there would be nowhere to keep the audio.
        raise write_failure(exc) from None
    try:
        OUTBOX_DIR.chmod(0o700)  # as outbox.stage: mkdir leaves an existing dir alone
    except OSError:
        pass  # not ours to change; the audio is 0600 either way
    path = OUTBOX_DIR / speech.entry_id
    size = await synthesize(
        speech.text, sender.speech_format, path, api_key=api_key,
        model=settings.model, voice=wolt_speech.get("voice", settings.voice),
        max_bytes=sender.max_attachment_bytes,
        url=settings.endpoint, key_env=settings.key_env, local=settings.local,
        instructions=wolt_speech.get("instructions"),
    )
    filename, content_type = SPEECH_FILES[sender.speech_format]
    return Attachment(path, filename, size, content_type, "voice")


def _own_chats(adapter: str, session_record: dict | None) -> set[str]:
    """The chats that are this lodge's own: the ones it is set up with, and the session's."""
    chats = set(_outbound(adapter).owner_chats())
    if session_record and session_record.get("adapter") == adapter:
        outbound = _outbound(adapter)
        if outbound.bound_chat is not None:
            # A declared connector's own route, read by its codec: exact, all fields.
            chats.add(outbound.bound_chat(session_record) or "")
        else:
            chats.add(str(session_record.get("chat_id") or ""))
    chats.discard("")
    return chats


async def _send(
    adapter: str, session: str, message: str, chat_id: str,
    thread_ts: str | None = None, routing: dict | None = None,
    attachments: tuple[Attachment, ...] = (), speech: _Speech | None = None,
    session_record: dict | None = None,
) -> dict:
    """Resolve core context, dispatch a built-in, and persist its declared updates.

    `routing` is the session's record only when the route is the session's own
    thread, and carries its progress state. `session_record` is that record
    whatever the route, and is read only to tell whose chat this is.
    """
    sender = _sender(adapter)
    secrets = {key: dotenv_env(key) for key in sender.secrets}
    destination, chat_id, session_link = _outbound(adapter).destination(
        session, chat_id, thread_ts, routing, secrets,
    )

    if (attachments or speech is not None) and str(chat_id) not in _own_chats(
        adapter, session_record,
    ):
        # Text may go to any chat a caller names. A file can be anything on this
        # machine, so it goes only where the lodge already talks to its human.
        raise AttachmentError(
            "attachment_destination",
            f"a file goes only to this lodge's own chats, and {adapter} {chat_id} is not "
            "one of them; send it to your own chat, or send text only",
        )

    speech_failure = None
    if speech is not None:
        # Only now that there is somewhere to send it: every spoken message is
        # an OpenAI request billed to the lodge's key, and its words leave the lodge.
        try:
            attachments = (await _spoken(
                speech, sender, adapter, (session_record or {}).get("wolt") or "",
            ),)
        except SpeechError as exc:
            if exc.reason != "speech_failed":
                raise
            # The service failed even after a retry. The words still reach the
            # human, as text, rather than nothing at all.
            speech_failure = str(exc)
            print(
                f"[notify] {time.strftime('%Y-%m-%dT%H:%M:%S%z')} speech failed, "
                f"sending text instead: session={session or '-'} {adapter}={chat_id}: {exc}"
            )
            message = f"{message.rstrip()}\n\n(voice note failed; sent as text)"

    for attachment in attachments:
        if sender.max_attachment_bytes == 0:
            raise AttachmentError("attachments_unsupported", f"{adapter} cannot carry files")
        if attachment.size > sender.max_attachment_bytes:
            raise AttachmentError(
                "attachment_too_large",
                f"{attachment.filename} is {attachment.size} bytes; "
                f"{adapter} accepts at most {sender.max_attachment_bytes}",
            )

    context = SendContext(
        secrets,
        {key: routing[key] for key in sender.route_state if routing and key in routing},
    )
    try:
        await sender.send(
            OutboundMessage(message, session_link or None, attachments), destination, context,
        )
    finally:
        updates = {}
        for key, value in context.updates.items():
            if key in sender.route_state:
                updates[key] = value
            else:
                logger.warning("Ignored undeclared notification state key: %s", key)
        if updates and session and routing:
            SessionRegistry(WOLTS_DIR).update(session, wolt=routing.get("wolt", ""), **updates)
    append_chat_history(adapter, chat_id, message + "".join(
        f"\n[{'voice note' if a.kind == 'voice' else 'file'} sent: "
        f"{a.filename}, {a.content_type}, {a.size} bytes]"
        for a in attachments
    ))
    result = {"adapter": adapter, _outbound(adapter).route_key: chat_id}
    if attachments:
        # Metadata only: this dict is logged and returned to the caller. A voice
        # note is marked, so `notify` can tell it from audio that went as a document.
        result["attachments"] = [
            {"name": a.filename, "content_type": a.content_type, "size": a.size}
            | ({"kind": "voice"} if a.kind == "voice" else {})
            for a in attachments
        ]
    if speech_failure:
        result["speech_failed"] = speech_failure
    return result


async def _send_telegram(session: str, message: str, chat_id: str) -> dict:
    """Compatibility wrapper; routing and local state remain in core."""
    return await _send("telegram", session, message, chat_id)


async def _send_slack(
    message: str, channel: str, thread_ts: str | None = None, *,
    session: str = "", routing: dict | None = None,
) -> dict:
    """Compatibility wrapper; routing and local state remain in core."""
    return await _send("slack", session, message, channel, thread_ts, routing)


async def send_notification(
    session: str, message: str, explicit: dict | None = None, attachments=(), speak=None,
) -> dict:
    """Send a notification. Explicit routing takes priority over session lookup.

    explicit dict can contain:
      {"adapter": "slack", "channel": "C123", "thread_ts": "1234.5678"}
      {"adapter": "telegram", "chat_id": "98765"}

    attachments is what the request carried: [{"id": <outbox entry>, "name": ...}],
    with "kind": "voice" on audio that is to arrive as a voice note.
    Every entry it names is removed from the outbox, whatever happens to the send.

    speak is the request's own value, expected {"text": <the words to say>}. The
    lodge has them spoken and sends the audio as a voice note with `message` as
    its text; that audio is removed from the outbox too.
    """
    try:
        outbox.sweep(OUTBOX_DIR)
    except Exception as exc:
        logger.warning("Could not sweep the outbox: %s", exc)
    speech = None
    try:
        speech = _speech_request(speak, attachments)
        return await _route(
            session, message, explicit, _resolve_attachments(attachments), speech,
        )
    finally:
        if isinstance(attachments, (list, tuple)):
            for entry in attachments:
                if isinstance(entry, dict):
                    outbox.discard(OUTBOX_DIR, entry.get("id"))
        if speech is not None:
            outbox.discard(OUTBOX_DIR, speech.entry_id)


async def _route(
    session: str, message: str, explicit: dict | None, attachments: tuple[Attachment, ...],
    speech: _Speech | None = None,
) -> dict:
    """Pick the destination: explicit route, then the session's, then the Telegram default."""
    routing = read_session_registry(session) if session else None

    # 1. Explicit routing — caller knows exactly where to send.
    if explicit and explicit.get("adapter"):
        adapter = explicit["adapter"]
        outbound = _outbound(adapter)
        plan = outbound.explicit(session, explicit, routing) if outbound else None
        if plan is not None:
            return await _send(
                adapter, plan["session"], message, plan["chat_id"], plan["thread_ts"],
                plan["routing"], attachments=attachments, speech=speech, session_record=routing,
            )

    # 2. Session registry lookup — find routing from session metadata
    if session and routing:
        adapter = routing.get("adapter")
        outbound = _outbound(adapter)
        target = outbound.from_session(routing) if outbound else None
        if target is not None:
            chat_id, thread_ts = target
            return await _send(
                adapter, session, message, chat_id, thread_ts, routing,
                attachments=attachments, speech=speech, session_record=routing,
            )

    # 3. A connector's default chat — today Telegram's first allowed user
    for adapter, outbound in OUTBOUND.items():
        chat_id = outbound.default_chat() if outbound.default_chat else None
        if chat_id:
            return await _send(
                adapter, session, message, chat_id, attachments=attachments,
                speech=speech, session_record=routing,
            )

    raise NoNotificationTarget(
        "no notification target — set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS "
        f"in {WOLTS_DIR}/.env, or connect a chat with the /telegram skill"
    )
