#!/usr/bin/env python3
"""Entrypoint setup — config, identity, and env resolution for container boot.

Uses only stdlib (runs before uv sync in dev mode).
Writes a sourceable env file for bash to pick up derived values.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# The installed bundle. Resolved from this file rather than hardcoded so the
# same script boots whatever path the wheel landed on (the image pins the
# familiar /workspace/woltspace name at it with a symlink).
WOLTSPACE_DIR = Path(
    os.environ.get("WOLTSPACE_DIR") or Path(__file__).resolve().parent.parent
)
HOME = Path("/home/node")

# Skill sync, hook normalization and session adoption are NOT here any more:
# the control plane runs all three at start, the same way it does natively.
# What is left in this file is the container-only half — per-wolt HOME config,
# harness trust/credential seeding, and the values bash needs.


def resolve_wolt_name(wolts_dir: Path) -> str:
    """Resolve active wolt name: WOLT_NAME env > woltspace.json > first wolt found."""
    env_name = os.environ.get("WOLT_NAME", "")
    if env_name:
        return env_name
    config_file = wolts_dir / "woltspace.json"
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            name = config.get("claude", {}).get("default_wolt", "")
            if name:
                return name
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: first wolt directory found
    for d in sorted(wolts_dir.iterdir()):
        if d.is_dir() and not d.name.startswith(".") and (d / "wolt").is_dir():
            return d.name
    return ""


def resolve_wolt_dir(wolts_dir: Path, wolt_name: str) -> Path:
    candidate = wolts_dir / wolt_name
    if candidate.is_dir():
        return candidate
    return Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))


WORKTUI_SKILL_NOTES = """
## Woltspace notes

- Worktrees live at `WORKTUI_DIR=/workspace/wolts/.worktui` (in the mount — they survive
  container rebuilds).
- `wt` is available on `PATH` in interactive and non-interactive agent shells. Interactive
  shells also source `/home/node/worktui/wt.sh` for directory-switching convenience.
- Beyond the orchestration verbs above, `wt` also manages worktrees directly
  (create/list/delete/clean/pr, ...) — run `wt --help` for the full command reference,
  or `wt` with no arguments for the interactive TUI.
