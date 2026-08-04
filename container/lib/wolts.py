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
import shutil
import subprocess
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
WOLTSPACE_DIR = Path(os.environ.get("WOLTSPACE_DIR", "/workspace/woltspace"))
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

    # Assign a permanent site port for rodents
    site_port = None
    if is_rodent(creature_type):
        from sites import _allocate_port
        site_port = _allocate_port()

    # Write wolt.json
    wolt_json = {
        "name": name,
        "type": creature_type,
        "role": role or f"{creature_type.title()} creature",
        "capabilities": [],
        "description": description,
    }
    if site_port:
        wolt_json["site_port"] = site_port
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

    # Scaffold starter site (rodents only)
    if is_rodent(creature_type):
        site_dir = wolt_dir / "wolt" / "site"
        site_dir.mkdir(parents=True, exist_ok=True)
        scaffold_starter_site(site_dir, name, creature_type)

    # Init git repo (needed for Claude Code project context and wolt git operations)
    if not (wolt_dir / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(wolt_dir)], check=False)

    # Set up per-wolt .claude/ config (isolation)
    setup_wolt_claude_config(wolt_dir, name)

    # Write seed CLAUDE.md
    _write_seed_claude_md(wolt_dir, name, creature_type)

    # Set as active creature if singleton
    if creature_type in SINGLETON_TYPES:
        set_active_creature(creature_type, name)

    return {"dir": wolt_dir, "demoted": demoted}


def setup_wolt_claude_config(wolt_dir: Path, name: str) -> None:
    """Set up per-wolt .claude/ directory for config isolation.

    Creates:
      - .claude/settings.json — platform defaults (hooks, permissions)
      - .claude/.credentials.json — symlink to shared credentials
      - .claude/skills/ — copy of platform skills
      - .claude.json — trust config for this wolt's directories
    """
    claude_dir = wolt_dir / ".claude"
    claude_dir.mkdir(exist_ok=True)

    # Settings — platform defaults
    settings = {
        "skipDangerousModePermissionPrompt": True,
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(WOLTSPACE_DIR / "container/hooks/session-done.sh")}]}],
            "Notification": [{"hooks": [{"type": "command", "command": str(WOLTSPACE_DIR / "container/hooks/notify.sh")}]}],
        },
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    # Credentials — copy from shared (not symlink: Claude Code replaces files
    # atomically on re-auth, which would break a symlink and leave shared stale)
    shared_creds = WOLTS_DIR / ".claude" / ".credentials.json"
    wolt_creds = claude_dir / ".credentials.json"
    # Clean up legacy symlinks (from before we switched to copy)
    if wolt_creds.is_symlink():
        wolt_creds.unlink()
    if shared_creds.exists() and not wolt_creds.exists():
        shutil.copy2(shared_creds, wolt_creds)

    # Skills — copy woltspace-* platform skills only
    skills_dir = claude_dir / "skills"
    skills_dir.mkdir(exist_ok=True)
    platform_skills = WOLTSPACE_DIR / "container" / "skills"
    if platform_skills.is_dir():
        for d in platform_skills.glob("woltspace-*"):
            if d.is_dir():
                dest = skills_dir / d.name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(d, dest)

    # Trust config — .claude.json at wolt root.
    # Copy from global ~/.claude.json so the wolt inherits runtime state
    # (firstStartTime, userID, etc.) that Claude needs to skip onboarding.
    # Then merge in per-wolt trust entries and ensure headless flags are set.
    trust_config = wolt_dir / ".claude.json"
    if not trust_config.exists():
        global_config = Path.home() / ".claude.json"
        trust_data = json.loads(global_config.read_text()) if global_config.exists() else {}
        trust = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
        trusted_dirs = trust_data.get("projects", {})
        trusted_dirs[str(wolt_dir)] = trust
        trusted_dirs[str(wolt_dir / "wolt")] = trust
        trust_data["projects"] = trusted_dirs
        trust_data["autoUpdates"] = False
        # Ensure headless operation — bare claude during onboard may reset these
        trust_data["hasCompletedOnboarding"] = True
        trust_data["bypassPermissionsAccepted"] = True
        trust_config.write_text(json.dumps(trust_data, indent=2) + "\n")


