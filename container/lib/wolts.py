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
from species import get_valid_types, get_singleton_types

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
CONFIG_FILE = WOLTS_DIR / "woltspace.json"


def _valid_types() -> set[str]:
    """Valid creature types — read from species/ definitions."""
    return get_valid_types()


def _singleton_types() -> set[str]:
    """Singleton types — read from species/ definitions."""
    return get_singleton_types()


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
    if creature_type not in _singleton_types():
        return None
    try:
        config = json.loads(CONFIG_FILE.read_text())
        return config.get("creatures", {}).get(f"active_{creature_type}")
    except (json.JSONDecodeError, OSError):
        return None


def set_active_creature(creature_type: str, wolt_name: str) -> None:
    """Set the active wolt for a singleton creature type."""
    if creature_type not in _singleton_types():
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

    Returns a dict with:
        - "dir": Path to the new wolt directory
        - "demoted": name of the old wolt that was demoted to rodent, or None
    Raises ValueError if the type is invalid or the name already exists.
    """
    valid = _valid_types()
    if creature_type not in valid:
        raise ValueError(f"Invalid creature type: {creature_type}. Must be one of: {', '.join(sorted(valid))}")

    wolt_dir = WOLTS_DIR / name
    if wolt_dir.exists():
        raise ValueError(f"Wolt '{name}' already exists at {wolt_dir}")

    # Check singleton constraint
    demoted = None
    if creature_type in _singleton_types():
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
    if creature_type in _singleton_types():
        set_active_creature(creature_type, name)

    return {"dir": wolt_dir, "demoted": demoted}
