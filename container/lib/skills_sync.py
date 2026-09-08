"""Platform skill delivery — keeps every wolt's woltspace skills current.

Lives in lib/ rather than the entrypoint because both runtimes need it: the
container syncs on boot, the native control plane syncs on `woltspace start`.

Two delivery paths run side by side while the colony ratchets over:

  copy sync (default)
      Every platform skill is copied into `<wolt>/.claude/skills/` under a
      `woltspace-` prefix. The prefix IS the namespace here — nothing else
      keeps a platform skill from colliding with a wolt's own. The sources
      lost that prefix when the plugin took over the namespacing, so this path
      renames on the way in (see `_delivered_name`) and the wolts on it keep
      exactly the names their boot prompts already use.

  plugin (`"skills_delivery": "plugin"` in wolt.json)
      Nothing is copied. One symlink, `<wolt>/.claude/skills/woltspace ->
      <platform skills dir>`, feeds codex and opencode the whole tree (both
      recurse; claude does not, so the link is invisible to it). Claude gets
      the same directory as an installed plugin, which namespaces every skill
      under `woltspace:`. All three harnesses read skill CONTENT live from
      the source directory, so an upgrade needs no re-delivery: codex and
      opencode follow the symlink, and claude — although the install drops a
      copy under `.claude/plugins/cache/` — demonstrably serves a
      directory-marketplace plugin's skills from the source path (probed by
      editing a skill after install: the next session quotes the edit while
      the cache copy still lacks it). The cache copy is dead weight for
      skills; only if a future plugin ships components beyond skills should
      `claude plugin update` after upgrades be revisited.

The second path is a ratchet, not a flag day: a wolt opts in, the stale copies
the first path owns are swept out for it, and everyone else is untouched.
"""

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Staging and retirement names for the atomic swap. Dotted, so the
# `woltspace-*` glob never sees them and claude never reads them as skills.
STAGE_SUFFIX = ".wsync-new"
RETIRED_SUFFIX = ".wsync-old"

# Every name we stage or retire is ".<platform-skill><suffix>", and platform
# skills are always woltspace-*. The recovery pass matches on that, not on
# "any dotted thing ending in our suffix" — the wolt's own directory is theirs,
# and a `.notes.wsync-new` they made is none of our business to delete.
_OURS_GLOB = ".woltspace-*"

LOCK_NAME = ".woltspace-skills-sync.lock"

# The prefix the copy-sync path delivers under. Sources dropped it when the
# plugin took over namespacing, so this path puts it back — and dies with the
# ratchet, along with everything else that mentions it.
LEGACY_PREFIX = "woltspace-"

# Plugin delivery names. The marketplace, the plugin inside it, and the entry
# in `<wolt>/.claude/skills/` that points every recursing harness at the source.
PLUGIN_MARKETPLACE = "woltspace"
PLUGIN_NAME = "woltspace"
PLUGIN_ID = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE}"
PLUGIN_LINK_NAME = "woltspace"

# wolt.json opt-in. Anything else (including absent) means the copy sync.
# These names are the contract between delivery and invocation: how a skill is
# spelled in a prompt follows how it was delivered, so harnesses.py reads them
# from here rather than keeping a second copy.
DELIVERY_KEY = "skills_delivery"
PLUGIN_DELIVERY = "plugin"
COPY_DELIVERY = "copy"

# `claude plugin install` reaches the network for a remote marketplace; ours is
# a local directory, so this is generous rather than tight.
PLUGIN_INSTALL_TIMEOUT = 120


def _warn(msg: str):
    print(f"⚠️  skills sync: {msg}", file=sys.stderr)


@contextlib.contextmanager
def _exclusive(skills_dir: Path):
    """Hold an exclusive lock over one wolt's skills directory.

    A sync is a sequence of stage-then-rename swaps under fixed names. Two of
    them at once — boot racing a manual `woltspace start`, or two control
    planes — share every staging name: one can rename the other's half-copied
    stage into place and then delete the skill that was there. The lock is a
    sidecar file in the same directory rather than the directory itself,
    because directories are what we rename.

    Yields True while held. Yields False when the lock could not be taken, and
    the caller then skips the sync entirely: the holder is copying the same
    sources into the same directory, so waiting our turn would only redo work
    that is already being done. Non-blocking for exactly that reason.
    """
    lock_path = skills_dir / LOCK_NAME
    fd = None
    try:
        skills_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
        _warn(f"another sync holds {lock_path} — skipping {skills_dir}")
        yield False
        return
    except OSError as exc:
        if fd is not None:
            os.close(fd)
        _warn(f"could not lock {lock_path}: {exc} — skipping {skills_dir}")
        yield False
        return
    try:
        yield True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def platform_skills_dir(woltspace_dir: Path) -> Path:
    return Path(woltspace_dir) / "container" / "skills"


