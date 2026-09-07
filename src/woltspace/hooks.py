"""Retire the woltspace claude hooks from existing wolts, natively."""

from __future__ import annotations

import sys

from .layout import RuntimeLayout


def normalize_platform_hooks(layout: RuntimeLayout) -> None:
    lib_dir = str(layout.install_root / "container" / "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    from hooks_normalize import normalize_all_wolt_hooks

    normalize_all_wolt_hooks(layout.wolts_dir)
