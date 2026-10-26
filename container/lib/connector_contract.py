"""The chat connector contract (RFC #617): what a connector declares and exchanges.

A connector translates one chat provider (Telegram, Slack, ...) to and from
the lodge. The lodge decides: commands, owner checks, wolt selection, routing
and sessions. The connector verifies provider identity, turns provider events
into the typed events below, and renders the lodge's neutral replies.

Pure types and helpers: no provider SDK, no storage, no network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

PROTOCOL_VERSION = 1

# What a connector can do. The lodge only asks for what a connector declares.
CAPABILITIES = frozenset({
    "text", "files", "voice_in", "voice_out", "images", "threads", "edit",
    "status", "read_receipts", "reply_to", "reactions",
})
# How owner commands reach the lodge through a connector.
COMMAND_INPUTS = frozenset({"native", "text", "buttons"})
EVENT_TYPES = frozenset({"message", "command", "selection", "attachment", "reaction"})


class ManifestError(ValueError):
    """A manifest the lodge cannot use. Unknown versions fail closed."""


@dataclass(frozen=True)
class Manifest:
    """What a connector declares about itself."""

    name: str
    protocol: int = PROTOCOL_VERSION
    capabilities: frozenset[str] = frozenset({"text"})
    command_input: frozenset[str] = frozenset({"text"})
    # The prefix of a typed command; only used with "text" command input.
    command_prefix: str = "/"
    events: frozenset[str] = frozenset({"message", "command"})
    # The fields that make one destination, in order (e.g. ("chat_id",)).
    route_fields: tuple[str, ...] = ()
    # The .env names this connector needs. Only these are handed to it.
    secrets: tuple[str, ...] = ()
    # The .env name listing the owner's provider ids, if the connector uses one.
    owner_env: str = ""

    def __post_init__(self):
        if self.protocol != PROTOCOL_VERSION:
            raise ManifestError(f"{self.name}: unsupported connector protocol {self.protocol}")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", self.name or ""):
            raise ManifestError(f"invalid connector name {self.name!r}")
        for label, values, known in (
            ("capability", self.capabilities, CAPABILITIES),
            ("command input", self.command_input, COMMAND_INPUTS),
            ("event type", self.events, EVENT_TYPES),
        ):
            unknown = set(values) - known
            if unknown:
                raise ManifestError(f"{self.name}: unknown {label} {sorted(unknown)}")
        if "text" in self.command_input and not re.fullmatch(r"[/!.]", self.command_prefix):
            raise ManifestError(f"{self.name}: command prefix must be one of / ! .")

    def can(self, capability: str) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class Route:
    """One destination, tagged with its connector so routes never mix."""

    connector: str
    conversation_id: str
    thread_id: str | None = None
    account_id: str = ""

    def key(self) -> str:
        """A stable conversation key: same chat and thread, same key.

        JSON-encoded, so no field can smuggle a separator into another.
        """
        return json.dumps([self.connector, self.account_id, self.conversation_id, self.thread_id],
                          separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class Choice:
    """One option in a reply: an action the lodge understands, or a link."""

    label: str
    action: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Reply:
    """A neutral reply. `markdown` means light markdown: **bold** and `code`.

    `choices` are rows of options. A connector renders them as buttons where it
    can, and as a numbered list where it cannot. An empty tuple is a picker
    that has nothing to offer right now; None is a reply that never offers any.
    """

    text: str
    choices: tuple[tuple[Choice, ...], ...] | None = None
    markdown: bool = False


@dataclass(frozen=True)
class Ack:
    """A short acknowledgement of a selection (a toast where the provider has one)."""

    text: str | None = None
    # None leaves the provider's default; True asks for a blocking alert.
    alert: bool | None = None


@dataclass(frozen=True)
class Outcome:
    """What a selection produced: an ack, and maybe a new body for its message."""

    ack: Ack | None = None
    edit: Reply | None = None
    # Set when the selection could not edit its message and a new one is due.
    reply: Reply | None = None


@dataclass(frozen=True)
class Command:
    """A typed command event: the owner asked the lodge to do something."""

    name: str
    args: tuple[str, ...] = ()


@dataclass(frozen=True)
class Selection:
    """The owner picked a choice the lodge offered (a button, a numbered item)."""

    action: str


@dataclass(frozen=True)
class Reaction:
    """The owner reacted to a message (e.g. 👍)."""

    emoji: str
    message_id: str = ""


@dataclass(frozen=True)
class Message:
    """Plain text for the active wolt's session."""

    text: str
    message_id: str = ""
    reply_to: str = ""
    attachments: tuple[dict, ...] = field(default_factory=tuple)