def platform_skill_sources_of(source_dir: Path) -> list[Path]:
    """`platform_skill_sources` for an already-resolved skills directory."""
    source_dir = Path(source_dir)
    if not source_dir.is_dir():
        return []
    return sorted(d for d in source_dir.iterdir()
                  if d.is_dir() and not d.name.startswith("."))


def platform_skill_sources(woltspace_dir: Path) -> list[Path]:
    """The skill directories this install can hand out.

    Every top-level directory under container/skills/ is a platform skill —
    the `woltspace-` prefix that used to mark them moved into the delivery
    (plugin namespace, or `_delivered_name` for the copy path). Dotted entries
    are not skills: `.claude-plugin/` is the plugin manifest, and a wolt's own
    dotdirs are none of our business.

    Empty when the install root is wrong — a stale bundle, a half-copied
    checkout, a skills folder holding nothing we ship. Callers treat an empty
    list as "this install has nothing to say" and leave every wolt alone.
    """
    return platform_skill_sources_of(platform_skills_dir(woltspace_dir))


def _delivered_name(source: Path) -> str:
    """What a copy-synced skill is called inside a wolt's skills directory.

    The copy path has no namespace of its own, so it wears the historical
    `woltspace-` prefix: the wolts on it have boot prompts, CLAUDE.md sections
    and habits built on those exact names. Sources are bare now, so the prefix
    is added here instead of being carried in the tree.
    """
    return f"{LEGACY_PREFIX}{source.name}"


def _rename_frontmatter(skill_md: Path, name: str) -> None:
    """Rewrite a staged SKILL.md's frontmatter `name:` to its delivered name.

    Claude reads a skill's name off its directory; codex reads it off the
    frontmatter. Renaming the directory alone therefore delivers a skill that
    two harnesses in the same colony call two different things — a copy-path
    codex wolt would see `notify` where its boot prompt says
    `woltspace-notify`. Only ever touches a staged copy, never a source.
    """
    try:
        text = skill_md.read_text()
    except OSError:
        return
    if not text.startswith("---"):
        return
    head, sep, body = text[3:].partition("---")
    if not sep:
        return
    head = "\n".join(f"name: {name}" if line.strip().startswith("name:") else line
                     for line in head.splitlines())
    try:
        skill_md.write_text(f"---{head}\n---{body}")
    except OSError as exc:
        _warn(f"could not rename {skill_md}: {exc}")


def _stage_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}{STAGE_SUFFIX}"


def _retired_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}{RETIRED_SUFFIX}"


def _recover_interrupted_sync(skills_dir: Path):
    """Finish what a killed sync left behind, before touching anything new.

    A swap is two renames. Die between them and the skill is gone from its
    real name but intact under its retired name — put it back. Staging dirs
    are always incomplete copies, so they just go.

    Only ever our own leftovers: `.woltspace-<something><suffix>`. A wolt is
    free to keep a `.notes.wsync-new` of its own in there and we will not
    touch it.
    """
    for stale in skills_dir.glob(f"{_OURS_GLOB}{RETIRED_SUFFIX}"):
        name = stale.name[1:-len(RETIRED_SUFFIX)]
        live = skills_dir / name
        if live.exists():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            os.replace(stale, live)
    for stale in skills_dir.glob(f"{_OURS_GLOB}{STAGE_SUFFIX}"):
        shutil.rmtree(stale, ignore_errors=True)


def _swap_in_skill(source: Path, skills_dir: Path):
    """Install one skill so the wolt never sees a missing or partial copy.

    Copy to a dotted sibling on the same filesystem, then rename it into
    place. The old copy is only retired once its replacement is complete, and
    only deleted once the new one is live.
    """
    name = _delivered_name(source)
    live = skills_dir / name
    staged = _stage_path(skills_dir, name)
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(source, staged)
    _rename_frontmatter(staged / "SKILL.md", name)

    if live.exists():
        # The retired name must be the DELIVERED name: `_recover_interrupted_sync`
        # only looks for its own leftovers, and it knows them by that name. Retire
        # under anything else and a crash between the two renames strands the old
        # copy where recovery cannot see it — while recovery deletes the staged
        # replacement, because a staging dir is always assumed incomplete.
        retired = _retired_path(skills_dir, name)
        shutil.rmtree(retired, ignore_errors=True)
        os.replace(live, retired)
        os.replace(staged, live)
        shutil.rmtree(retired, ignore_errors=True)
    else:
        os.replace(staged, live)


