"""
Creature types — the built-in creatures and the lodge's own custom ones.

A custom creature is a new identity on one of the three rodent sizes. It has
its own name and emoji, and optionally its own model per harness and a
default role, but it behaves exactly like its size: the size decides the
site, the tier, the sprite, the colours and the lore. Wolf, dog and the other
special creatures are not affected.

The owner's custom creatures live in the lodge's woltspace.json, next to the
singleton bookkeeping that already sits in "creatures":

    "creatures": {
      "active_wolf": "howlie",
      "custom": {
        "star": {"emoji": "⭐", "size": "raccoon",
                 "models": {"codex": "gpt-6-astra"}, "role": "..."}
      }
    }

No "custom" entry means the lodge has only the built-ins, exactly as before.

Two questions have two different answers, and keeping them apart is the
whole design:

- what a creature is CALLED and SHOWN as: its own type and emoji
  (``creature_emoji``), for every surface a person reads;
- how it BEHAVES: its size (``base_type``), for every decision about sites,
  tiers and lore.

Which model a session gets: the wolt's own pin, then the custom creature's
model for that harness, then the size's default (see harnesses.creature_model).

Usage:
    from creature_types import base_type, creature_emoji, custom_creatures
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from env_compat import get_env
from settings_files import settings_lock

# The sizes a custom creature can take.
SIZES = ("otter", "beaver", "raccoon")

# Every built-in creature's emoji, in one place. "rodent" is the legacy type,
# treated as a raccoon.
BUILTIN_EMOJI = {
    "raccoon": "🦝",
    "beaver": "🦫",
    "otter": "🦦",
    "rodent": "🦝",
    "wolf": "🐺",
    "dog": "🐶",
    "spider": "🕷️",
    "bear": "🐻",
    "panda": "🐼",
}

# Names a custom creature can never take: every built-in, every type a
# surface keeps its own emoji for (the lodge pages still know "eagle"), the
# creature modules, and the keys the "creatures" section uses for itself.
RESERVED = set(BUILTIN_EMOJI) | {"eagle", "vulture", "custom", "creature", "wolt"}

CREATURE_ID_MAX = 20
ROLE_MAX = 200
EMOJI_MAX = 16  # code points: a flag or a skin-toned emoji is several
_ID = re.compile(r"^[a-z][a-z0-9-]*$")
_FIELDS = {"emoji", "size", "models", "role"}

def _config_path() -> Path:
    return Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts")) / "woltspace.json"


def custom_creatures() -> dict[str, dict]:
    """The lodge's custom creatures, keyed by id. Entries that don't validate
    are left out, so a hand-edited mistake never reaches a session."""
    try:
        cfg = json.loads(_config_path().read_text())
        raw = cfg.get("creatures", {}).get("custom", {})
    except (json.JSONDecodeError, OSError, AttributeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for cid, entry in raw.items():
        try:
            out[cid] = _clean(cid, entry, check_models=False)
        except ValueError:
            continue
    return out


def get_custom(creature_type: str | None) -> dict | None:
    if not creature_type or creature_type in RESERVED:
        return None
    return custom_creatures().get(creature_type)


def is_custom(creature_type: str | None) -> bool:
    return get_custom(creature_type) is not None


def base_type(creature_type: str | None) -> str | None:
    """The creature whose behaviour this type has: a custom creature's size,
    any other type itself."""
    custom = get_custom(creature_type)
    return custom["size"] if custom else creature_type


def creature_emoji(creature_type: str | None, default: str = "🐾") -> str:
    """How a creature is shown, wherever a person reads it."""
    if creature_type in BUILTIN_EMOJI:
        return BUILTIN_EMOJI[creature_type]
    custom = get_custom(creature_type)
    return custom["emoji"] if custom else default


def custom_emoji(creature_type: str | None) -> str | None:
    """A custom creature's emoji, or None for any other type. The surfaces
    that keep their own built-in table ask this for everything else, so a
    built-in creature looks exactly as it did there."""
    custom = get_custom(creature_type)
    return custom["emoji"] if custom else None


def emoji_from(table: dict, creature_type: str | None, default: str = "") -> str:
    """Look a creature up in a surface's own emoji table, then among the
    custom creatures. Built-ins keep whatever that table always said."""
    if creature_type in table:
        return table[creature_type]
    return custom_emoji(creature_type) or default


def wolt_emoji(wolt_name: str, wolts_dir: Path | None = None, default: str = "🐾") -> str:
    """A wolt's emoji, from its own wolt.json type."""
    root = wolts_dir or Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
    try:
        cfg = json.loads((root / wolt_name / "wolt" / "wolt.json").read_text())
        return creature_emoji(cfg.get("type") or "rodent", default)
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        return default