# --- typed commands from plain text ------------------------------------------

def parse_command(text: str, names, prefix: str = "/", bot_username: str = "") -> Command | None:
    """A Command when `text` is an exact registered command, else None.

    The safety rules of RFC #617, so every text connector gets them:
    - only an exact registered name at the very start counts, so a path such as
      /Users/me or an engine command such as /compact stays plain text;
    - a doubled prefix ("//wolt") or a leading space escapes: plain text;
    - a "name@bot" suffix counts only when it names this bot.
    Owner checks are not here: the lodge runs a command only for its owner.
    """
    if not isinstance(text, str) or not text.startswith(prefix):
        return None
    if text.startswith(prefix * 2):
        return None
    head, _, rest = text[len(prefix):].partition(" ")
    if "\n" in head:
        head, _, tail = head.partition("\n")
        rest = f"{tail} {rest}".strip()
    name, _, target = head.partition("@")
    if target and (not bot_username or target.lower() != bot_username.lower()):
        return None
    name = name.lower()
    if name not in set(names):
        return None
    return Command(name, tuple(rest.split()))


def plain_text_reply(reply: Reply) -> str:
    """A reply for connectors without buttons: choices become a numbered list."""
    text = reply.text.replace("**", "")
    numbered = []
    for row in reply.choices or ():
        for choice in row:
            if choice.action or choice.url:
                numbered.append(choice)
    if not numbered:
        return text
    lines = [f"{i}. {c.label}" + (f" ({c.url})" if c.url else "") for i, c in enumerate(numbered, 1)]
    return text + "\n\n" + "\n".join(lines)


# --- routes as text, owned by each connector -----------------------------------

@dataclass(frozen=True)
class RouteCodec:
    """How one connector spells, checks and binds its destinations.

    A route is a dict of the connector's own fields (Telegram: chat_id; Slack:
    channel and thread_ts). The codec is the only place that knows their shape:
    the lodge never guesses a route from a regex of its own.
    """

    # The fields of a route, in the order they are written.
    fields: tuple[str, ...]
    # "123" or "C1:1712.5" -> {"chat_id": "123"}; raises ValueError when malformed.
    parse: object
    # The session record's own route, or None when it has none for this connector.
    from_record: object
    # The legacy CLI flag and its arguments, when the connector has one (--telegram 123).
    legacy_args: object = None

    def format(self, route: dict) -> str:
        return ":".join(str(route[field]) for field in self.fields)

    def canonical(self, text: str) -> dict:
        """Parse `text`, and refuse anything that does not read back the same."""
        route = self.parse(text)
        if not isinstance(route, dict) or set(route) != set(self.fields):
            raise ValueError("route does not have this connector's fields")
        if self.format(route) != text:
            raise ValueError("route is not in its canonical form")
        return route

    def matches(self, route: dict, record: dict | None) -> bool:
        """Exact binding: the route is the session record's own route."""
        bound = self.from_record(record or {}) if record else None
        return bound is not None and bound == route


@dataclass(frozen=True)
class ConnectorSpec:
    """A connector as the lodge knows it: its manifest and how its parts are found.

    `launch` (a module run as `python -m`) and `outbound` (a "module:attr" whose
    attribute is the connector's sender Plugin) are import paths. The lodge
    resolves them only for connectors listed in its own registry; a connector
    from outside the repository will talk to the lodge over a socket instead.
    """

    manifest: Manifest
    codec: RouteCodec
    title: str
    launch: str = ""
    outbound: str = ""
    # The import the launched process needs (a provider SDK), checked before start.
    dependency: str = ""
    # Each declared secret's key in the data root's channels.<name> config.
    config_keys: tuple[tuple[str, str], ...] = ()
    # Turns the connector on from the environment, at the platform entrypoint only.
    enable_env: str = ""

    @property
    def name(self) -> str:
        return self.manifest.name

    def reply_args(self, route: dict) -> list[str]:
        """The notify arguments that reply on `route`: the legacy flag when the
        connector has one (a stable alias), else the generic --to form."""
        if self.codec.legacy_args:
            return list(self.codec.legacy_args(route))
        return ["--to", f"{self.name}:{self.codec.format(route)}"]
