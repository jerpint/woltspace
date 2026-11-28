"""Local, interactive setup for the bundled WhatsApp connector."""
from __future__ import annotations

import getpass
import json
import os
import secrets
import sys
import time
import warnings
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import dotenv_values

from .config import config_path
from .instance import inspect_instance
from .layout import RuntimeLayout

UPDATES = "https://api.whatsapp.com/agent/v1/updates"


class SetupError(ValueError):
    pass


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def valid_creator(value: object) -> bool:
    # Owner IDs are passed to the connector as a comma-separated list.
    return (isinstance(value, str) and value.startswith("user:") and 5 < len(value) <= 128
            and value.isascii() and "," not in value
            and all(c.isprintable() and not c.isspace() for c in value))


def discover_creator(key: str, marker: str, not_before: int) -> str:
    opener = build_opener(RejectRedirects())
    offset = 0
    for _ in range(4):
        query = urlencode({"offset": offset, "limit": 50, "timeout": 8})
        request = Request(f"{UPDATES}?{query}", headers={"Authorization": f"Bearer {key}"})
        try:
            with opener.open(request, timeout=13) as response:
                if response.status == 204:
                    continue
                data = json.loads(response.read(1024 * 1024))
        except HTTPError as exc:
            if exc.code == 409:
                raise SetupError("Another process is polling this WhatsApp agent. Stop it and retry.") from None
            raise SetupError(f"WhatsApp returned HTTP {exc.code}. Check the Agent Platform key.") from None
        except (URLError, TimeoutError, ValueError):
            raise SetupError("Could not read updates from WhatsApp. Check the connection and retry.") from None
        try:
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    for message in change.get("value", {}).get("messages", []):
                        sender = message.get("from")
                        text = message.get("text")
                        if (message.get("type") == "text" and isinstance(text, dict)
                                and text.get("body") == marker and valid_creator(sender)
                                and int(message.get("timestamp", "0")) >= not_before):
                            return sender
            next_offset = data.get("next_offset")
        except (AttributeError, TypeError, ValueError):
            raise SetupError("WhatsApp returned an unusable update. Retry setup.") from None
        if type(next_offset) is int and next_offset >= offset:
            offset = next_offset
    raise SetupError("The marker did not arrive. Verify you sent it in the test agent chat and retry.")


