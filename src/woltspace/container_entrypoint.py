"""Container boot, as a subcommand of the installed CLI.

`ENTRYPOINT ["/usr/local/bin/woltspace", "container-entrypoint"]`. The wheel
ships no bash: what used to be `container/entrypoint.sh` (root) →
`container/start.sh` (node) → `container/entrypoint_setup.py` (config) is one
command with two phases, dispatched on uid.

Phase 1 (root) moves the `node` user onto the host's uid/gid, re-owns the two
trees that mount and boot both write, and re-execs *this same command* as node
through gosu.

Phase 2 (node) scaffolds the lodge and the wolt, seeds harness trust/settings
into the per-wolt HOME (container-only by design — native reuses your own login
and copies nothing), assembles the environment every child inherits, opens the
tmux window the human lands in, and then runs the control plane **in-process**:
the same `serve` a native user runs, from the same installed package. No exec,
no shell in between — docker's SIGTERM lands on the process that owns the
connectors, which is the guarantee bash's `exec` used to provide.

Why the entrypoint co-versions with the wheel: an image built on an older
release ran that release's entrypoint. Now there is only one artifact.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .envvars import export_both, get_env, warn_legacy_once
from .layout import resolve_install_root
from .platform_docs import sync_claude_md_platform_section

# The per-wolt HOME the image builds. Containers are the only isolation mode
# that owns a home directory outright, so this is a constant rather than $HOME:
# the root phase runs with HOME=/root and still has to write node's files.
HOME = Path("/home/node")

# The one host mount. Not passed in by the host CLI — the bind target is fixed
# by `docker run -v "$WOLTSPACE_WOLTS_DIR:/workspace/wolts"`.
DEFAULT_WOLTS_DIR = "/workspace/wolts"

TUNNEL_POLL_ATTEMPTS = 45
TUNNEL_POLL_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Phase 1 — root
# ---------------------------------------------------------------------------

def run_root_phase() -> int:
    """Match `node` to the host user, re-own its trees, drop privileges.

    Port of the old `container/entrypoint.sh`. Everything the mount touches has
    to belong to the host's uid or the files a wolt writes are unreadable on the
    host side; everything in the bundle has to belong to node because boot
    writes a derived skill into it.
    """
    host_uid = os.environ.get("HOST_UID") or "1000"
    host_gid = os.environ.get("HOST_GID") or "1000"

    subprocess.run(["groupmod", "-o", "-g", host_gid, "node"], check=True)
    subprocess.run(["usermod", "-o", "-u", host_uid, "-g", host_gid, "node"], check=True)
    subprocess.run(["chown", "node:node", "/workspace"], check=True)
    subprocess.run(["chown", "-R", "node:node", str(HOME)], check=True)
    # The platform is an installed package, so this is its wheel bundle rather
    # than a checkout — a few MB, not the venv. It still has to be re-owned
    # here: `usermod` above just moved node to the host's uid, and boot writes a
    # derived skill into it (container/skills/worktui).
    bundle = resolve_install_root(os.environ.get("WOLTSPACE_DIR"))
    subprocess.run(["chown", "-R", "node:node", str(bundle)], check=True)

    # gosu leaves the environment alone apart from the user it switches to, and
    # this process was started with root's HOME. Name node's home explicitly so
    # nothing downstream depends on gosu's behaviour to find ~/.gitconfig.
    os.environ["HOME"] = str(HOME)
    os.execvp("gosu", ["gosu", "node", "/usr/local/bin/woltspace", "container-entrypoint"])
    return 1  # unreachable — execvp either replaces this process or raises


# ---------------------------------------------------------------------------
# Phase 2 — node: config and identity
#
# These are `container/entrypoint_setup.py` verbatim. The stdlib-only rule they
# were written under is dead (the package is always installed in the image now),
# but they stay import-light: this runs before anything else.
# ---------------------------------------------------------------------------

def resolve_wolt_name(wolts_dir: Path) -> str:
    """Active wolt: env name > woltspace.json > first wolt found."""
    env_name = get_env("WOLTSPACE_WOLT_NAME", "")
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
    return Path(get_env("WOLTSPACE_WOLT_DIR", "/workspace/wolt"))


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


def derive_worktui_skill(woltspace_dir: Path, worktui_dir: Path | None = None):
    """Regenerate the worktui platform skill from worktui's own bundled skill.

    worktui (cloned into the image at ~/worktui) ships its own skill at
    skills/worktui/ — deriving from it at boot means the delivered skill always
    matches the installed wt version instead of a hand-maintained copy that
    drifts. The frontmatter name is normalised to the directory name (what
    every delivery path keys on) and woltspace-specific notes are appended.

    No-op if worktui isn't installed — a skill for a missing binary is worse
    than no skill.
    """
    if worktui_dir is None:
        worktui_dir = HOME / "worktui"
    bundled = worktui_dir / "skills" / "worktui" / "SKILL.md"
    dest_dir = woltspace_dir / "container" / "skills" / "worktui"
    if not bundled.is_file():
        return

    text = bundled.read_text()
    if text.startswith("---"):
        head, _, body = text[3:].partition("---")
        head = "\n".join(
            "name: worktui" if line.strip().startswith("name:") else line
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


def scaffold_lodge(wolts_dir: Path, woltspace_dir: Path):
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
    bin_path = f'export PATH="{woltspace_dir}/container/bin:$PATH"'
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

    # Signal first-run so entrypoint launches create-wolt instead of normal greeting
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


# ---------------------------------------------------------------------------
# Phase 2 — node: environment
# ---------------------------------------------------------------------------

def is_truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def build_environment(
    *, wolt_name: str, wolt_dir: Path, wolts_dir: Path, woltspace_dir: Path,
    dev_mode: bool, env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The variables every child of this process inherits.

    Replaces the sourceable env file. That mechanism existed only so bash could
    read values python had derived; nothing reads it now, so the derived values
    go straight into the environment they were always destined for.
    """
    source = os.environ if env is None else env
    tg_dir, tg_mod = resolve_bot_module(wolt_dir, woltspace_dir, "telegram")
    return export_both({
        "WOLTSPACE_WOLT_NAME": wolt_name,
        "WOLTSPACE_WOLT_DIR": str(wolt_dir),
        "WOLTSPACE_WOLTS_DIR": str(wolts_dir),
        "DEV_MODE": "true" if dev_mode else "false",
        "TELEGRAM_BOT_DIR": tg_dir,
        "TELEGRAM_BOT_MODULE": tg_mod,
        "PYTHONPATH": f"{woltspace_dir}/container/lib:{source.get('PYTHONPATH', '')}",
        "PATH": f"{woltspace_dir}/container/bin:{source.get('PATH', '')}",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        # This process IS the platform entrypoint. Nothing else in the container
        # should claim the data root, the tunnel, or the bot token — a stray
        # `woltspace serve` from a worktree inherits every variable above, so the
        # control plane refuses to act as owner unless it sees this.
        "WOLTSPACE_ENTRYPOINT": "1",
        "WOLTSPACE_ISOLATION": "external",
        "LANG": "C.UTF-8",
    })


