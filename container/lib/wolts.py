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
import re
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
CONFIG_FILE = WOLTS_DIR / "woltspace.json"

# Valid creature types
RODENT_TYPES = {"otter", "beaver", "raccoon", "rodent"}  # "rodent" = legacy, treated as raccoon
VALID_TYPES = RODENT_TYPES | {"wolf", "dog", "spider", "bear", "panda"}

# Types that can only have one active at a time
SINGLETON_TYPES = {"wolf", "dog"}

# Name validation — lowercase letters, numbers, hyphens. Must start with a letter.
WOLT_NAME_PATTERN = re.compile(r'^[a-z][a-z0-9-]*$')
WOLT_NAME_MAX_LENGTH = 20


def slugify_wolt_name(name: str) -> str:
    """Sanitize a wolt name into a valid slug.

    'Wolter White' → 'wolter-white', '  My Wolt! ' → 'my-wolt', '123bad' → 'bad'
    Returns empty string if nothing salvageable.
    """
    s = name.strip().lower()
    s = re.sub(r'[^a-z0-9]+', '-', s)  # replace non-alphanumeric runs with single hyphen
    s = s.strip('-')                     # trim leading/trailing hyphens
    s = re.sub(r'^[0-9-]+', '', s)       # strip leading numbers/hyphens
    return s[:WOLT_NAME_MAX_LENGTH]


def is_rodent(creature_type: str) -> bool:
    """Check if a creature type is a rodent (chatty, runs Claude Code sessions)."""
    return creature_type in RODENT_TYPES


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


def create_creature_wolt(name: str, creature_type: str, role: str = "", description: str = "") -> dict:
    """Create a minimal creature-wolt directory.

    The name is auto-slugified: 'Wolter White' → 'wolter-white'.

    Returns a dict with:
        - "dir": Path to the new wolt directory
        - "name": the sanitized name (may differ from input)
        - "demoted": name of the old wolt that was demoted to rodent, or None
    Raises ValueError if the name is unsalvageable, type is invalid, or the name already exists.
    """
    name = slugify_wolt_name(name)
    if not name:
        raise ValueError(f"Invalid wolt name: '{name}'. Must contain at least one letter.")

    if creature_type not in VALID_TYPES:
        raise ValueError(f"Invalid creature type: {creature_type}. Must be one of: {', '.join(sorted(VALID_TYPES))}")

    wolt_dir = WOLTS_DIR / name
    if wolt_dir.exists():
        raise ValueError(f"Wolt '{name}' already exists at {wolt_dir}")

    # Check singleton constraint
    demoted = None
    if creature_type in SINGLETON_TYPES:
        active = get_active_creature(creature_type)
        if active:
            # Demote the old one to rodent
            old_wolt_json = WOLTS_DIR / active / "wolt" / "wolt.json"
            if old_wolt_json.exists():
                try:
                    old_data = json.loads(old_wolt_json.read_text())
                    old_data["type"] = "rodent"
                    old_wolt_json.write_text(json.dumps(old_data, indent=2) + "\n")
                    demoted = active
                except (json.JSONDecodeError, OSError):
                    pass

    # Create directory structure
    (wolt_dir / "wolt" / "memory" / "archive").mkdir(parents=True)
    (wolt_dir / ".state").mkdir(parents=True)

    # Write wolt.json
    wolt_json = {
        "name": name,
        "type": creature_type,
        "role": role or f"{creature_type.title()} creature",
        "capabilities": [],
        "description": description,
    }
    (wolt_dir / "wolt" / "wolt.json").write_text(json.dumps(wolt_json, indent=2) + "\n")

    # Write minimal identity.md
    (wolt_dir / "wolt" / "memory" / "identity.md").write_text(
        f"# {name}\n\nI am {name}, a {creature_type}.\n"
    )

    # Write empty context and learnings
    (wolt_dir / "wolt" / "memory" / "context.md").write_text(
        f"# Context\n\n## Session 1\n\nJust created.\n"
    )
    (wolt_dir / "wolt" / "memory" / "learnings.md").write_text(
        "# Learnings\n\n*Day one.*\n"
    )

    # Set as active creature if singleton
    if creature_type in SINGLETON_TYPES:
        set_active_creature(creature_type, name)

    return {"dir": wolt_dir, "name": name, "demoted": demoted}
