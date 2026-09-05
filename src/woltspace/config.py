"""Native data-root configuration.

The control plane reads channel settings from the data root it owns, not from a
container-era `.env` beside the source checkout. Precedence, highest first:

1. process environment (how the container entrypoint and ad-hoc runs override);
2. `<wolts_dir>/.space/platform/config.json` (the native surface);
3. built-in defaults.

The file is only ever read here. Nothing in this module writes a credential.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from .layout import RuntimeLayout

CONFIG_FILENAME = "config.json"


def config_path(layout: RuntimeLayout, env: Mapping[str, str] | None = None) -> Path:
    values = os.environ if env is None else env
    override = values.get("WOLTSPACE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return layout.platform_state / CONFIG_FILENAME


def load_config(layout: RuntimeLayout, env: Mapping[str, str] | None = None) -> dict:
    """Return the parsed config, or an empty mapping when absent/unreadable."""
    try:
        data = json.loads(config_path(layout, env).read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def channel_config(
    layout: RuntimeLayout, name: str, env: Mapping[str, str] | None = None
) -> dict:
    channels = load_config(layout, env).get("channels")
    if not isinstance(channels, dict):
        return {}
    section = channels.get(name)
    return section if isinstance(section, dict) else {}