def sync_wolt_skills(sources: list[Path], skills_dir: Path):
    """Replace the copy-synced platform skills inside one wolt's skills dir.

    Sources are bare-named; they land under `woltspace-<name>` (see
    `_delivered_name`), which is the only namespace this path has.

    Crash-safe: each skill is staged beside its target and swapped in with a
    rename, and skills this install no longer ships are removed only after
    every replacement is in place. An interrupted sync leaves a wolt with the
    old skill or the new one — never a gap, never a half-copied directory.

    Refuses to run on an empty source list: the delete pass would strip every
    platform skill the wolt has and the copy pass would put nothing back.

    The whole body runs under an exclusive lock on the skills directory, so
    two syncs never share a staging name — see `_exclusive`.
    """
    if not sources:
        return

    skills_dir = Path(skills_dir)
    with _exclusive(skills_dir) as locked:
        if not locked:
            return

        _recover_interrupted_sync(skills_dir)

        for d in sources:
            _swap_in_skill(d, skills_dir)

        keep = {_delivered_name(d) for d in sources}
        for d in skills_dir.glob(f"{LEGACY_PREFIX}*"):
            if d.is_dir() and d.name not in keep:
                shutil.rmtree(d)


# ---------------------------------------------------------------------------
# Plugin delivery — one symlink, and (for claude) one installed plugin
# ---------------------------------------------------------------------------

def _ensure_symlink(link: Path, target: Path) -> bool:
    """Point `link` at `target`, replacing a link that points elsewhere.

    Only ever touches this one entry. Wolt-owned skills live in the same
    directory, and a real directory sitting on the name is not ours to delete —
    that gets a warning and no delivery, which is recoverable; an rmtree is
    not.
    """
    target = Path(target).resolve()
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if Path(os.readlink(link)) == target:
            return True
        link.unlink()
    elif link.exists():
        _warn(f"{link} exists and is not a symlink — leaving it alone")
        return False
    os.symlink(target, link, target_is_directory=True)
    return True


class _MergeConflict(Exception):
    """A key we need to write into holds something that is not an object."""


def _deep_merge(base: dict, overlay: dict, path: str = "") -> dict:
    """Overlay onto base, recursing into dicts. Unrelated keys survive.

    "Survive" is the whole promise, so a collision is a refusal rather than an
    overwrite: if we need `enabledPlugins` to be an object and this wolt has a
    list there, replacing it silently destroys settings we were supposed to
    preserve. Raises `_MergeConflict`; the caller leaves the file alone.
    """
    for key, value in overlay.items():
        here = f"{path}.{key}" if path else key
        if isinstance(value, dict):
            existing = base.get(key)
            if existing is None:
                base[key] = {}
            elif not isinstance(existing, dict):
                raise _MergeConflict(here)
            _deep_merge(base[key], value, here)
        else:
            base[key] = value
    return base


def _merge_plugin_settings(claude_dir: Path, source_dir: Path) -> None:
    """Teach this wolt's claude about the woltspace marketplace and plugin.

    A settings file is a wolt's own — everything not ours is read back and
    written out untouched. An unparseable one is left where it is rather than
    overwritten: losing a wolt's settings to a stray comma is worse than
    skipping a delivery.
    """
    settings_path = claude_dir / "settings.json"
    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            _warn(f"could not read {settings_path}: {exc} — not touching it")
            return
        if not isinstance(settings, dict):
            _warn(f"{settings_path} is not an object — not touching it")
            return

    try:
        _deep_merge(settings, {
            "extraKnownMarketplaces": {
                PLUGIN_MARKETPLACE: {
                    "source": {"source": "directory", "path": str(source_dir)},
                },
            },
            "enabledPlugins": {PLUGIN_ID: True},
        })
    except _MergeConflict as clash:
        _warn(f"{settings_path} holds a non-object at {clash} — not touching it")
        return
    claude_dir.mkdir(parents=True, exist_ok=True)
    tmp = settings_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n")
    os.replace(tmp, settings_path)