def _read_config(path: Path) -> tuple[dict, dict, dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (ValueError, OSError):
        raise SetupError("The lodge config could not be read. Check it before setting up WhatsApp.") from None
    channels = data.get("channels", {}) if isinstance(data, dict) else None
    current = channels.get("whatsapp", {}) if isinstance(channels, dict) else None
    if not isinstance(current, dict):
        raise SetupError("The lodge config has an invalid channels or WhatsApp section.")
    return data, channels, current


def _check_creator_binding(layout: RuntimeLayout, key: str, creator: str) -> None:
    # Load the shipped state reader from this install, never from a lodge's local connector.
    path = layout.install_root / "container/connectors/whatsapp/whatsapp_binding.py"
    spec = spec_from_file_location("_woltspace_whatsapp_binding", path)
    if spec is None or spec.loader is None:
        raise SetupError("The bundled WhatsApp binding check is unavailable. Reinstall Woltspace.")
    binding = module_from_spec(spec)
    try:
        spec.loader.exec_module(binding)
    except OSError:
        raise SetupError("The bundled WhatsApp binding check is unavailable. Reinstall Woltspace.") from None
    try:
        binding.read_creator(layout.state_root / "whatsapp", binding.account_for_key(key), expected=creator)
    except binding.BindingError as exc:
        raise SetupError(str(exc)) from None


def configure(layout: RuntimeLayout) -> str:
    """Collect private input and save the channel settings. Return the next step."""
    if str(layout.runtime_lib) not in sys.path:
        sys.path.insert(0, str(layout.runtime_lib))
    import connector_registry
    from settings_files import settings_lock, write_json_atomic

    spec = connector_registry.spec("whatsapp", layout.wolts_dir)
    if not spec or not spec.launchable:
        raise SetupError("This Woltspace installation does not include the WhatsApp connector.")
    if not sys.stdin.isatty():
        raise SetupError("Run woltspace whatsapp setup in a private interactive terminal.")

    def live_status():
        instance = inspect_instance(layout)
        if instance["state"] in ("conflict", "starting"):
            raise SetupError("Wait for this lodge to finish starting or resolve the port conflict first.")
        active = any(item.get("name") == "whatsapp" and item.get("state") in ("running", "degraded")
                     for item in (instance.get("health") or {}).get("connectors", []))
        return instance["state"], active

    _, active = live_status()
    if active:
        return "WhatsApp is already running. Send /help in the agent chat to check it."

    dotenv_path = layout.wolts_dir / ".env"
    env = {**(dotenv_values(dotenv_path) if dotenv_path.is_file() else {}), **os.environ}
    path = config_path(layout, env)
    _, _, old = _read_config(path)
    flag = env.get("ENABLE_WHATSAPP_AGENT")
    if flag is not None and flag.strip().lower() not in ("1", "true", "yes", "on"):
        raise SetupError("ENABLE_WHATSAPP_AGENT disables WhatsApp. Remove that override before setup.")

    saved = old.get("agent_token")
    override = env.get("WHATSAPP_AGENT_TOKEN")
    prior_key = override or saved
    print(f"Lodge data: {layout.wolts_dir}")
    print(f"Config: {path}")
    print("Use an Agent Platform key that has no other running poller.")
    key_prompt = "Agent Platform API key (Enter keeps the current key): " if prior_key else "Agent Platform API key: "
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            key = getpass.getpass(key_prompt).strip() or prior_key
        except getpass.GetPassWarning:
            raise SetupError("This terminal cannot hide the API key. Open a private terminal and retry.") from None
    if not isinstance(key, str) or not key or not key.isascii() or not key.isprintable() or any(c.isspace() for c in key):
        raise SetupError("Enter a valid Agent Platform API key.")
    if override and key != override:
        raise SetupError("WHATSAPP_AGENT_TOKEN would override that key. Remove the override before changing keys.")

    owners = old.get("owners")
    saved_owner = (owners[0] if key == saved and isinstance(owners, list)
                   and len(owners) == 1 and valid_creator(owners[0]) else "")
    suffix = "Enter keeps the current ID" if saved_owner else "Enter discovers it from a message"
    creator = input(f"Full creator ID ({suffix}): ").strip() or saved_owner
    if not creator:
        marker = f"wa-setup-{secrets.token_hex(6)}"
        not_before = int(time.time())
        print(f"Send this exact message in the WhatsApp agent chat: {marker}")
        input("Press Enter after sending it. ")
        if live_status()[1]:
            raise SetupError("WhatsApp started during setup. Try /help in the agent chat.")
        creator = discover_creator(key, marker, not_before)
    if not valid_creator(creator):
        raise SetupError("The creator ID must be the full user: identifier.")

    state, active = live_status()
    if active:
        raise SetupError("WhatsApp started during setup. Try /help in the agent chat.")
    with settings_lock(path):
        data, channels, current = _read_config(path)
        if current != old:
            raise SetupError("The WhatsApp settings changed during setup. Re-run setup.")
        _check_creator_binding(layout, key, creator)
        channels["whatsapp"] = {**current, "enabled": True, "agent_token": key, "owners": [creator]}
        data["channels"] = channels
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json_atomic(path, data)

    if layout.isolation == "external":
        return "Saved WhatsApp setup. Restart this lodge with its host container tooling, then send /help."
    command = "woltspace restart" if state == "healthy" else "woltspace start"
    return f"Saved WhatsApp setup. Run {command} for this lodge, then send /help in WhatsApp."


def run() -> int:
    try:
        print(configure(RuntimeLayout.from_env()))
    except (EOFError, KeyboardInterrupt):
        print("WhatsApp setup cancelled.", file=sys.stderr)
        return 1
    except SetupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Could not save WhatsApp setup. Check the lodge config permissions.", file=sys.stderr)
        return 1
    return 0
