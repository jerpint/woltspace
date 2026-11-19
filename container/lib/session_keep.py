"""How long engines keep their own sessions: woltspace.json "sessions.keep_days".

Unset, woltspace changes nothing. null means forever, a positive whole number
that many days. Of the engines, Claude Code deletes its own sessions (after
cleanupPeriodDays, 30 unset); Codex has no delete of its own; opencode, Pi and
Hermes are not measured. Claude wolts get cleanupPeriodDays in their own
settings, merged, never over a value someone set, and never 0, which Claude
reads as "keep no transcripts". Natively woltspace never edits ~/.claude.
"""

from __future__ import annotations

import json
from pathlib import Path

from settings_files import settings_lock, write_json_atomic

FOREVER_DAYS = 3650
_UNSET = object()


def _raw(wolts_dir):
    try:
        sessions = json.loads((Path(wolts_dir) / "woltspace.json").read_text()).get("sessions", {})
    except (OSError, ValueError, AttributeError):
        return _UNSET
    return sessions.get("keep_days", _UNSET) if isinstance(sessions, dict) else _UNSET


def keep_days(wolts_dir) -> int | None:
    """The days to ask engines for, or None: leave them alone (unset or invalid)."""
    value = _raw(wolts_dir)
    if value is None:
        return FOREVER_DAYS
    return value if type(value) is int and value > 0 else None


def setting(wolts_dir) -> str:
    value = _raw(wolts_dir)
    if value is _UNSET:
        return "unset (each engine keeps its own default)"
    days = keep_days(wolts_dir)
    return f"invalid: {value!r}" if days is None else "forever" if value is None else f"{days} days"


def merge_claude_settings(claude_dir: Path, days: int) -> str:
    """Add cleanupPeriodDays unless one is there: "set", "kept", or "refused" (an
    unreadable or non-object file is left as it is). Holds the file's settings
    lock from the read to the write."""
    path = Path(claude_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with settings_lock(path):
        settings = {}
        if path.exists():
            try:
                settings = json.loads(path.read_text())
            except (OSError, ValueError):
                return "refused"
            if not isinstance(settings, dict):
                return "refused"
        if "cleanupPeriodDays" in settings:
            return "kept"
        settings["cleanupPeriodDays"] = days
        write_json_atomic(path, settings)
    return "set"


def apply_to_wolts(wolts_dir) -> dict[str, str]:
    """The container boot sweep: {wolt: outcome} for every wolt, {} when unset."""
    days = keep_days(wolts_dir)
    if days is None:
        return {}
    return {wolt.name: merge_claude_settings(wolt / ".claude", days)
            for wolt in sorted(Path(wolts_dir).iterdir())
            if wolt.is_dir() and not wolt.name.startswith(".") and (wolt / "wolt" / "wolt.json").is_file()}