def _plugin_installed(home: Path) -> bool:
    """Has claude already installed the woltspace plugin for this HOME?

    Settings alone lag a session behind — the install command is what makes
    the plugin live immediately — so this is the check that decides whether to
    spend a subprocess. The installed-plugins file has carried both a flat
    `<plugin>@<marketplace>` key and a marketplace → plugins mapping; the
    current one (schema version 2, verified live 2026-09-06) nests the flat key
    under "plugins". Accept all three, and treat anything unreadable as "not
    installed" — a redundant install is cheap and idempotent, a missing one
    costs the wolt every platform skill.
    """
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    # {"version": 2, "plugins": {"woltspace@woltspace": [...]}} — the live shape
    nested = data.get("plugins")
    if isinstance(nested, dict) and PLUGIN_ID in nested:
        return True
    if PLUGIN_ID in data:
        return True
    entry = data.get(PLUGIN_MARKETPLACE)
    if isinstance(entry, (dict, list, set, tuple)):
        return PLUGIN_NAME in entry
    return False


def _claude_plugin(home: Path, *args: str) -> tuple[bool, str]:
    """Run one `claude plugin ...` under this wolt's HOME.

    Returns (ok, last line of output). Failure is never an exception: this runs
    inside boot, and a colony that will not start because one wolt's claude
    binary is missing is a far worse outcome than a wolt whose plugin lands on
    the next pass.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    try:
        proc = subprocess.run(
            ["claude", "plugin", *args],
            env=env, cwd=str(home), capture_output=True, text=True,
            timeout=PLUGIN_INSTALL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode == 0, (detail[-1] if detail else f"exit {proc.returncode}")


def _install_plugin(home: Path, source_dir: Path) -> bool:
    """Register the marketplace, then install the plugin. Returns delivered?

    The marketplace add is not optional and not a belt-and-braces extra.
    Writing `extraKnownMarketplaces` into settings.json declares the
    marketplace for a future *session*; it does not register it for the CLI, so
    `claude plugin install woltspace@woltspace` in a fresh HOME fails with
    `Plugin "woltspace" not found in marketplace "woltspace"` (reproduced live,
    2026-09-06). `marketplace add` is what puts it on disk. Both commands are
    idempotent — a second run reports "already on disk" / "already installed"
    and exits 0.

    The answer comes from re-reading the installed-plugins file rather than
    from the exit code, because the caller uses it to decide whether it is safe
    to delete this wolt's copy-synced skills. Only a plugin that is actually
    there earns that.
    """
    ok, detail = _claude_plugin(home, "marketplace", "add", str(source_dir))
    if not ok:
        _warn(f"could not add the {PLUGIN_MARKETPLACE} marketplace for {home}: {detail}")
        return False
    ok, detail = _claude_plugin(home, "install", PLUGIN_ID)
    if not ok:
        _warn(f"{PLUGIN_ID} install failed for {home}: {detail}")
    return _plugin_installed(home)


def _remove_stale_copies(skills_dir: Path, sources: list[Path]) -> None:
    """Sweep out the copies the copy-sync path put here.

    Exactly the names that path owns — `woltspace-<platform skill>` — and no
    others. A wolt is free to keep its own `woltspace-notes`; it is not a
    platform skill name, so it stays.
    """
    for source in sources:
        stale = skills_dir / _delivered_name(source)
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale, ignore_errors=True)


def _ensure_agents_bridge(wolt_dir: Path) -> None:
    """Point codex's skills directory at the wolt's claude one.

    codex reads `$HOME/.agents/skills`, not `.claude/skills`. In container mode
    the wcodex wrapper lays this bridge on every launch — but it exits early in
    host isolation, so a native codex wolt never gets one and sees no skills at
    all. Same shape wcodex writes (a relative link, so the wolt directory stays
    movable), same real-directory guard: something that is not our symlink is
    not ours to replace.
    """
    agents = wolt_dir / ".agents"
    link = agents / "skills"
    if link.is_symlink():
        if Path(os.readlink(link)) == Path("../.claude/skills"):
            return
        link.unlink()
    elif link.exists():
        _warn(f"{link} exists and is not a symlink — leaving it alone")
        return
    agents.mkdir(parents=True, exist_ok=True)
    os.symlink("../.claude/skills", link, target_is_directory=True)


def ensure_platform_skills(wolt_dir: Path, harness: str, source_dir: Path) -> bool:
    """Deliver the platform skills to one wolt as a plugin, not as copies.

    The symlink is the whole delivery for codex and opencode: both recurse
    through the skills directory, including through a symlink, so one entry
    hands them the entire tree — and claude, which does not recurse, never
    sees it. Claude is told about the same directory as a marketplace instead,
    and the plugin namespaces every skill under `woltspace:`.

    Returns whether delivery is confirmed. Nothing is swept out of the wolt's
    skills directory unless it is: the copy-synced skills are the only ones a
    wolt has until the plugin is genuinely in place, and deleting them on an
    unconfirmed delivery leaves it with no platform skills at all.

    Idempotent, and safe to run on every boot.
    """
    wolt_dir = Path(wolt_dir)
    source_dir = Path(source_dir)
    skills_dir = wolt_dir / ".claude" / "skills"

    if not _ensure_symlink(skills_dir / PLUGIN_LINK_NAME, source_dir):
        return False
    _ensure_agents_bridge(wolt_dir)

    if harness == "claude":
        _merge_plugin_settings(wolt_dir / ".claude", source_dir)
        if not _plugin_installed(wolt_dir) and not _install_plugin(wolt_dir, source_dir):
            _warn(f"{PLUGIN_ID} is not installed for {wolt_dir} — "
                  "keeping its copy-synced skills")
            return False

    _remove_stale_copies(skills_dir, platform_skill_sources_of(source_dir))
    return True


# ---------------------------------------------------------------------------
# Dispatch — which delivery a wolt is on
# ---------------------------------------------------------------------------

def _wolt_config(wolt_dir: Path) -> dict:
    try:
        data = json.loads((wolt_dir / "wolt" / "wolt.json").read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _lodge_default_harness(wolts_dir: Path) -> str:
    try:
        cfg = json.loads((Path(wolts_dir) / "woltspace.json").read_text())
        return cfg.get("harness", {}).get("default") or "claude"
    except (json.JSONDecodeError, OSError, AttributeError):
        return "claude"


def wolt_skills_delivery(wolt_dir: Path) -> str:
    """Which delivery path this wolt is on: "plugin" or "copy".

    The public answer to "how are this wolt's platform skills named?", which is
    what every prompt-building caller actually needs. Anything but an explicit
    opt-in is the copy path — an unreadable wolt.json, a missing one, a wolt
    that predates the key. Guessing "plugin" for a wolt that is not on it hands
    its session a skill name that does not exist; guessing "copy" wrongly is
    the state the whole colony was in yesterday.
    """
    if _wolt_config(Path(wolt_dir)).get(DELIVERY_KEY) == PLUGIN_DELIVERY:
        return PLUGIN_DELIVERY
    return COPY_DELIVERY


def _wolt_harness(config: dict, wolts_dir: Path) -> str:
    """The harness this wolt's sessions run on: pin, then the lodge default.

    Read here rather than imported from harnesses.py on purpose — this module
    runs at the very front of boot and stays import-light.
    """
    return config.get("harness") or _lodge_default_harness(wolts_dir)


def sync_all_wolt_skills(woltspace_dir: Path, wolts_dir: Path):
    """Deliver the platform skills to every wolt, each by its own path.

    A wolt with `"skills_delivery": "plugin"` in wolt.json gets the symlink +
    plugin delivery; everyone else gets the copy sync, under the historical
    `woltspace-` names. Wolt-owned skills are never modified either way.

    A source with no platform skills at all is a no-op for the whole colony.
    That reading is deliberate: an install root pointing somewhere stale is
    far likelier than a real platform that genuinely ships zero skills, and
    the cost of guessing wrong is every wolt losing every platform skill at
    once.
    """
    sources = platform_skill_sources(woltspace_dir)
    if not sources:
        return
    source_dir = platform_skills_dir(woltspace_dir)

    for wolt in sorted(Path(wolts_dir).iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        skills_dir = wolt / ".claude" / "skills"
        if not skills_dir.exists():
            # Skip wolts without .claude/skills/ (non-rodents, etc.)
            continue

        config = _wolt_config(wolt)
        if wolt_skills_delivery(wolt) == PLUGIN_DELIVERY:
            ensure_platform_skills(wolt, _wolt_harness(config, wolts_dir), source_dir)
        else:
            sync_wolt_skills(sources, skills_dir)


def seed_wolt_skills(woltspace_dir: Path, wolt_dir: Path):
    """Give a newly created wolt its .claude/skills/.

    sync_all_wolt_skills deliberately skips wolts that have no skills
    directory, so a wolt that is never seeded is never synced either. When the
    source has no woltspace-* skills the wolt is left unseeded rather than
    handed an empty directory: an empty skills dir is no more useful than none,
    and leaving it absent keeps a later sync from a healthy install honest —
    it will still skip this wolt, and the next seed can do the job properly.
    """
    sources = platform_skill_sources(woltspace_dir)
    if not sources:
        return

    skills_dir = Path(wolt_dir) / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    sync_wolt_skills(sources, skills_dir)
