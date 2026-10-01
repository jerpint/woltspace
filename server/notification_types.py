"""The internal outbound sender boundary; no provider or storage dependencies."""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class OutboundMessage:
    text: str
    session_link: str | None = None


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