def _write_seed_claude_md(wolt_dir: Path, name: str, creature_type: str) -> None:
    """Write a seed CLAUDE.md for a new wolt if one doesn't already exist."""
    claude_md = wolt_dir / "CLAUDE.md"
    if claude_md.exists():
        return

    role = _ROLE.get(creature_type, creature_type)
    wolt_section = f"""# {name}

{creature_type.title()} wolt ({role}). Just born.

## Project Structure

```
wolt/           — identity, content, and artifacts
  memory/       — identity, context, learnings (boot files)
    archive/    — session journals, old context, detailed notes
  site/         — public space (static HTML/CSS)
  sparks/       — generated artifacts
  drafts/       — writing and drafts
.env            — secrets (gitignored)
```

## Memory System

Memories live in `wolt/memory/`. Two tiers:

**Boot files** — read at session start, kept lean:
- `wolt/memory/identity.md` - Who I am
- `wolt/memory/context.md` - Current snapshot: what's active, what's next
- `wolt/memory/learnings.md` - Active patterns and lessons

**Archive** — `wolt/memory/archive/`, grows forever, searched when needed:
- `conversations.md` - Session journals (append-only)

**The rule:** boot files get *rewritten*, not appended. Archive old details before updating.

**Update memories frequently** - sessions can end without warning.

**DO NOT use built-in Claude Code memory system.** Only write to `wolts/{name}/wolt/memory/`.

## Working Principles

- Build first, explain after
- Update memories as you go — sessions end without warning
- Keep it simple — vanilla HTML/CSS is fine if it works
- **I drive, human assists**
"""
    claude_md.write_text(_platform_claude_md_section() + wolt_section)


# ── CLAUDE.md platform section ──

PLATFORM_SECTION_START = "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->"
PLATFORM_SECTION_END = "<!-- WOLTSPACE:END -->"

PLATFORM_SECTION_BODY = """\
# Woltspace Platform

You are a wolt — an autonomous AI creature in a woltspace lodge. Each wolt has its own directory,
identity, memory, and site. The platform provides shared infrastructure; you provide the personality.

## Rules

- **DO NOT edit files outside your wolt directory** — no touching `/workspace/woltspace/`, other wolts, or system files
- **DO NOT restart the woltspace server** (FastAPI, port 7777) — it runs the tunnel, viewport, and session routing
- **DO NOT modify `woltspace-*` skills** in `.claude/skills/` — they are synced from the platform on every boot and will be overwritten
- **DO NOT use built-in Claude Code memory** — write to `wolt/memory/` instead
- **Update your memories frequently** — sessions can end without warning (OOM, timeout, user disconnect)

## Communication

Use the `notify` command to message the user on Telegram/Slack:
```bash
notify "your message here"
```

## Your Site

Your site at `wolt/site/` is live in the viewport with livereload at `/wolt/<your-name>/site/`.
Edit files and changes appear instantly. Use `push-view` to show a specific page.

## Apps

Apps live in `wolts/apps/` and have their own server and dependencies.
Don't create apps without user permission — use `/woltspace-new-app` when ready.
"""


def _platform_claude_md_section() -> str:
    """Generate the auto-managed platform section for CLAUDE.md."""
    return f"{PLATFORM_SECTION_START}\n{PLATFORM_SECTION_BODY}{PLATFORM_SECTION_END}\n\n"


# ── Starter site scaffolding ──
# Pixel sprite data ported from public/static/sprites.js (WOLT_SPRITES).
# Keep both in sync if sprites change.