# ---------------------------------------------------------------------------
# Phase 2 — node: tmux, services, tunnel
# ---------------------------------------------------------------------------

def runtime_lib_on_path(woltspace_dir: Path) -> None:
    """Make the bundle's `container/lib` importable — the shared runtime code."""
    lib_dir = str(Path(woltspace_dir) / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def _boot_harness(wolt_name: str, wolts_dir: Path) -> str:
    """Resolve the lodge default or an existing wolt's explicit override."""
    runtime_lib_on_path(resolve_install_root(os.environ.get("WOLTSPACE_DIR")))
    from harnesses import get_default_harness, resolve_harness
    if not wolt_name:
        return get_default_harness()
    wolt_json = Path(wolts_dir) / wolt_name / "wolt" / "wolt.json"
    pinned = ""
    try:
        pinned = json.loads(wolt_json.read_text()).get("harness", "") or ""
    except (json.JSONDecodeError, OSError):
        pass
    return resolve_harness(pinned or get_default_harness())


def open_tmux_window(wolt_name: str, wolt_dir: Path, wolts_dir: Path) -> None:
    """The window the human lands in, and the greeting that belongs in it.

    Only the create is tolerant, exactly as bash's `2>/dev/null || true` was: a
    session named `main` already existing is the normal case on a restart.
    Everything after it is fatal under `set -e` and stays fatal here. A tmux
    that cannot be talked to must not produce a healthy-looking API with no
    window behind it — and on first run the `.first-run` marker is already
    spent by then, so a boot that swallowed the failure would never offer the
    creation greeting again.
    """
    subprocess.run(
        ["tmux", "-u", "new-session", "-d", "-s", "main", "-c", str(wolt_dir)],
        capture_output=True, check=False,
    )
    subprocess.run(["tmux", "set", "-g", "mouse", "on"], check=True)

    runtime_lib_on_path(resolve_install_root(os.environ.get("WOLTSPACE_DIR")))
    from harnesses import build_command, platform_skill_invoke

    harness = _boot_harness(wolt_name, wolts_dir)
    first_run = HOME / ".claude" / ".first-run"

    if not wolt_name:
        # The lodge home owns first-run harness selection. No harness process
        # starts until there is a wolt to run, and every CLI owns its own login.
        print(f"onboard mode: wolt_name=<none> harness={harness}")
    elif first_run.is_file():
        first_run.unlink()
        sweep_node_modules(wolts_dir)
        preload_viewport(wolt_name, wolt_dir / ".state")
        # A wolt that has just been scaffolded is on the copy delivery — its
        # skills directory holds `woltspace-create-wolt`, not a plugin. The
        # copy-path spelling is also the safe default anywhere the delivery
        # cannot be read: it is what every un-ratcheted wolt understands.
        skill = platform_skill_invoke(harness, "create-wolt")
        command = build_command(harness, "spawn", prompt=skill)
        send_keys(f"export WOLTSPACE_WOLT_SESSION=main WOLT_SESSION=main && {command}")
    else:
        # TODO: replace with a /wake skill — check for recent sessions, offer
        # resume or fresh start
        send_keys(build_command(harness, "spawn", prompt=f"hey {wolt_name}"))


def send_keys(keys: str) -> None:
    subprocess.run(["tmux", "send-keys", "-t", "main", keys, "Enter"], check=True)


def sweep_node_modules(wolts_dir: Path) -> None:
    """Fresh container — clear node_modules for all apps so installs run clean.

    Prevents binary/native dep corruption across container rebuilds (e.g. Node
    version changes).
    """
    print("fresh container: clearing node_modules in all apps...")
    for root in (wolts_dir / "apps", wolts_dir / "projects"):
        if not root.is_dir():
            continue
        # maxdepth 2, relative to each root — the app dir and one level below it
        candidates = [root / "node_modules", *root.glob("*/node_modules")]
        for nm in sorted(candidates):
            if not nm.is_dir():
                continue
            print(f"  removing {nm}")
            shutil.rmtree(nm, ignore_errors=True)


def preload_viewport(wolt_name: str, state_dir: Path) -> str:
    """Pre-load viewport with the wolt site URL.

    The starter site is scaffolded at creation, so the pond has something on it
    the moment the human looks.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    url = f"/wolt/{wolt_name}/site/"
    data = json.dumps({"url": url, "port": 7777, "updated": int(time.time() * 1000)})
    (state_dir / "current-url-main.json").write_text(data)
    print(f"[viewport:main] → {url}")
    return url


def report_tunnel_url(wolts_dir: Path) -> None:
    """Report the tunnel URL once it lands, without standing in the way.

    The tunnel itself is owned by the control plane (server-side lifecycle);
    this only reads the state file it writes, so the docker log still shows the
    URL without anyone between docker and the supervisor.
    """
    state_file = wolts_dir / ".space" / "platform" / "tunnel.json"
    print("waiting for tunnel...")
    for _ in range(TUNNEL_POLL_ATTEMPTS):
        if state_file.is_file():
            try:
                match = re.search(r'"url": *"([^"]*)"', state_file.read_text())
            except OSError:
                match = None
            if match and match.group(1):
                print(f"tunnel ready: {match.group(1)}")
                return
        time.sleep(TUNNEL_POLL_INTERVAL)
    print("warning: tunnel URL not available yet (server will keep trying)")


def start_tunnel_report(wolts_dir: Path, env: dict[str, str]) -> threading.Thread | None:
    if (env.get("WOLTSPACE_PUBLIC_TUNNEL") or "true") != "true":
        print("tunnel disabled — access via http://localhost:7777")
        return None
    thread = threading.Thread(
        target=report_tunnel_url, args=(wolts_dir,), name="tunnel-report", daemon=True,
    )
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Phase 2 — node
# ---------------------------------------------------------------------------

def run_node_phase() -> int:
    woltspace_dir = resolve_install_root(os.environ.get("WOLTSPACE_DIR"))
    wolts_dir = Path(get_env("WOLTSPACE_WOLTS_DIR") or DEFAULT_WOLTS_DIR)

    # Lodge infrastructure — always, regardless of whether any wolts exist
    scaffold_lodge(wolts_dir, woltspace_dir)

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
    dev_mode = is_truthy(os.environ.get("DEV_MODE"))

    # Config & identity. The worktui skill is regenerated *before* the control
    # plane's skill sync runs (it is the next thing to happen in this process),
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

    os.environ.update(build_environment(
        wolt_name=wolt_name, wolt_dir=wolt_dir, wolts_dir=wolts_dir,
        woltspace_dir=woltspace_dir, dev_mode=dev_mode,
    ))
    print(f"setup complete: wolt={wolt_name} dir={wolt_dir} dev={dev_mode}")

    # Codex seed home — codex errors if CODEX_HOME doesn't exist, and the wolts
    # mount shadows any build-time mkdir. Harmless if codex is never used.
    Path(os.environ.get("CODEX_HOME") or wolts_dir / ".codex").mkdir(
        parents=True, exist_ok=True)

    open_tmux_window(wolt_name, wolt_dir, wolts_dir)
    start_tunnel_report(wolts_dir, dict(os.environ))

    # ── The control plane ──
    # Every chat adapter is now a supervised ChannelConnector; boot must not
    # launch a detached duplicate before handing ownership to `serve`.
    # The same supervisor a native user runs, from the same installed package,
    # in this very process: docker's SIGTERM reaches the owner of the connectors
    # instead of a shell that would leave them orphaned.
    from .cli import serve

    return serve(host="0.0.0.0", port=7777, isolation="external", no_doctor=True)


def main() -> int:
    warn_legacy_once()
    if os.getuid() == 0:
        return run_root_phase()
    return run_node_phase()