"""


def derive_worktui_skill(woltspace_dir: Path, worktui_dir: Path = HOME / "worktui"):
    """Regenerate the woltspace-worktui skill from worktui's own bundled skill.

    worktui (cloned into the image at ~/worktui) ships its own skill at
    skills/worktui/ — deriving from it at boot means the synced skill always
    matches the installed wt version instead of a hand-maintained copy that
    drifts. The frontmatter name is rewritten to the woltspace- prefix so the
    sync machinery owns it, and woltspace-specific notes are appended.

    No-op if worktui isn't installed — a skill for a missing binary is worse
    than no skill.
    """
    bundled = worktui_dir / "skills" / "worktui" / "SKILL.md"
    dest_dir = woltspace_dir / "container" / "skills" / "woltspace-worktui"
    if not bundled.is_file():
        return

    text = bundled.read_text()
    if text.startswith("---"):
        head, _, body = text[3:].partition("---")
        head = "\n".join(
            "name: woltspace-worktui" if line.strip().startswith("name:") else line
            for line in head.splitlines()
        )
        text = f"---{head}\n---{body}"

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)
    (dest_dir / "SKILL.md").write_text(text.rstrip("\n") + "\n" + WORKTUI_SKILL_NOTES)


def write_bashrc(wolt_dir: Path, wolt_name: str):
    bashrc = HOME / ".bashrc"
    with open(bashrc, "a") as f:
        # wolt() shortcut — $1 and $@ must reach bash unexpanded
        f.write(
            f"wolt() {{\n"
            f'  cd {wolt_dir}\n'
            f'  if [[ "$1" == "--resume" ]]; then\n'
            f"    wclaude --dangerously-skip-permissions --resume\n"
            f"  else\n"
            # TODO: replace with a /wake skill instead of hardcoded greeting
            f'    wclaude --dangerously-skip-permissions "hey {wolt_name}" "$@"\n'
            f"  fi\n"
            f"}}\n"
        )
        worktui = HOME / "worktui" / "wt.sh"
        if worktui.exists():
            f.write(f"source {worktui}\n")




def write_trust_config(wolts_dir: Path):
    # Merge trust entries into existing .claude.json rather than overwriting.
    # Claude Code writes runtime state (firstStartTime, userID, etc.) that it
    # needs to skip the onboarding/theme prompt. Overwriting nukes that state.
    config_path = HOME / ".claude.json"
    config = json.loads(config_path.read_text()) if config_path.exists() else {}

    trust = {"hasTrustDialogAccepted": True, "hasCompletedProjectOnboarding": True}
    projects = config.get("projects", {})
    # Trust the wolts dir itself (needed for onboard mode before any wolts exist)
    projects[str(wolts_dir)] = trust
    for d in sorted(wolts_dir.iterdir()):
        if d.is_dir() and not d.name.startswith("."):
            projects[str(d)] = trust

    config["hasCompletedOnboarding"] = True
    config["bypassPermissionsAccepted"] = True
    config["projects"] = projects
    (HOME / ".claude.json").write_text(json.dumps(config, indent=2) + "\n")


def write_settings_json():
    claude_dir = HOME / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "skipDangerousModePermissionPrompt": True,
    }
    (claude_dir / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")


def configure_git(wolt_name: str):
    subprocess.run(["git", "config", "--global", "user.name", wolt_name], check=True)
    subprocess.run(["git", "config", "--global", "user.email", f"{wolt_name}@woltspace.com"], check=True)
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], check=True)


def scaffold_lodge(wolts_dir: Path):
    """Ensure lodge-level infrastructure exists. Idempotent — safe to call every boot."""
    # Global state dirs
    (wolts_dir / ".space" / "platform").mkdir(parents=True, exist_ok=True)
    (wolts_dir / ".space" / "logs").mkdir(parents=True, exist_ok=True)

    # Session registry (lodge-level, shared across wolts)
    (wolts_dir / ".state" / "registry").mkdir(parents=True, exist_ok=True)

    # woltspace.json — multi-wolt config
    config_file = wolts_dir / "woltspace.json"
    if not config_file.exists():
        config_file.write_text(json.dumps({
            "telegram": {"model": "claude-haiku-4-5", "active_wolt": ""},
            "claude": {"default_wolt": ""},
        }, indent=2) + "\n")

    # Ensure container/bin is on PATH for all shells (docker exec, tmux, etc.)
    bashrc = HOME / ".bashrc"
    bin_path = f'export PATH="{WOLTSPACE_DIR}/container/bin:$PATH"'
    existing = bashrc.read_text() if bashrc.exists() else ""
    if bin_path not in existing:
        with open(bashrc, "a") as f:
            f.write(f"\n{bin_path}\n")


def scaffold_wolt(wolt_name: str, wolts_dir: Path, woltspace_dir: Path) -> Path:
    """Create wolt directory from template if it doesn't exist. Returns wolt_dir."""
    wolt_dir = wolts_dir / wolt_name
    if (wolt_dir / "wolt").is_dir():
        return wolt_dir  # already exists

    # Copy template
    template = woltspace_dir / "template"
    if template.is_dir():
        shutil.copytree(template, wolt_dir, dirs_exist_ok=True)

    # Write wolt.json
    wolt_json = wolt_dir / "wolt" / "wolt.json"
    wolt_json.parent.mkdir(parents=True, exist_ok=True)
    wolt_json.write_text(json.dumps({
        "name": wolt_name,
        "type": "rodent",
        "role": "",
        "capabilities": [],
        "description": "",
    }, indent=2) + "\n")

    # Update woltspace.json with this wolt as active (lodge scaffold ensures file exists)
    config_file = wolts_dir / "woltspace.json"
    try:
        config = json.loads(config_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        config = {}
    config.setdefault("telegram", {})["active_wolt"] = wolt_name
    config.setdefault("claude", {})["default_wolt"] = wolt_name
    config_file.write_text(json.dumps(config, indent=2) + "\n")

    # Init git repo
    if not (wolt_dir / ".git").is_dir():
        subprocess.run(["git", "init", "-q", str(wolt_dir)], check=False)

    # Signal first-run so entrypoint launches /woltspace-create-wolt instead of normal greeting
    first_run = HOME / ".claude" / ".first-run"
    first_run.parent.mkdir(parents=True, exist_ok=True)
    first_run.touch()

    print(f"scaffolded new wolt: {wolt_name}")
    return wolt_dir


def seed_wolf_json(wolt_dir: Path, woltspace_dir: Path):
    target = wolt_dir / "wolt" / "wolf.json"
    template = woltspace_dir / "template" / "wolt" / "wolf.json"
    if not target.exists() and template.exists():
        shutil.copy2(template, target)
        print(f"seeded default wolf.json for {wolt_dir.name}")


def resolve_bot_module(wolt_dir: Path, woltspace_dir: Path, adapter: str) -> tuple:
    """Returns (bot_dir, bot_module) — custom wolt bot or platform default."""
    custom = wolt_dir / "wolt" / "bot" / f"{adapter}_adapter.py"
    if custom.is_file():
        return str(wolt_dir), f"wolt.bot.{adapter}_adapter"
    return str(woltspace_dir / "container"), f"bot.{adapter}_adapter"


PLATFORM_SECTION_START = "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->"
PLATFORM_SECTION_END = "<!-- WOLTSPACE:END -->"


def sync_claude_md_platform_section(wolts_dir: Path, woltspace_dir: Path):
    """Regenerate the platform section at the top of every wolt's CLAUDE.md.

    Preserves everything after the WOLTSPACE:END marker (the wolt's own content).
    If no markers exist, prepends the platform section to the existing content.
    """
    # Import the canonical platform section from wolts.py
    lib_dir = str(woltspace_dir / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        from wolts import _platform_claude_md_section
        platform_block = _platform_claude_md_section()
    except ImportError:
        return  # wolts.py not available yet (first-ever boot)

    for wolt in sorted(wolts_dir.iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        claude_md = wolt / "CLAUDE.md"
        if not claude_md.exists():
            continue

        content = claude_md.read_text()

        if PLATFORM_SECTION_START in content and PLATFORM_SECTION_END in content:
            # Replace existing platform section
            before = content[:content.index(PLATFORM_SECTION_START)]
            after = content[content.index(PLATFORM_SECTION_END) + len(PLATFORM_SECTION_END):]
            # Strip leading newlines from after to avoid double-spacing
            after = after.lstrip("\n")
            new_content = before + platform_block + "\n" + after
        else:
            # No markers — prepend platform section
            new_content = platform_block + "\n" + content

        if new_content != content:
            claude_md.write_text(new_content)


def write_env_file(env_file: Path, env_vars: dict):
    """Write a sourceable shell file with exported vars."""
    lines = []
    for key, value in env_vars.items():
        escaped = value.replace("'", "'\\''")
        lines.append(f"export {key}='{escaped}'")
    env_file.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args()

    woltspace_dir = WOLTSPACE_DIR
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))

    # Lodge infrastructure — always, regardless of whether any wolts exist
    scaffold_lodge(wolts_dir)

    wolt_name = resolve_wolt_name(wolts_dir)

    if wolt_name:
        # Scaffold wolt if it doesn't exist (first boot with new name)
        scaffold_wolt(wolt_name, wolts_dir, woltspace_dir)
        wolt_dir = resolve_wolt_dir(wolts_dir, wolt_name)
    else:
        # No wolts yet — user will create one from the lodge after auth
        wolt_dir = wolts_dir
        print("no wolts found — starting in onboard mode")

    # The image installs a package, so there is no checkout to detect any more —
    # dev mode is a thing you declare (`DEV_MODE=true` in the data root's .env),
    # not a thing inferred from a .git the slim image never has.
    dev_mode = os.environ.get("DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}

    # Config & identity. The worktui skill is regenerated *before* the control
    # plane's skill sync runs (it is the next thing to happen after this exits),
    # so the copy every wolt receives matches the wt in this image.
    derive_worktui_skill(woltspace_dir)
    sync_claude_md_platform_section(wolts_dir, woltspace_dir)
    if wolt_name:
        write_bashrc(wolt_dir, wolt_name)
        seed_wolf_json(wolt_dir, woltspace_dir)
    # Always configure git — new wolts created later via the lodge need it
    configure_git(wolt_name or "wolt")
    write_trust_config(wolts_dir)
    write_settings_json()

    # Resolve derived values for bash
    tg_dir, tg_mod = resolve_bot_module(wolt_dir, woltspace_dir, "telegram")
    slack_dir, slack_mod = resolve_bot_module(wolt_dir, woltspace_dir, "slack")

    write_env_file(Path(args.env_file), {
        "WOLT_NAME": wolt_name,
        "WOLT_DIR": str(wolt_dir),
        "WOLTS_DIR": str(wolts_dir),
        "DEV_MODE": "true" if dev_mode else "false",
        "TELEGRAM_BOT_DIR": tg_dir,
        "TELEGRAM_BOT_MODULE": tg_mod,
        "SLACK_BOT_DIR": slack_dir,
        "SLACK_BOT_MODULE": slack_mod,
        "PYTHONPATH": f"{woltspace_dir}/container/lib:{os.environ.get('PYTHONPATH', '')}",
        "PATH": f"{woltspace_dir}/container/bin:{os.environ.get('PATH', '')}",
    })

    print(f"setup complete: wolt={wolt_name} dir={wolt_dir} dev={dev_mode}")


if __name__ == "__main__":
    main()