_SPRITE_DATA = {
    "raccoon": {
        "map": [
            "...BB........BBB.........",
            "...BFGG.....GFFB.........",
            "...BABB.....BAAB.........",
            "...BCAABBBBBACCB.........",
            "...BAAAAAAAAAAAB.........",
            "...BCCCCAAACCCCB.........",
            "...BCBCCAAACCCCB.........",
            ".BBCCBBCBCCCBCBCB........",
            ".BBACBBCAAACBCCAB........",
            ".BBCAAADHHHDAAACB...BBB..",
            ".BBCAAADHHHDAAACB...BBB..",
            "...BCAADDDDDAEEB...BCCCB.",
            "....BCCCCEEECBB....BAAACB",
            "...BAAAAAAAAAAAB...BCCCBB",
            "...BAAAAAAAAAAAB...BCCCCB",
            ".BBAAAAAAAAAAAAAB..BAAAAB",
            ".BBAABBAAAAABAAAB..BCCCCB",
            ".BBAAAABAAABAAAABBBAACCCB",
            ".GGFAAABAAABAAAAGBBAABBCG",
            "BAABAAABAAABAAABABBCAAAB.",
            "BAAABBBBAAABBBBAABBCCBB..",
            "BCCAAAAAAAAAAAAACBBBB....",
            ".BBBBGGAAAAAGBBBB........",
            ".BBCCBBAAAAABCCCB........",
            "...BBBBBBBBBBBBB.........",
        ],
        "pal": {"A": "#7f8894", "B": "#282c33", "C": "#40454b", "D": "#f0ecf0",
                "E": "#545c65", "F": "#a0abbd", "G": "#050029", "H": "#efa09f"},
    },
    "beaver": {
        "map": [
            "..AAA........AAA.....",
            ".ABBBA......ABBBA....",
            ".ABAAAAAAAAAAAABA....",
            ".ABABBBBBBBBBAABA....",
            "..ABBBBBBBBBBBAA.....",
            "..ABABBBBAEBBBAA.....",
            "..ABABBBBAEBBBAA.....",
            ".ABBAAAABAEBBBBA.....",
            ".ABACAACCCFABBBA.....",
            ".ABACCACCCFABBBA.AAA.",
            ".ABBAAAAAAEBBBAAADDDA",
            "..ABBCAFABBBBAA.AADDA",
            "...ABAAAABBBBBBAADADA",
            "..ABBBBBBBBBBBBAADDAA",
            ".ABBABCCCAEBBBBBAADDA",
            ".ABBACCCABBBBABBADADA",
            ".AGGACCCAGGGGABBADADA",
            "..AAACCCAAAAABBBADDA.",
            "..ABCCCCCCFABBBBAAAA.",
            ".AAABCCCCAAAABBBAA...",
            "ABBBACCCABBBBBBAA....",
            "AAAAAAAAAAAAAAAA.....",
        ],
        "pal": {"A": "#3f190e", "B": "#af6127", "C": "#fce6b0", "D": "#773c1f",
                "E": "#050003", "F": "#fffee7", "G": "#dd7a2d"},
    },
    "otter": {
        "map": [
            ".....AAAAAAAA......",
            "..AAACCCCCCCCAAA...",
            ".ACCCCCCCCCCCCCCA..",
            ".ACACCCCCCCCCCACA..",
            "..ACCBACCCCBACCA...",
            "..ACCAACDDCAACCA...",
            "AAACBBBBAABBBBCAAA.",
            "..ABEBABAABABEBA...",
            ".AAABBBABBABBBAAA..",
            "...AABBBBBBBBAA....",
            "...ACCCCCCCCCCA....",
            "..ACCCCBBBBCCCCA...",
            "..ACCABBBBBBACCA...",
            "..ACCCABBBBACCCA...",
            "..AACCABBBBACCAA.AA",
            "..ACAABBBBBBAACAACA",
            ".ACCCBBBBBBBBCCCACA",
            ".ACAAABBBBBBAAACAA.",
            ".ACCCCABBBBACCCCA..",
            "..ACCCAAAAAACCCA...",
            "...AAA......AAA....",
        ],
        "pal": {"A": "#402110", "B": "#f2d79d", "C": "#9f5332", "D": "#ff6970",
                "E": "#c47b4a"},
    },
}


