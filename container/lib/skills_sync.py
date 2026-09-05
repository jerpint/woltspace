"""Platform skill sync — keeps every wolt's woltspace-* skills current.

Lives in lib/ rather than the entrypoint because both runtimes need it: the
container syncs on boot, the native control plane syncs on `woltspace start`.
"""

import shutil
from pathlib import Path


def platform_skills_dir(woltspace_dir: Path) -> Path:
    return Path(woltspace_dir) / "container" / "skills"


def sync_wolt_skills(platform_skills: Path, skills_dir: Path):
    """Replace the woltspace-* skills inside one wolt's skills directory."""
    for d in skills_dir.glob("woltspace-*"):
        if d.is_dir():
            shutil.rmtree(d)

    for d in platform_skills.glob("woltspace-*"):
        if d.is_dir():
            shutil.copytree(d, skills_dir / d.name)


def sync_all_wolt_skills(woltspace_dir: Path, wolts_dir: Path):
    """Sync woltspace-* platform skills to every wolt's .claude/skills/.

    Only touches woltspace-* prefixed skills — wolt-owned skills (no prefix)
    are never modified. The legacy/ folder is never copied.
    """
    platform_skills = platform_skills_dir(woltspace_dir)
    if not platform_skills.is_dir():
        return

    for wolt in sorted(Path(wolts_dir).iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        skills_dir = wolt / ".claude" / "skills"
        if not skills_dir.exists():
            # Skip wolts without .claude/skills/ (non-rodents, etc.)
            continue

        sync_wolt_skills(platform_skills, skills_dir)


def seed_wolt_skills(woltspace_dir: Path, wolt_dir: Path):
    """Give a newly created wolt its .claude/skills/.

    sync_all_wolt_skills deliberately skips wolts that have no skills
    directory, so a wolt that is never seeded is never synced either.
    """
    platform_skills = platform_skills_dir(woltspace_dir)
    if not platform_skills.is_dir():
        return

    skills_dir = Path(wolt_dir) / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    sync_wolt_skills(platform_skills, skills_dir)
