"""
Wolt site management — each wolt gets a static site served directly by FastAPI.

Sites live at wolts/{wolt}/wolt/site/ and are served at /wolt/{wolt}/site/.
Livereload is handled by the server's own watchfiles WebSocket — there is no
per-site server process and no port to manage.

Usage:
    from sites import site_dir, ensure_site
"""

from __future__ import annotations

import os
from pathlib import Path

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))


def site_dir(wolt_name: str) -> Path:
    """Get the site directory for a wolt."""
    return WOLTS_DIR / wolt_name / "wolt" / "site"


def ensure_site(wolt_name: str) -> Path:
    """Make sure a wolt's site exists, scaffolding the starter site if needed.

    Idempotent — returns the site directory.
    """
    sdir = site_dir(wolt_name)
    if not (sdir / "index.html").exists():
        sdir.mkdir(parents=True, exist_ok=True)
        _write_default_index(wolt_name, sdir)
    return sdir


def _write_default_index(wolt_name: str, sdir: Path) -> None:
    """Scaffold the starter site for a wolt that doesn't have one yet."""
    from wolts import scaffold_starter_site, _get_wolt_type
    creature_type = _get_wolt_type(wolt_name)
    scaffold_starter_site(sdir, wolt_name, creature_type)
