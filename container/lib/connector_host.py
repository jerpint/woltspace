"""Starts the lodge's connector broker inside the server process (RFC #617).

The launcher (the channel supervisor) decides which folder connectors run and
who owns each one, and writes that to `.space/run/connectors/lodge.json`
(0600). The server reads it at startup and serves one socket per connector,
each answered by a ChatLodge. Owner ids come from the lodge's own settings,
never from a connector's manifest.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import connector_broker

logger = logging.getLogger(__name__)

LODGE_FILE = "lodge.json"


def run_dir(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "run" / "connectors"


def write_lodge_file(wolts_dir, entries: list[dict]) -> Path:
    """[{name, owners}] for the connectors the launcher starts; replaced each start."""
    directory = connector_broker.prepare_run_dir(run_dir(wolts_dir))
    path = directory / LODGE_FILE
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"connectors": entries}, handle)
    os.replace(tmp, path)
    return path


def read_lodge_file(wolts_dir) -> list[dict]:
    try:
        data = json.loads((run_dir(wolts_dir) / LODGE_FILE).read_text())
    except (OSError, ValueError):
        return []
    entries = data.get("connectors") if isinstance(data, dict) else None
    return [e for e in entries or [] if isinstance(e, dict) and isinstance(e.get("name"), str)]


async def start(wolts_dir) -> connector_broker.Broker | None:
    """Serve every connector the launcher planned; None when there are none."""
    entries = read_lodge_file(wolts_dir)
    if not entries:
        return None
    # The lodge's handlers live beside this library (container/bot, container/lib).
    container = str(Path(__file__).resolve().parents[1])
    if container not in sys.path:
        sys.path.insert(0, container)
    import chat_lodge
    import connector_events
    import connector_registry
    import lodge_commands

    broker = connector_broker.Broker(
        run_dir(wolts_dir), lambda name, event_id: connector_events.record(wolts_dir, name, event_id))
    menu = [(c.name, c.description) for c in lodge_commands.COMMANDS]
    for entry in entries:
        spec = connector_registry.spec(entry["name"], wolts_dir)
        if spec is None or not spec.launchable:
            logger.warning("connector %s is planned but has no folder; not served", entry["name"])
            continue
        owners = [str(o) for o in entry.get("owners") or []]
        lodge = chat_lodge.ChatLodge(spec, owners, help_tips=spec.help)
        broker.add(spec.name, lodge.handle, menu)
    await broker.start()
    connector_broker.install(broker)
    return broker


async def stop() -> None:
    broker = connector_broker.current()
    if broker is not None:
        connector_broker.install(None)
        await broker.stop()