def custom_model(harness: str | None, creature_type: str | None) -> str | None:
    """The custom creature's own model for this harness, if it names one."""
    custom = get_custom(creature_type)
    if not custom or not harness:
        return None
    return custom.get("models", {}).get(harness) or None


def custom_creature_models(creature_type: str | None) -> dict:
    """A custom creature's own model per harness ({} for any other type)."""
    custom = get_custom(creature_type)
    return dict(custom.get("models", {})) if custom else {}


# --- validation and writes (control plane only) ---------------------------

def _clean(cid: object, entry: object, *, check_models: bool = True) -> dict:
    if not isinstance(cid, str) or not _ID.match(cid) or len(cid) > CREATURE_ID_MAX:
        raise ValueError(
            f"a creature name is lowercase letters, digits and hyphens, starting with a letter, "
            f"at most {CREATURE_ID_MAX} characters"
        )
    if cid in RESERVED:
        raise ValueError(f"{cid} is a built-in creature name; pick another")
    if not isinstance(entry, dict):
        raise ValueError("a creature is an object")
    unknown = set(entry) - _FIELDS
    if unknown:
        raise ValueError(f"unknown creature field: {', '.join(sorted(unknown))}")
    emoji = entry.get("emoji")
    if (not isinstance(emoji, str) or not emoji or len(emoji) > EMOJI_MAX
            or any(ch.isspace() or ch.isascii() for ch in emoji)):
        raise ValueError("emoji must be one emoji (no letters or spaces)")
    size = entry.get("size")
    if size not in SIZES:
        raise ValueError(f"size must be one of: {', '.join(SIZES)}")
    out = {"emoji": emoji, "size": size}
    models = entry.get("models")
    if models not in (None, {}):
        if not isinstance(models, dict):
            raise ValueError("models maps a harness to a model id")
        if check_models:
            from harnesses import HARNESSES, is_valid_model
            for harness, model in models.items():
                if harness not in HARNESSES:
                    raise ValueError(f"unknown harness: {harness}")
                if not isinstance(model, str) or not is_valid_model(harness, model):
                    raise ValueError(f"unknown model for {harness}: {model}")
        out["models"] = {h: m for h, m in models.items() if isinstance(h, str) and isinstance(m, str) and m}
    role = entry.get("role")
    if role not in (None, ""):
        if not isinstance(role, str) or len(role) > ROLE_MAX or "\n" in role:
            raise ValueError(f"role must be one line, at most {ROLE_MAX} characters")
        out["role"] = role.strip()
    return out


def _custom_section(cfg: dict) -> dict:
    from harnesses import _settings_section

    return _settings_section(_settings_section(cfg, "creatures"), "custom")


def add_custom(cid: str, entry: dict) -> dict:
    """Register a custom creature. Refuses to overwrite one that exists."""
    from harnesses import _load_lodge_settings_for_write, _write_lodge_settings

    clean = _clean(cid, entry)
    with settings_lock(_config_path()):
        path, cfg = _load_lodge_settings_for_write()
        section = _custom_section(cfg)
        if cid in section:
            raise ValueError(f"a creature named {cid} already exists; remove it first")
        section[cid] = clean
        _write_lodge_settings(path, cfg)
    return clean


def wolts_using(cid: str, wolts_dir: Path | None = None) -> list[str]:
    root = wolts_dir or Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
    names = []
    for wolt_json in sorted(root.glob("*/wolt/wolt.json")):
        try:
            if json.loads(wolt_json.read_text()).get("type") == cid:
                names.append(wolt_json.parent.parent.name)
        except (json.JSONDecodeError, OSError, AttributeError):
            continue
    return names


def remove_custom(cid: str) -> None:
    """Remove a custom creature. Refuses while a wolt still is one.

    The in-use check runs under the lodge settings lock, which creating a wolt
    and assigning a creature also hold: neither can give a wolt this creature
    between the check and the removal.
    """
    from harnesses import _load_lodge_settings_for_write, _write_lodge_settings

    with settings_lock(_config_path()):
        users = wolts_using(cid)
        if users:
            raise ValueError(f"{cid} is still the creature of: {', '.join(users)}; assign them another first")
        path, cfg = _load_lodge_settings_for_write()
        section = _custom_section(cfg)
        if cid not in section:
            raise ValueError(f"no custom creature named {cid}")
        del section[cid]
        _write_lodge_settings(path, cfg)