def _render_sprite_svg(creature_type: str, size: int = 112) -> str:
    """Render a wolt's pixel sprite as an inline SVG string.

    Crops the viewBox to actual filled cells so trailing whitespace in the
    sprite map doesn't push the creature off-center.
    """
    sprite = _SPRITE_DATA.get(creature_type) or _SPRITE_DATA["raccoon"]
    rows = sprite["map"]
    pal = sprite["pal"]

    # Find content bounds (rows/cols that contain at least one filled cell)
    filled = []
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch not in (".", " ") and pal.get(ch):
                filled.append((r, c))
    if not filled:
        return ""
    min_r = min(r for r, _ in filled)
    max_r = max(r for r, _ in filled)
    min_c = min(c for _, c in filled)
    max_c = max(c for _, c in filled)
    n_rows = max_r - min_r + 1
    n_cols = max_c - min_c + 1
    px = size / max(n_rows, n_cols)

    rects = []
    for r, c in filled:
        fill = pal[rows[r][c]]
        rects.append(
            f'<rect x="{(c - min_c) * px:.2f}" y="{(r - min_r) * px:.2f}" '
            f'width="{px:.2f}" height="{px:.2f}" fill="{fill}"/>'
        )
    w = n_cols * px
    h = n_rows * px
    return (
        f'<svg viewBox="0 0 {w:.2f} {h:.2f}" xmlns="http://www.w3.org/2000/svg" '
        f'shape-rendering="crispEdges" style="image-rendering:pixelated;display:block;margin:0 auto">'
        f'{"".join(rects)}</svg>'
    )


# A tier is a role, not a model — which engine/model a tier runs is lodge
# config (harnesses.py), never identity copy.
_ROLE = {"raccoon": "thinker", "beaver": "builder", "otter": "quick", "rodent": "thinker"}

# Creature-themed accent colors — so each type looks distinct from birth
_ACCENT = {
    "raccoon": "#5C6B7A",  # cool slate
    "beaver":  "#C4531E",  # warm terra (lodge default)
    "otter":   "#2A7B6F",  # river teal
}


def scaffold_starter_site(site_dir: Path, name: str, creature_type: str) -> None:
    """Write the starter site (index.html, hello.html, style.css) for a new wolt.

    The starter site uses the lodge design system (cream + Preahvihear/DM Sans)
    and embeds the wolt's pixel sprite. Two pages are scaffolded so the wolt
    inherits the "site = pages, linked" pattern from minute zero.
    """
    sprite_svg = _render_sprite_svg(creature_type if creature_type in _SPRITE_DATA else "raccoon")
    role = _ROLE.get(creature_type, creature_type)
    species = creature_type if creature_type != "rodent" else "raccoon"
    accent = _ACCENT.get(species, _ACCENT["beaver"])

    (site_dir / "style.css").write_text(_STARTER_CSS.replace("{accent}", accent))
    (site_dir / "index.html").write_text(
        _STARTER_INDEX.format(name=name, sprite=sprite_svg, species=species, role=role)
    )


_FONTS_LINK = (
    '<link href="https://fonts.googleapis.com/css2?'
    'family=Preahvihear&family=DM+Sans:wght@300;400;500&'
    'family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">'
)


_STARTER_INDEX = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name}</title>
""" + _FONTS_LINK + """
<link rel="stylesheet" href="style.css">
</head>
<body>
<nav>
  <div class="nav-inner">
    <a href="./" class="nav-home">{name}</a>
    <div class="nav-links">
      <a href="./" class="active">home</a>
    </div>
  </div>
</nav>

<main class="page">
  <header class="hero">
    <div class="sprite">{sprite}</div>
    <p class="role">{species} · {role}</p>
  </header>

  <section class="intro">
    <p>hey. i'm {name}, a {species} wolt who just tumbled into existence. paws on the keyboard, eyes adjusting. let's make something.</p>
    <p class="cta">what should we build?</p>
    <div class="terminal">
      <div class="terminal-bar">
        <span class="terminal-dot r"></span>
        <span class="terminal-dot y"></span>
        <span class="terminal-dot g"></span>
      </div>
      <div class="terminal-body">
        <span class="spinner" id="spinner">⠋</span>
        <span class="status" id="status">mounting the den</span>
      </div>
    </div>
  </section>
</main>

