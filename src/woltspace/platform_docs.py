"""Synchronize platform-managed instructions into each wolt."""

from __future__ import annotations

import sys
from pathlib import Path


PLATFORM_SECTION_START = "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->"
PLATFORM_SECTION_END = "<!-- WOLTSPACE:END -->"


def sync_claude_md_platform_section(wolts_dir: Path, install_root: Path) -> None:
    """Regenerate the platform section at the top of every wolt's CLAUDE.md.

    Preserve everything after the WOLTSPACE:END marker. If no complete marker
    pair exists, prepend the platform section to the existing content.
    """
    lib_dir = str(install_root / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    try:
        from wolts import _platform_claude_md_section
    except ImportError:
        return  # wolts.py is not available yet on a first-ever container boot

    platform_block = _platform_claude_md_section()
    for wolt in sorted(wolts_dir.iterdir()):
        if not wolt.is_dir() or wolt.name.startswith("."):
            continue
        claude_md = wolt / "CLAUDE.md"
        if not claude_md.exists():
            continue

        content = claude_md.read_text()
        if PLATFORM_SECTION_START in content and PLATFORM_SECTION_END in content:
            before = content[:content.index(PLATFORM_SECTION_START)]
            after = content[
                content.index(PLATFORM_SECTION_END) + len(PLATFORM_SECTION_END):
            ].lstrip("\n")
            new_content = before + platform_block + "\n" + after
        else:
            new_content = platform_block + "\n" + content

        if new_content != content:
            claude_md.write_text(new_content)
