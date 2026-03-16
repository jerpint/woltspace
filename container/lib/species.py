"""
Species — load species definitions and resolve inheritance.

Usage:
    from species import list_species, get_species, get_valid_types, get_singleton_types
    from species import get_creature_model, get_creature_emoji, get_species_instructions

    all_species = list_species()
    # {"rodent": {...}, "wolf": {...}, ...}

    wolf = get_species("wolf")
    # {"name": "wolf", "runtime": "daemon", "singleton": true, ...}

    model = get_creature_model("raccoon")
    # "anthropic/claude-opus-4-1"  (resolves rodent tier)
"""

import json
import os
from pathlib import Path

WOLTSPACE_DIR = Path(os.environ.get("WOLTSPACE_DIR", "/workspace/woltspace"))
SPECIES_DIR = WOLTSPACE_DIR / "species"

# Cache — loaded once per process
_species_cache: dict[str, dict] | None = None


def _load_species() -> dict[str, dict]:
    """Discover all species by scanning species.json files."""
    species = {}
    if not SPECIES_DIR.is_dir():
        return species
    for species_json in sorted(SPECIES_DIR.glob("*/species.json")):
        try:
            data = json.loads(species_json.read_text())
            name = data.get("name", species_json.parent.name)
            data["_dir"] = str(species_json.parent)
            species[name] = data
        except (json.JSONDecodeError, OSError):
            continue
    return species


def list_species() -> dict[str, dict]:
    """Return all species definitions, keyed by name. Cached after first call."""
    global _species_cache
    if _species_cache is None:
        _species_cache = _load_species()
    return _species_cache


def get_species(name: str) -> dict | None:
    """Get a single species definition by name."""
    return list_species().get(name)


def get_valid_types() -> set[str]:
    """Return all valid species type names. Replaces hardcoded VALID_TYPES."""
    species = list_species()
    if species:
        return set(species.keys())
    # Fallback if no species/ dir exists yet
    return {"rodent", "wolf", "dog", "spider", "bear", "panda"}


def get_singleton_types() -> set[str]:
    """Return species that are singletons. Replaces hardcoded SINGLETON_TYPES."""
    species = list_species()
    if species:
        return {name for name, data in species.items() if data.get("singleton", False)}
    # Fallback
    return {"wolf", "dog"}


def get_creature_model(creature: str) -> str | None:
    """Resolve the model for a creature name.

    Handles both direct species (wolf → species model) and rodent tiers
    (raccoon → rodent.tiers.raccoon.model).
    """
    # Check if it's a direct species with a model
    sp = get_species(creature)
    if sp and sp.get("model"):
        return sp["model"]

    # Check if it's a rodent tier
    rodent = get_species("rodent")
    if rodent and rodent.get("tiers"):
        tier = rodent["tiers"].get(creature)
        if tier:
            return tier.get("model")

    return None


def get_creature_emoji(creature: str) -> str:
    """Get the emoji for a creature (species or rodent tier)."""
    sp = get_species(creature)
    if sp:
        return sp.get("emoji", "")

    # Check rodent tiers
    rodent = get_species("rodent")
    if rodent and rodent.get("tiers"):
        tier = rodent["tiers"].get(creature)
        if tier:
            return tier.get("emoji", "")

    return ""


def get_species_instructions(species_name: str) -> str | None:
    """Load the instructions.md for a species."""
    sp = get_species(species_name)
    if not sp:
        return None
    instructions_path = Path(sp["_dir"]) / "instructions.md"
    if instructions_path.exists():
        return instructions_path.read_text().strip()
    return None


def get_species_skills_dir(species_name: str) -> Path | None:
    """Get the skills directory for a species, if it exists."""
    sp = get_species(species_name)
    if not sp:
        return None
    skills_dir = Path(sp["_dir"]) / "skills"
    if skills_dir.is_dir():
        return skills_dir
    return None


def resolve_species_for_creature(creature: str) -> str:
    """Given a creature name (beaver, wolf, raccoon...), return its species name.

    Rodent tiers (otter, beaver, raccoon) resolve to 'rodent'.
    Direct species (wolf, dog) resolve to themselves.
    """
    if get_species(creature):
        return creature

    # Check if it's a rodent tier
    rodent = get_species("rodent")
    if rodent and rodent.get("tiers", {}).get(creature):
        return "rodent"

    return "rodent"  # default fallback
