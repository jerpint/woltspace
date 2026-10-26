"""The connectors this lodge knows, by manifest (RFC #617).

Built-ins are listed here. Light on purpose: no provider SDK is imported, so
the server and the CLI can read what a connector declares without loading it.
Connectors outside the repository will be found the same way later (an entry
point or a folder with a manifest), never by importing them into the lodge.
"""

from connector_contract import Manifest

TELEGRAM = Manifest(
    name="telegram",
    capabilities=frozenset({"text", "files", "voice_in", "voice_out", "images", "edit", "reply_to", "status"}),
    command_input=frozenset({"native", "buttons"}),
    events=frozenset({"message", "command", "selection", "attachment"}),
    route_fields=("chat_id",),
    secrets=("TELEGRAM_BOT_TOKEN",),
    owner_env="TELEGRAM_ALLOWED_USERS",
)

BUILTIN = {m.name: m for m in (TELEGRAM,)}


def manifest(name: str) -> Manifest | None:
    return BUILTIN.get(name)
