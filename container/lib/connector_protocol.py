"""The wire between a connector process and the lodge (RFC #617).

JSON lines over a unix socket the lodge owns. A connector never imports the
lodge and the lodge never imports a connector: this module (frames, limits,
checks) is all they share. Pure: no sockets, no storage.

Connector to lodge:
  hello        {v, name, token}                      first frame, once
  event        {id, kind, route, sender, body}       one owner event
  send-result  {id, status, error, message_id}       answer to a `send`
  ping         {id}

Lodge to connector:
  welcome      {v, commands, help}                   answer to hello
  receipt      {event, status}                       every event: accepted / duplicate / failed
  say          {event, route, reply, photo}          a new message in the chat
  edit         {event, reply}                        new body for the message a selection came from
  ack          {event, ack}                          the selection's answer (a toast)
  send         {id, route, method, text, path, filename, content_type}   from notify
  pong         {id}
  bye          {reason}                              the lodge closes this connection

A receipt means "accepted for one dispatch attempt", not "delivered": the
lodge commits the event's dedup key, answers, then dispatches once.
"""

from __future__ import annotations

import json
import re

from connector_contract import Ack, Choice, Reply

VERSION = 1

# One frame is one line. Big enough for a long picker; far below what could
# hurt the lodge. Payloads (files, audio) travel as paths, never as bytes.
MAX_FRAME_BYTES = 256 * 1024
MAX_TEXT = 64 * 1024
MAX_ID = 128
MAX_ARGS = 32
MAX_CHOICE_ROWS = 100
MAX_ROUTE_FIELDS = 4

EVENT_KINDS = frozenset({"message", "command", "selection", "attachment"})
MEDIA = frozenset({"voice", "image", "video", "file"})
RECEIPTS = frozenset({"accepted", "duplicate", "failed"})
SEND_METHODS = frozenset({"message", "document", "voice", "audio"})
SEND_RESULTS = frozenset({"delivered", "refused", "failed", "ambiguous"})

FROM_CONNECTOR = frozenset({"hello", "event", "send-result", "ping"})
FROM_LODGE = frozenset({"welcome", "receipt", "say", "edit", "ack", "send", "pong", "bye"})

_ID = re.compile(r"[A-Za-z0-9_.:@-]{1,%d}" % MAX_ID)
_FIELD = re.compile(r"[a-z][a-z0-9_]{0,31}")


class FrameError(ValueError):
    """A frame the other side must not act on. The connection is closed."""


# --- encoding ----------------------------------------------------------------

def encode(frame: dict) -> bytes:
    line = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    if len(line) > MAX_FRAME_BYTES:
        raise FrameError(f"{frame.get('type')} frame is {len(line)} bytes; at most {MAX_FRAME_BYTES}")
    return line


def decode(line: bytes, allowed: frozenset[str]) -> dict:
    """One received line as a checked frame, or FrameError."""
    if len(line) > MAX_FRAME_BYTES:
        raise FrameError("frame too large")
    try:
        frame = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        raise FrameError("frame is not JSON") from None
    if not isinstance(frame, dict):
        raise FrameError("frame is not an object")
    kind = frame.get("type")
    if kind not in allowed:
        raise FrameError(f"unexpected frame type {kind!r}")
    _CHECKS[kind](frame)
    return frame


# --- neutral replies on the wire ----------------------------------------------

def reply_to_wire(reply: Reply) -> dict:
    choices = None
    if reply.choices is not None:
        choices = [[{k: v for k, v in (("label", c.label), ("action", c.action), ("url", c.url))
                     if v is not None} for c in row] for row in reply.choices]
    return {"text": reply.text, "choices": choices, "markdown": reply.markdown}


def reply_from_wire(data) -> Reply:
    _check_reply(data)
    choices = data.get("choices")
    if choices is not None:
        choices = tuple(tuple(Choice(c["label"], c.get("action"), c.get("url")) for c in row)
                        for row in choices)
    return Reply(data["text"], choices, bool(data.get("markdown")))


def ack_to_wire(ack: Ack) -> dict:
    return {"text": ack.text, "alert": ack.alert}


def ack_from_wire(data) -> Ack:
    if not isinstance(data, dict):
        raise FrameError("ack is not an object")
    text, alert = data.get("text"), data.get("alert")
    if text is not None:
        _text(text, "ack text")
    if alert is not None and not isinstance(alert, bool):
        raise FrameError("ack alert must be true, false or null")
    return Ack(text, alert)


# --- checks --------------------------------------------------------------------

def _text(value, what: str, limit: int = MAX_TEXT, empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value):
        raise FrameError(f"{what} must be a string of at most {limit} characters")
    return value


