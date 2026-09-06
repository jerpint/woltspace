"""Platform skill sync — keeps every wolt's woltspace-* skills current.

Lives in lib/ rather than the entrypoint because both runtimes need it: the
container syncs on boot, the native control plane syncs on `woltspace start`.
"""

import contextlib
import fcntl
import os
import shutil
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
