"""Platform skill sync for the native control plane."""

from __future__ import annotations

import sys

from .layout import RuntimeLayout


def sync_platform_skills(layout: RuntimeLayout) -> None:
    lib_dir = str(layout.install_root / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from skills_sync import sync_all_wolt_skills

    sync_all_wolt_skills(layout.install_root, layout.wolts_dir)