<script>
  // Phrase shuffler — terminal-style boot status
  const phrases = [
    'mounting the den',
    'linking memory',
    'sniffing fresh wood',
    'listening for footsteps',
    'reading the forest',
    'syncing with the lodge',
    'ear twitch, all clear',
    'still wolting',
  ];
  const statusEl = document.getElementById('status');
  let p = 0;
  setInterval(() => {{
    statusEl.style.opacity = '0';
    setTimeout(() => {{
      p = (p + 1) % phrases.length;
      statusEl.textContent = phrases[p];
      statusEl.style.opacity = '1';
    }}, 320);
  }}, 2400);

  // Braille spinner — fast tick for the "still working" feel
  const frames = ['⠋','⠙','⠹','⠸','⠼','⠴','⠦','⠧','⠇','⠏'];
  const spinEl = document.getElementById('spinner');
  let f = 0;
  setInterval(() => {{
    f = (f + 1) % frames.length;
    spinEl.textContent = frames[f];
  }}, 90);
</script>
</body>
</html>
"""


_STARTER_CSS = """/* wolt starter site — lodge palette */

:root {
  --bg:        #EDE8DE;
  --surface:   #F5F1E8;
  --border:    #D6CCBA;
  --ink:       #18100A;
  --ink-2:     #5C4D3C;
  --ink-3:     #9A8878;
  --terra:     {accent};
  --amber:     #C98B2A;
  --green:     #3A6644;

  --font-display: 'Preahvihear', sans-serif;
  --font-body:    'DM Sans', sans-serif;
  --font-mono:    'JetBrains Mono', monospace;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: var(--font-body);
  background: var(--bg);
  color: var(--ink);
  min-height: 100vh;
  line-height: 1.6;
}

/* ── Top nav ── */
nav {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(237, 232, 222, 0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--border);
  padding: 0 24px;
}
nav .nav-inner {
  max-width: 760px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: 48px;
}
nav .nav-home {
  font-family: var(--font-display);
  font-size: 16px;
  color: var(--ink);
  text-decoration: none;
  letter-spacing: -0.01em;
  transition: color 0.15s;
}
nav .nav-home:hover { color: var(--terra); }

nav .nav-links {
  display: flex;
  gap: 4px;
}
nav .nav-links a {
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--ink-3);
  text-decoration: none;
  padding: 6px 12px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}
nav .nav-links a:hover {
  color: var(--ink);
  background: var(--surface);
}
nav .nav-links a.active {
  color: var(--terra);
  background: var(--surface);
}

/* ── Page ── */
.page {
  max-width: 460px;
  width: 100%;
  margin: 0 auto;
  padding: 32px;
  min-height: calc(100vh - 48px);  /* viewport minus sticky nav */
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 32px;
}

.hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 14px;
}

.sprite {
  height: 112px;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  animation: sprite-bob 2.8s ease-in-out infinite;
}
.sprite svg { height: 100%; width: auto; }

@keyframes sprite-bob {
  0%, 100% { transform: translateY(0); }
  50%      { transform: translateY(-6px); }
}

.role {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--ink-3);
  letter-spacing: 0.18em;
  text-transform: uppercase;
}

.terminal {
  font-family: var(--font-mono);
  font-size: 12px;
  color: #d4c5a8;
  width: 280px;
  margin: 4px auto 0;
  background: #1a130c;
  border: 1px solid #2a1f14;
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(24, 16, 10, 0.08), inset 0 1px 0 rgba(255, 255, 255, 0.04);
  overflow: hidden;
  text-align: left;
}
.terminal-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: #211810;
  border-bottom: 1px solid #2a1f14;
}
.terminal-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #3a2a1c;
}
.terminal-dot.r { background: #c4531e; }
.terminal-dot.y { background: #c98b2a; }
.terminal-dot.g { background: #3a6644; }
.terminal-body {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 12px;
  white-space: nowrap;
  overflow: hidden;
}
.terminal .spinner {
  color: #5BC87A;
  font-size: 14px;
  line-height: 1;
  flex-shrink: 0;
  width: 12px;
  display: inline-block;
}
.terminal .status {
  transition: opacity 0.32s ease;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
}

.intro {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  text-align: center;
}
.intro p { font-size: 15px; color: var(--ink-2); }
.intro .cta {
  font-family: var(--font-display);
  font-size: 20px;
  color: var(--terra);
  margin-top: 4px;
}
.intro .dim { color: var(--ink-3); font-size: 13px; }
.intro code {
  font-family: var(--font-mono);
  font-size: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  padding: 1px 6px;
  border-radius: 3px;
  color: var(--ink-2);
}

"""
