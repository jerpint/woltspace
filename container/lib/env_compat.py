"""The WOLTSPACE_* env namespace, for the flat container runtime tree.

Mirror of `src/woltspace/envvars.py` — see the shared block below for why the
copy exists rather than an import. Same API, same semantics:

    from env_compat import get_env, export_both
    wolts_dir = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))

The inventory of every variable lives in `docs/environment.md`.
"""

from __future__ import annotations

import os
import sys


# --- BEGIN SHARED BLOCK ---
# Everything between these markers is mirrored byte-for-byte between
# `src/woltspace/envvars.py` and `container/lib/env_compat.py`. The two trees
# cannot import each other: `container/lib` is a flat, stdlib-only PYTHONPATH
# tree, and `src/woltspace/layout.py` is what *puts* it on the path, so the
# package cannot depend on it at import time. `test_env_namespace.py` asserts
# the two copies never drift. Edit one, copy it to the other.

#: canonical name -> the pre-namespace name still honoured for it.
LEGACY_ALIASES = {
    "WOLTSPACE_WOLTS_DIR": "WOLTS_DIR",
    "WOLTSPACE_WOLT_DIR": "WOLT_DIR",
    "WOLTSPACE_WOLT_NAME": "WOLT_NAME",
    "WOLTSPACE_WOLT_SESSION": "WOLT_SESSION",
}

#: the legacy name -> the canonical name that replaced it.
CANONICAL_NAMES = {legacy: name for name, legacy in LEGACY_ALIASES.items()}

#: Legacy names read through `get_env` in this process. Unioned with whatever
#: the environment scan finds, so the warning covers both.
_legacy_reads: set = set()
_warned = False


def get_env(name, default=None, *, legacy=None, env=None):
    """Read `name`, falling back to its pre-namespace spelling.

    The canonical name wins whenever it is set to a non-empty value; the legacy
    name is only consulted after that. `legacy` defaults to the alias table, so
    `get_env("WOLTSPACE_WOLTS_DIR")` already honours `WOLTS_DIR` — pass it
    explicitly only for a pair the table does not know.
    """
    values = os.environ if env is None else env
    legacy_name = LEGACY_ALIASES.get(name) if legacy is None else legacy
    value = values.get(name)
    if value not in (None, ""):
        return value
    if legacy_name:
        legacy_value = values.get(legacy_name)
        if legacy_value not in (None, ""):
            _legacy_reads.add(legacy_name)
            _warn_legacy_once(env=values)
            return legacy_value
    return default


def export_both(values):
    """Fill in the other spelling of every pair present in a child environment.

    Whatever exports env to a child — a session spawn, a connector, the control
    plane, the container entrypoint — passes its mapping through here. Wolt
    skills and tools written against `$WOLT_NAME` keep working while the
    canonical names become the ones the platform itself reads.

    Whichever spelling the caller set wins; the canonical name wins when both
    are given, matching `get_env`.
    """
    merged = dict(values)
    for name, legacy in LEGACY_ALIASES.items():
        canonical_value = merged.get(name)
        legacy_value = merged.get(legacy)
        if canonical_value not in (None, ""):
            merged[name] = canonical_value
            merged[legacy] = canonical_value
        elif legacy_value not in (None, ""):
            merged[name] = legacy_value
            merged[legacy] = legacy_value
    return merged


def legacy_in_use(env=None):
    """The legacy names this process is actually relying on.

    A legacy name that sits beside its canonical twin is not in use — the
    canonical one is what gets read — so it is not reported. Union with the
    names `get_env` has already fallen back to, for a caller that passed an
    explicit `legacy=` outside the table.
    """
    values = os.environ if env is None else env
    relied_on = {
        legacy
        for name, legacy in LEGACY_ALIASES.items()
        if (values.get(legacy) or "").strip() and not (values.get(name) or "").strip()
    }
    return sorted(relied_on | _legacy_reads)


def warn_legacy_once(env=None, stream=None):
    """Emit at most one deprecation line per process. Returns what it named.

    Called automatically the first time `get_env` falls back, and again at the
    top of each entrypoint so the line lands early rather than from inside
    whichever module happened to import first. Idempotent either way — a read
    is not an event worth logging, a process is.
    """
    return _warn_legacy_once(env=env, stream=stream)


def _warn_legacy_once(env=None, stream=None):
    global _warned
    if _warned:
        return []
    names = legacy_in_use(env)
    if not names:
        # Nothing to warn about *yet* — a later fallback still gets its line.
        return []
    _warned = True
    out = sys.stderr if stream is None else stream
    for legacy in names:
        print(
            f"deprecated env var {legacy} — use {CANONICAL_NAMES.get(legacy, legacy)}"
            " (legacy names honored until 1.0)",
            file=out,
        )
    return names


def _reset_warning_state():
    """Test seam: forget that this process has already warned."""
    global _warned
    _warned = False
    _legacy_reads.clear()
# --- END SHARED BLOCK ---
