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
RODENT_TYPES = {"otter", "beaver", "raccoon", "rodent"}  # "rodent" = legacy, treated as raccoon
VALID_TYPES = RODENT_TYPES | {"wolf", "dog", "spider", "bear", "panda"}

# Rodent emojis and lore for default site template
CREATURE_META = {
    "raccoon": {"emoji": "🦝", "lore": "the den is warm. something stirs."},
    "beaver":  {"emoji": "🦫", "lore": "the dam is quiet. wood creaks."},
    "otter":   {"emoji": "🦦", "lore": "the river hums. a splash."},
    "rodent":  {"emoji": "🦫", "lore": "the burrow is dark. eyes open."},
}

# Types that can only have one active at a time
SINGLETON_TYPES = {"wolf", "dog"}


def is_rodent(creature_type: str) -> bool:
    """Check if a creature type is a rodent (chatty, runs Claude Code sessions)."""
    return creature_type in RODENT_TYPES


def _get_wolt_type(wolt_name: str) -> str:
    """Read a wolt's creature type from its wolt.json. Defaults to 'rodent'."""
    wolt_json = WOLTS_DIR / wolt_name / "wolt" / "wolt.json"
    try:
        data = json.loads(wolt_json.read_text())
        return data.get("type", "rodent")
    except (json.JSONDecodeError, OSError):
        return "rodent"


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

    Returns a dict with:
        - "dir": Path to the new wolt directory
        - "demoted": name of the old wolt that was demoted to rodent, or None
    Raises ValueError if the type is invalid or the name already exists.
    """
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

    # Create site with wakeup template (rodents only)
    if is_rodent(creature_type):
        site_dir = wolt_dir / "wolt" / "site"
        site_dir.mkdir(parents=True, exist_ok=True)
        (site_dir / "index.html").write_text(
            _wakeup_template(name, creature_type)
        )

    # Set as active creature if singleton
    if creature_type in SINGLETON_TYPES:
        set_active_creature(creature_type, name)

    return {"dir": wolt_dir, "demoted": demoted}


def _wakeup_template(name: str, creature_type: str) -> str:
    """Generate the default wakeup site page for a new wolt."""
    meta = CREATURE_META.get(creature_type, CREATURE_META["rodent"])
    emoji = meta["emoji"]
    lore = meta["lore"]
    # Map creature type to model tier label
    tier = {"raccoon": "opus", "beaver": "sonnet", "otter": "haiku"}.get(
        creature_type, creature_type
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'SF Mono', 'Fira Code', 'Cascadia Code', monospace;
    background: #2a1f14; color: #f0dfc0;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
    overflow: hidden;
  }}
  body::after {{
    content: '';
    position: fixed; inset: 0;
    background: repeating-linear-gradient(
      0deg, transparent, transparent 2px,
      rgba(0,0,0,0.03) 2px, rgba(0,0,0,0.03) 4px
    );
    pointer-events: none; z-index: 10;
  }}
  .den {{
    display: flex; flex-direction: column;
    align-items: center; gap: 1.8rem;
    text-align: center;
  }}
  .sigil {{
    font-size: 3.2rem; line-height: 1;
    user-select: none;
    filter: drop-shadow(0 0 12px rgba(74, 124, 89, 0.5));
    animation: breathe 2.4s ease-in-out infinite;
  }}
  @keyframes breathe {{
    0%, 100% {{
      transform: scale(1);
      filter: drop-shadow(0 0 8px rgba(74, 124, 89, 0.3));
    }}
    50% {{
      transform: scale(1.08);
      filter: drop-shadow(0 0 24px rgba(74, 124, 89, 0.7));
    }}
  }}
  .name {{ font-size: 1.2rem; color: #f0dfc0; letter-spacing: 0.04em; }}
  .species {{
    font-size: 0.68rem; color: #a08060;
    letter-spacing: 0.12em; text-transform: uppercase;
    margin-top: -1rem;
  }}
  .wake {{
    font-size: 0.82rem; color: #7bbf8a;
    letter-spacing: 0.08em;
    display: flex; align-items: center; gap: 0;
  }}
  .wake-text {{ opacity: 0; animation: fadeIn 0.6s ease forwards 0.3s; }}
  @keyframes fadeIn {{ to {{ opacity: 1; }} }}
  .dots span {{
    opacity: 0;
    animation: dotPulse 1.4s ease-in-out infinite;
  }}
  .dots span:nth-child(1) {{ animation-delay: 0s; }}
  .dots span:nth-child(2) {{ animation-delay: 0.2s; }}
  .dots span:nth-child(3) {{ animation-delay: 0.4s; }}
  @keyframes dotPulse {{
    0%, 60%, 100% {{ opacity: 0; }}
    30% {{ opacity: 1; }}
  }}
  .progress {{
    width: 200px; height: 2px;
    background: #3d2b1a; border-radius: 2px;
    overflow: hidden;
  }}
  .progress-fill {{
    height: 100%; width: 60%; border-radius: 2px;
    background: linear-gradient(90deg, #3d2b1a, #4a7c59, #3d2b1a);
    background-size: 200% 100%;
    animation: shimmer 1.8s ease-in-out infinite;
  }}
  @keyframes shimmer {{
    0% {{ background-position: 200% 0; }}
    100% {{ background-position: -200% 0; }}
  }}
  .lore {{
    font-size: 0.64rem; color: #8a7060;
    font-style: italic; margin-top: 0.5rem;
  }}
  .status {{
    font-size: 0.64rem; color: #a08060;
    height: 1.2em; overflow: hidden;
  }}
  .status span {{
    display: block;
    animation: fadeInOut 2s ease forwards;
  }}
  @keyframes fadeInOut {{
    0% {{ opacity: 0; transform: translateY(4px); }}
    15% {{ opacity: 1; transform: translateY(0); }}
    85% {{ opacity: 1; transform: translateY(0); }}
    100% {{ opacity: 0; transform: translateY(-4px); }}
  }}
</style>
</head>
<body>
<div class="den">
  <div class="sigil">{emoji}</div>
  <div class="name">{name}</div>
  <div class="species">{creature_type} · {tier}</div>
  <div class="wake">
    <span class="wake-text">your wolt is waking up</span>
    <span class="dots"><span>.</span><span>.</span><span>.</span></span>
  </div>
  <div class="progress"><div class="progress-fill"></div></div>
  <div class="status" id="status"></div>
  <div class="lore">{lore}</div>
</div>
<script>
  const phrases = [
    'sniffing around',
    'creating identity',
    'wolting',
    'finding its footing',
    'reading the forest',
    'stretching',
    'almost there',
  ];
  const statusEl = document.getElementById('status');
  let i = 0;
  function next() {{
    statusEl.innerHTML = '';
    const span = document.createElement('span');
    span.textContent = '\\u25b8 ' + phrases[i];
    statusEl.appendChild(span);
    i = (i + 1) % phrases.length;
  }}
  next();
  setInterval(next, 2000);
</script>
</body>
</html>
"""
