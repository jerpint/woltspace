"""The connectors this lodge knows (RFC #617).

Built-ins are listed here, each as a ConnectorSpec: its manifest, its route
codec, and where its process and its sender live. Light on purpose: no
provider SDK is imported, so the server, the CLI and the launcher can all read
what a connector declares without loading it.

Adding a built-in connector means adding its spec here; the launcher, the
notify CLI, the server's outbound path and a session's boot text read it from
this registry. Connectors from outside the repository will be found another way
(an entry point or a folder with a manifest) and are never imported into the
lodge: their import paths are not resolved, they talk over a socket.
"""

from __future__ import annotations

import re

from connector_contract import ConnectorSpec, Manifest, RouteCodec

_TELEGRAM_CHAT_ID = re.compile(r"-?[0-9]+")
_SLACK_CHANNEL = re.compile(r"[A-Za-z0-9_-]+")
_SLACK_THREAD = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def _telegram_parse(text: str) -> dict:
    if not isinstance(text, str) or _TELEGRAM_CHAT_ID.fullmatch(text) is None:
        raise ValueError("Telegram chat ID must be an integer")
    return {"chat_id": text}


def _telegram_from_record(record: dict) -> dict | None:
    chat_id = str(record.get("chat_id") or "")
    if record.get("adapter") != "telegram" or _TELEGRAM_CHAT_ID.fullmatch(chat_id) is None:
        return None
    return {"chat_id": chat_id}


def _slack_parse(text: str) -> dict:
    channel, sep, thread_ts = (text or "").partition(":")
    if not sep or _SLACK_CHANNEL.fullmatch(channel) is None:
        raise ValueError("Slack channel has invalid characters")
    if _SLACK_THREAD.fullmatch(thread_ts) is None:
        raise ValueError("Slack thread timestamp must be numeric")
    return {"channel": channel, "thread_ts": thread_ts}


def _slack_from_record(record: dict) -> dict | None:
    if record.get("adapter") != "slack":
        return None
    try:
        return _slack_parse(f"{record.get('chat_id') or ''}:{record.get('thread_ts') or ''}")
    except ValueError:
        return None


TELEGRAM = Manifest(
    name="telegram",
    capabilities=frozenset({"text", "files", "voice_in", "voice_out", "images", "edit", "reply_to", "status"}),
    command_input=frozenset({"native", "buttons"}),
    events=frozenset({"message", "command", "selection", "attachment"}),
    route_fields=("chat_id",),
    secrets=("TELEGRAM_BOT_TOKEN",),
    owner_env="TELEGRAM_ALLOWED_USERS",
)

SLACK = Manifest(
    name="slack",
    capabilities=frozenset({"text", "files", "voice_out", "threads", "status"}),
    command_input=frozenset({"buttons"}),
    events=frozenset({"message", "selection"}),
    route_fields=("channel", "thread_ts"),
    secrets=("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"),
    owner_env="SLACK_OWNER_USER",
)

# Telegram and Slack predate the registry: their launch plans and senders are
# their own classes (channels.py, server/notify.py), so `launch` and `outbound`
# stay empty here. Their codecs and boot text come from the registry.
SPECS: dict[str, ConnectorSpec] = {
    "telegram": ConnectorSpec(
        TELEGRAM,
        RouteCodec(("chat_id",), _telegram_parse, _telegram_from_record,
                   legacy_args=lambda r: ["--telegram", r["chat_id"]]),
        title="Telegram",
    ),
    "slack": ConnectorSpec(
        SLACK,
        RouteCodec(("channel", "thread_ts"), _slack_parse, _slack_from_record,
                   legacy_args=lambda r: ["--slack", r["channel"], r["thread_ts"]]),
        title="Slack",
    ),
}

# Kept for callers that only need manifests.
BUILTIN = {name: spec.manifest for name, spec in SPECS.items()}


def spec(name: str) -> ConnectorSpec | None:
    return SPECS.get(name)


def manifest(name: str) -> Manifest | None:
    found = SPECS.get(name)
    return found.manifest if found else None


def declared() -> list[ConnectorSpec]:
    """Specs whose parts are all declared (no class of their own elsewhere)."""
    return [s for s in SPECS.values() if s.launch]
