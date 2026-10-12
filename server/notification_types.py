"""The internal outbound sender boundary; no provider or storage dependencies."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Attachment:
    """One file to deliver. Core builds it; senders only read `path`."""

    path: Path
    filename: str
    size: int
    content_type: str
    # "file" travels as a document. "voice" asks for a voice note where the
    # provider has one, and is an ordinary audio file where it has not.
    kind: str = "file"


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    session_link: str | None = None
    attachments: tuple[Attachment, ...] = ()


@dataclass
class SendContext:
    secrets: Mapping[str, str]
    route_state: Mapping[str, str]
    updates: dict[str, str] = field(default_factory=dict)


Send = Callable[[OutboundMessage, Mapping[str, str], SendContext], Awaitable[None]]


@dataclass(frozen=True)
class Plugin:
    id: str
    send: Send
    secrets: tuple[str, ...] = ()
    route_state: tuple[str, ...] = ()
    # Largest file this sender can carry, in bytes. 0 means it cannot carry files.
    max_attachment_bytes: int = 0
    # The audio format this sender wants for a message core speaks aloud:
    # "opus" or "mp3". Empty means it cannot carry a spoken message.
    speech_format: str = ""
