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

WOLTSPACE_DIR = Path("/workspace/woltspace")
HOME = Path("/home/node")


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


def sync_all_wolt_skills(woltspace_dir: Path, wolts_dir: Path):
    """Sync woltspace-* platform skills to every wolt's .claude/skills/.

    Only touches woltspace-* prefixed skills — wolt-owned skills (no prefix)
    are never modified. The legacy/ folder is never copied.
    """
    platform_skills = woltspace_dir / "container" / "skills"
    if not platform_skills.is_dir():
        return

    for wolt in sorted(wolts_dir.iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        skills_dir = wolt / ".claude" / "skills"
        if not skills_dir.exists():
            # Skip wolts without .claude/skills/ (non-rodents, etc.)
            continue

        # Remove old platform skills
        for d in skills_dir.glob("woltspace-*"):
            if d.is_dir():
                shutil.rmtree(d)

        # Copy fresh platform skills
        for d in platform_skills.glob("woltspace-*"):
            if d.is_dir():
                shutil.copytree(d, skills_dir / d.name)


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


def write_settings_json(woltspace_dir: Path):
    hooks_dir = woltspace_dir / "container" / "hooks"
    if not (hooks_dir / "session-done.sh").exists():
        return
    claude_dir = HOME / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings = {
        "skipDangerousModePermissionPrompt": True,
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(hooks_dir / "session-done.sh")}]}],
            "Notification": [{"hooks": [{"type": "command", "command": str(hooks_dir / "notify.sh")}]}],
        },
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


def resolve_wolf_config(wolts_dir: Path, wolt_dir: Path) -> str:
    """Find wolf.json — dedicated wolf-wolt first, then fallback to active wolt."""
    try:
        config = json.loads((wolts_dir / "woltspace.json").read_text())
        active_wolf = config.get("creatures", {}).get("active_wolf", "")
        if active_wolf:
            wolf_json = wolts_dir / active_wolf / "wolt" / "wolf.json"
            if wolf_json.is_file():
                return str(wolf_json)
    except (json.JSONDecodeError, OSError):
        pass
    fallback = wolt_dir / "wolt" / "wolf.json"
    return str(fallback) if fallback.is_file() else ""


def resolve_bot_module(wolt_dir: Path, woltspace_dir: Path, adapter: str) -> tuple:
    """Returns (bot_dir, bot_module) — custom wolt bot or platform default."""
    custom = wolt_dir / "wolt" / "bot" / f"{adapter}_adapter.py"
    if custom.is_file():
        return str(wolt_dir), f"wolt.bot.{adapter}_adapter"
    return str(woltspace_dir / "container"), f"bot.{adapter}_adapter"


def reconcile_sessions(wolts_dir: Path, woltspace_dir: Path):
    """Mark any 'running' sessions as orphaned if their tmux session is gone.

    Runs on container boot before tmux starts, so any session still marked
    'running' from a previous boot is definitely dead.
    """
    registry_dir = wolts_dir / ".state" / "registry"
    if not registry_dir.is_dir():
        return
    lib_dir = str(woltspace_dir / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        from sessions import SessionRegistry
        reg = SessionRegistry(registry_dir)
        orphaned = reg.reconcile()
        if orphaned:
            print(f"reconciled {len(orphaned)} orphaned session(s): {', '.join(orphaned)}")
    except Exception as e:
        print(f"session reconcile skipped: {e}", file=sys.stderr)


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


def symlink_node_modules(woltspace_dir: Path):
    target = Path("/workspace/node_modules")
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(woltspace_dir / "node_modules")


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

    dev_mode = (woltspace_dir / ".git").is_dir()

    # Config & identity
    sync_all_wolt_skills(woltspace_dir, wolts_dir)
    sync_claude_md_platform_section(wolts_dir, woltspace_dir)
    if wolt_name:
        write_bashrc(wolt_dir, wolt_name)
        seed_wolf_json(wolt_dir, woltspace_dir)
    # Always configure git — new wolts created later via the lodge need it
    configure_git(wolt_name or "wolt")
    write_trust_config(wolts_dir)
    write_settings_json(woltspace_dir)
    symlink_node_modules(woltspace_dir)

    # Clean up stale sessions from previous boot
    reconcile_sessions(wolts_dir, woltspace_dir)

    # Resolve derived values for bash
    wolf_config = resolve_wolf_config(wolts_dir, wolt_dir)
    tg_dir, tg_mod = resolve_bot_module(wolt_dir, woltspace_dir, "telegram")
    slack_dir, slack_mod = resolve_bot_module(wolt_dir, woltspace_dir, "slack")

    write_env_file(Path(args.env_file), {
        "WOLT_NAME": wolt_name,
        "WOLT_DIR": str(wolt_dir),
        "WOLTS_DIR": str(wolts_dir),
        "DEV_MODE": "true" if dev_mode else "false",
        "WOLF_CONFIG": wolf_config,
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
