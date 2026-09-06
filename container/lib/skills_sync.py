"""Platform skill sync — keeps every wolt's woltspace-* skills current.

Lives in lib/ rather than the entrypoint because both runtimes need it: the
container syncs on boot, the native control plane syncs on `woltspace start`.
"""

import os
import shutil
from pathlib import Path

# Staging and retirement names for the atomic swap. Dotted, so the
# `woltspace-*` glob never sees them and claude never reads them as skills.
STAGE_SUFFIX = ".wsync-new"
RETIRED_SUFFIX = ".wsync-old"


def platform_skills_dir(woltspace_dir: Path) -> Path:
    return Path(woltspace_dir) / "container" / "skills"


def platform_skill_sources(woltspace_dir: Path) -> list[Path]:
    """The woltspace-* skill directories this install can hand out.

    Empty when the install root is wrong — a stale bundle, a half-copied
    checkout, a skills folder holding only legacy/. Callers treat an empty
    list as "this install has nothing to say" and leave every wolt alone.
    """
    platform_skills = platform_skills_dir(woltspace_dir)
    if not platform_skills.is_dir():
        return []
    return sorted(d for d in platform_skills.glob("woltspace-*") if d.is_dir())


def _stage_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}{STAGE_SUFFIX}"


def _retired_path(skills_dir: Path, name: str) -> Path:
    return skills_dir / f".{name}{RETIRED_SUFFIX}"


def _recover_interrupted_sync(skills_dir: Path):
    """Finish what a killed sync left behind, before touching anything new.

    A swap is two renames. Die between them and the skill is gone from its
    real name but intact under its retired name — put it back. Staging dirs
    are always incomplete copies, so they just go.
    """
    for stale in skills_dir.glob(f".*{RETIRED_SUFFIX}"):
        name = stale.name[1:-len(RETIRED_SUFFIX)]
        live = skills_dir / name
        if live.exists():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            os.replace(stale, live)
    for stale in skills_dir.glob(f".*{STAGE_SUFFIX}"):
        shutil.rmtree(stale, ignore_errors=True)


def _swap_in_skill(source: Path, skills_dir: Path):
    """Install one skill so the wolt never sees a missing or partial copy.

    Copy to a dotted sibling on the same filesystem, then rename it into
    place. The old copy is only retired once its replacement is complete, and
    only deleted once the new one is live.
    """
    live = skills_dir / source.name
    staged = _stage_path(skills_dir, source.name)
    shutil.rmtree(staged, ignore_errors=True)
    shutil.copytree(source, staged)

    if live.exists():
        retired = _retired_path(skills_dir, source.name)
        shutil.rmtree(retired, ignore_errors=True)
        os.replace(live, retired)
        os.replace(staged, live)
        shutil.rmtree(retired, ignore_errors=True)
    else:
        os.replace(staged, live)


def sync_wolt_skills(sources: list[Path], skills_dir: Path):
    """Replace the woltspace-* skills inside one wolt's skills directory.

    Crash-safe: each skill is staged beside its target and swapped in with a
    rename, and skills this install no longer ships are removed only after
    every replacement is in place. An interrupted sync leaves a wolt with the
    old skill or the new one — never a gap, never a half-copied directory.

    Refuses to run on an empty source list: the delete pass would strip every
    platform skill the wolt has and the copy pass would put nothing back.
    """
    if not sources:
        return

    _recover_interrupted_sync(skills_dir)

    for d in sources:
        _swap_in_skill(d, skills_dir)

    keep = {d.name for d in sources}
    for d in skills_dir.glob("woltspace-*"):
        if d.is_dir() and d.name not in keep:
            shutil.rmtree(d)


def sync_all_wolt_skills(woltspace_dir: Path, wolts_dir: Path):
    """Sync woltspace-* platform skills to every wolt's .claude/skills/.

    Only touches woltspace-* prefixed skills — wolt-owned skills (no prefix)
    are never modified. The legacy/ folder is never copied.

    A source with no woltspace-* skills at all is a no-op for the whole
    colony. That reading is deliberate: an install root pointing somewhere
    stale is far likelier than a real platform that genuinely ships zero
    skills, and the cost of guessing wrong is every wolt losing every
    platform skill at once.
    """
    sources = platform_skill_sources(woltspace_dir)
    if not sources:
        return

    for wolt in sorted(Path(wolts_dir).iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        skills_dir = wolt / ".claude" / "skills"
        if not skills_dir.exists():
            # Skip wolts without .claude/skills/ (non-rodents, etc.)
            continue

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
