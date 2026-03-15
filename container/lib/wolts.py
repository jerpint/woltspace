"""
Wolt discovery — find wolts and their types.

Usage:
    from wolts import list_wolts, find_by_type, get_active_creature

    all_wolts = list_wolts()
    # [{"name": "neowolt", "type": "rodent", "role": "...", "dir": "/workspace/wolts/neowolt"}, ...]

    wolves = find_by_type("wolf")
    active_wolf = get_active_creature("wolf")  # name or None
"""

import json
import os
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
CONFIG_FILE = WOLTS_DIR / "woltspace.json"

# Valid creature types
VALID_TYPES = {"rodent", "wolf", "dog", "spider", "bear", "panda"}

# Types that can only have one active at a time
SINGLETON_TYPES = {"wolf", "dog"}


def list_wolts() -> list[dict]:
    """Discover all wolts by scanning wolt.json files."""
    wolts = []
    for wolt_json in sorted(WOLTS_DIR.glob("*/wolt/wolt.json")):
        try:
            data = json.loads(wolt_json.read_text())
            data.setdefault("type", "rodent")
            data["dir"] = str(wolt_json.parent.parent)
            wolts.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    return wolts


def find_by_type(creature_type: str) -> list[dict]:
    """Find all wolts of a given type."""
    return [w for w in list_wolts() if w.get("type") == creature_type]


def get_active_creature(creature_type: str) -> str | None:
    """Get the name of the active wolt for a singleton creature type (wolf/dog)."""
    if creature_type not in SINGLETON_TYPES:
        return None
    try:
        config = json.loads(CONFIG_FILE.read_text())
        return config.get("creatures", {}).get(f"active_{creature_type}")
    except (json.JSONDecodeError, OSError):
        return None


def set_active_creature(creature_type: str, wolt_name: str) -> None:
    """Set the active wolt for a singleton creature type."""
    if creature_type not in SINGLETON_TYPES:
        return
    try:
        config = json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        config = {}
    config.setdefault("creatures", {})
    config["creatures"][f"active_{creature_type}"] = wolt_name
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")