def _id(value, what: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise FrameError(f"{what} is not a valid id")
    return value


def _optional_id(value, what: str) -> None:
    if value is not None:
        _id(value, what)


def _route(value) -> None:
    if not isinstance(value, dict) or not 0 < len(value) <= MAX_ROUTE_FIELDS:
        raise FrameError("route must be an object of 1 to 4 fields")
    for key, item in value.items():
        if not isinstance(key, str) or not _FIELD.fullmatch(key):
            raise FrameError("route field names are lowercase words")
        _text(item, f"route {key}", 256, empty=False)


def _check_reply(data) -> None:
    if not isinstance(data, dict):
        raise FrameError("reply is not an object")
    _text(data.get("text"), "reply text")
    if not isinstance(data.get("markdown", False), bool):
        raise FrameError("reply markdown must be a boolean")
    choices = data.get("choices")
    if choices is None:
        return
    if not isinstance(choices, list) or len(choices) > MAX_CHOICE_ROWS:
        raise FrameError("reply choices must be a list of rows")
    for row in choices:
        if not isinstance(row, list) or len(row) > 8:
            raise FrameError("a choice row is a list of at most 8 choices")
        for choice in row:
            if not isinstance(choice, dict):
                raise FrameError("a choice is an object")
            _text(choice.get("label"), "choice label", 256)
            for key in ("action", "url"):
                if choice.get(key) is not None:
                    _text(choice[key], f"choice {key}", 2048)


def _check_hello(f: dict) -> None:
    if f.get("v") != VERSION:
        raise FrameError(f"unsupported protocol version {f.get('v')!r}")
    _text(f.get("name"), "name", 32, empty=False)
    _text(f.get("token"), "token", 256, empty=False)


def _check_reply_to(value) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise FrameError("reply_to is an object")
    if not isinstance(value.get("from_bot"), bool):
        raise FrameError("reply_to.from_bot must be a boolean")
    _text(value.get("text", ""), "reply_to text")
    if value.get("file_name") is not None:
        _text(value["file_name"], "reply_to file_name", 512)
    if not isinstance(value.get("voice", False), bool):
        raise FrameError("reply_to.voice must be a boolean")


def _check_event(f: dict) -> None:
    _id(f.get("id"), "event id")
    kind = f.get("kind")
    if kind not in EVENT_KINDS:
        raise FrameError(f"unknown event kind {kind!r}")
    _route(f.get("route"))
    _text(f.get("sender"), "sender", 128, empty=False)
    body = f.get("body")
    if not isinstance(body, dict):
        raise FrameError("event body is an object")
    for flag in ("group", "addressed", "mention"):
        if not isinstance(body.get(flag, False), bool):
            raise FrameError(f"body.{flag} must be a boolean")
    if kind == "message":
        _text(body.get("text"), "message text")
        _check_reply_to(body.get("reply_to"))
    elif kind == "command":
        _text(body.get("name"), "command name", 32, empty=False)
        args = body.get("args", [])
        if not isinstance(args, list) or len(args) > MAX_ARGS:
            raise FrameError(f"command args are a list of at most {MAX_ARGS}")
        for arg in args:
            _text(arg, "command argument", 1024)
    elif kind == "selection":
        _text(body.get("action"), "selection action", 2048)
    elif kind == "attachment":
        if body.get("media") not in MEDIA:
            raise FrameError("attachment media must be voice, image, video or file")
        _text(body.get("path"), "attachment path", 4096, empty=False)
        _text(body.get("name"), "attachment name", 512, empty=False)
        _text(body.get("mime"), "attachment mime", 255, empty=False)
        if not isinstance(body.get("size"), int) or isinstance(body.get("size"), bool) or body["size"] < 0:
            raise FrameError("attachment size is a non-negative integer")
        _text(body.get("caption", ""), "attachment caption")
        _check_reply_to(body.get("reply_to"))


def _check_send_result(f: dict) -> None:
    _id(f.get("id"), "send id")
    if f.get("status") not in SEND_RESULTS:
        raise FrameError("send-result status is delivered, refused, failed or ambiguous")
    if f.get("error") is not None:
        _text(f["error"], "send-result error", 2048)
    if f.get("message_id") is not None:
        _text(f["message_id"], "message_id", 128)


def _check_ping(f: dict) -> None:
    _id(f.get("id"), "ping id")


def _check_welcome(f: dict) -> None:
    if f.get("v") != VERSION:
        raise FrameError(f"unsupported protocol version {f.get('v')!r}")
    commands = f.get("commands")
    if not isinstance(commands, list):
        raise FrameError("welcome commands is a list")
    for entry in commands:
        if not (isinstance(entry, list) and len(entry) == 2):
            raise FrameError("a command is [name, description]")
        _text(entry[0], "command name", 32, empty=False)
        _text(entry[1], "command description", 256)


def _check_receipt(f: dict) -> None:
    _id(f.get("event"), "receipt event")
    if f.get("status") not in RECEIPTS:
        raise FrameError("receipt status is accepted, duplicate or failed")


def _check_say(f: dict) -> None:
    _optional_id(f.get("event"), "say event")
    _route(f.get("route"))
    _check_reply(f.get("reply"))
    if f.get("photo") is not None:
        _text(f["photo"], "photo path", 4096, empty=False)


def _check_edit(f: dict) -> None:
    _id(f.get("event"), "edit event")
    _check_reply(f.get("reply"))


def _check_ack(f: dict) -> None:
    _id(f.get("event"), "ack event")
    ack_from_wire(f.get("ack"))


def _check_send(f: dict) -> None:
    _id(f.get("id"), "send id")
    _route(f.get("route"))
    if f.get("method") not in SEND_METHODS:
        raise FrameError("send method is message, document, voice or audio")
    _text(f.get("text", ""), "send text")
    if f["method"] != "message":
        _text(f.get("path"), "send path", 4096, empty=False)
        _text(f.get("filename"), "send filename", 512, empty=False)
        _text(f.get("content_type"), "send content type", 255, empty=False)


def _check_bye(f: dict) -> None:
    _text(f.get("reason", ""), "bye reason", 512)


_CHECKS = {
    "hello": _check_hello,
    "event": _check_event,
    "send-result": _check_send_result,
    "ping": _check_ping,
    "welcome": _check_welcome,
    "receipt": _check_receipt,
    "say": _check_say,
    "edit": _check_edit,
    "ack": _check_ack,
    "send": _check_send,
    "pong": _check_ping,
    "bye": _check_bye,
}
