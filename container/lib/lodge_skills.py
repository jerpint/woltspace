"""Local lodge-owned skills, linked into each wolt's existing skill paths."""

import os
import re
import sys
from pathlib import Path

NAME = re.compile(r"lodge-[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def shared_skills_dir(wolts_dir: Path) -> Path:
    return Path(wolts_dir) / ".space" / "shared-skills"


def _warn(message: str) -> None:
    print(f"⚠️  lodge skills: {message}", file=sys.stderr)


def _sources(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    sources = {}
    for skill in sorted(root.iterdir()):
        if skill.name.startswith("."):
            continue
        if not skill.is_dir():
            continue
        if skill.is_symlink() or not NAME.fullmatch(skill.name) or len(skill.name) > 64:
            _warn(f"skipping {skill}: use a local directory named lodge-<name>")
            continue
        try:
            text = (skill / "SKILL.md").read_text()
            if not (skill / "SKILL.md").resolve().is_relative_to(skill.resolve()):
                raise ValueError("SKILL.md must stay inside its skill directory")
            # Names use the Agent Skills lowercase/dash format. Accept bare or
            # quoted YAML scalars, with an optional inline comment; never rewrite
            # the lodge owner's source to fix a mismatch.
            parts = text.splitlines()
            if not parts or parts[0] != "---":
                raise ValueError("SKILL.md needs YAML frontmatter")
            end = parts.index("---", 1)
            names = [line for line in parts[1:end] if line.startswith("name:")]
            scalar = re.escape(skill.name)
            pattern = rf"name:\s*(?:{scalar}|'{scalar}'|\"{scalar}\")\s*(?:#.*)?"
            if len(names) != 1 or not re.fullmatch(pattern, names[0]):
                raise ValueError("frontmatter name must match the lodge- directory name")
        except (OSError, UnicodeError, ValueError) as exc:
            _warn(f"skipping {skill}: {exc}")
            continue
        sources[skill.name] = skill
    return sources


def _owned_link(link: Path, root: Path) -> bool:
    if not link.is_symlink():
        return False
    # Do not resolve the target: deleted sources leave dangling links that still
    # belong to us. Only immediate children of this lodge's source root count.
    target = Path(os.path.abspath(link.parent / os.readlink(link)))
    return target.parent == root.resolve() and target.name == link.name


def _sync_directory(directory: Path, root: Path, sources: dict[str, Path]) -> None:
    from skills_sync import _exclusive

    with _exclusive(directory) as locked:
        if not locked:
            return
        for link in directory.glob("lodge-*"):
            if _owned_link(link, root) and link.name not in sources:
                link.unlink()
        for name, source in sources.items():
            link = directory / name
            if link.exists() or link.is_symlink():
                if not _owned_link(link, root):
                    _warn(f"{link} is wolt-owned — keeping its override")
                continue
            try:
                link.symlink_to(os.path.relpath(source.resolve(), directory), target_is_directory=True)
            except FileExistsError:
                # An owner may install an override concurrently; never replace it.
                _warn(f"{link} appeared during delivery — leaving it alone")


def sync_lodge_skills(wolts_dir: Path, wolt_dir: Path) -> None:
    """Refresh links only; never copy, download or modify shared skill contents.

    Missing sources remove only our own links. Real directories and unrelated
    symlinks are per-wolt overrides. Codex normally uses the existing .agents
    bridge; if its directory is user-owned, deliver there separately too.
    """
    root = shared_skills_dir(wolts_dir)
    try:
        root.mkdir(parents=True, exist_ok=True)
        sources = _sources(root)
        claude = Path(wolt_dir) / ".claude" / "skills"
        if not sources and not claude.is_dir():
            return
        _sync_directory(claude, root, sources)
        from skills_sync import ensure_agent_bridges
        ensure_agent_bridges(wolt_dir)
        agents = Path(wolt_dir) / ".agents" / "skills"
        if agents.is_dir() and agents.resolve() != claude.resolve():
            _sync_directory(agents, root, sources)
    except OSError as exc:
        _warn(f"could not refresh {wolt_dir}: {exc}")
