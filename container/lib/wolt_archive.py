"""Archive a wolt, bring it back, or delete an archived one for good.

Archiving moves wolts/<name> to wolts/.archive/<name>. Every scan of the wolts
folder already skips it: the glob scans look one level deep for
*/wolt/wolt.json, and the directory listings skip dot names. So the move takes
the wolt out of every list, cron pass and sweep at once, and moving it back
brings all of it back. A wolt's session records live inside its own folder and
move with it. Only the lodge calls this; the page and the CLI call its routes.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from session_expiry import hold_active
from session_runtime import RuntimeHandle, get_runtime
from sessions import SessionRegistry, claim_resume_id, stamp_resume_id, stored_resume_id

ARCHIVE_DIRNAME = ".archive"

# Older wolts can have capitals or underscores, so this is wider than the slug
# rule new wolts get. No dot, no slash: a name is one plain folder.
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")

# Archive, unarchive and delete run one at a time.
_LOCK = threading.Lock()


class ArchiveError(Exception):
    """A refusal the routes answer with `status`, `message` and `extra`."""

    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def check_wolt_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ArchiveError(400, f"not a wolt name: {name!r}")
    return name


def archive_dir(wolts_dir: Path) -> Path:
    return Path(wolts_dir) / ARCHIVE_DIRNAME


def is_archived(name: str, *, wolts_dir: Path) -> bool:
    return bool(_NAME_RE.fullmatch(name or "")) and (
        archive_dir(wolts_dir) / name / "wolt" / "wolt.json").is_file()


def _held(record: dict) -> dict:
    return {"session": record["name"], "reason": (record.get("hold") or {}).get("reason")}


def _end_session(registry: SessionRegistry, name: str, wolt: str, now: int, *,
                 sweep: bool = False) -> bool:
    """End one session's tmux, resting it when it has a conversation to resume.

    The fields the /rest route writes: after unarchive, a message by session or
    by wolt name wakes it, and those routes take only running or resting.
    True when its tmux was ended. The sweep after the move neither recovers an
    id (the archive's registry knows only archived sessions) nor refuses a
    hold (the wolt has already gone).
    """
    data = registry.get(name, wolt=wolt, check_alive=False)
    if data is None:
        return False
    # Recover a missing id before the session lock, never inside it: the
    # recovery lock is always taken first (see _rest_session_locked).
    if not sweep and data.get("status") == "running" and not stored_resume_id(data):
        claim_resume_id(registry, data, lambda d: stamp_resume_id(
            registry, wolt, name, d["harness_session_id"]))
    with registry._lock(wolt, name):
        data = registry.get(name, wolt=wolt, check_alive=False)
        handle = RuntimeHandle.from_record(data or {})
        if data is None or not get_runtime().is_alive(handle):
            return False
        # The hold route takes this lock too: a hold may have come since the check.
        if not sweep and hold_active(data, now):
            raise ArchiveError(409, f"{wolt} has a session that holds", held=[_held(data)])
        if not get_runtime().stop(handle) and get_runtime().is_alive(handle):
            raise ArchiveError(500, f"session {name} could not be stopped")
        if data.get("status") in ("running", "resting") and stored_resume_id(data):
            data.update(status="resting", rested_at=now, rest_reason="archived")
        else:
            data.update(status="stopped", finished_at=now)
        registry._write(wolt, name, data)
    return True


def _end_live(registry: SessionRegistry, name: str, now: int, *, sweep: bool = False) -> list[str]:
    """End every session of the wolt whose tmux is alive, by its exact name."""
    return [r["name"] for r in registry.list(wolt=name)
            if r.get("tmux_alive") and _end_session(registry, r["name"], name, now, sweep=sweep)]


def archive_wolt(name: str, *, wolts_dir: Path, now: int | None = None) -> dict:
    check_wolt_name(name)
    wolts_dir = Path(wolts_dir)
    home = wolts_dir / name
    target = archive_dir(wolts_dir) / name
    with _LOCK:
        # Before any registry call: a lookup creates the wolt's sessions folder.
        if not (home / "wolt" / "wolt.json").is_file():
            raise ArchiveError(404, f"no wolt named {name}")
        if target.exists() or target.is_symlink():
            raise ArchiveError(409, f"an archived wolt is already named {name}")
        now = int(time.time()) if now is None else now
        registry = SessionRegistry(wolts_dir)
        held = [_held(r) for r in registry.list(wolt=name) if hold_active(r, now)]
        if held:
            raise ArchiveError(409, f"{name} has a session that holds", held=held)
        stopped = _end_live(registry, name, now)
        target.parent.mkdir(exist_ok=True)
        try:
            os.rename(home, target)
        except OSError as exc:
            raise ArchiveError(500, f"could not move {name} into {ARCHIVE_DIRNAME}: {exc}")
        # A message can wake a session, or a new one start, between the stop
        # pass and the move. The registry can't see .archive, so read there.
        try:
            swept = _end_live(SessionRegistry(target.parent), name, now, sweep=True)
        except ArchiveError as exc:
            raise ArchiveError(500, f"archived {name}, but {exc.message}")
    return {"archived": name, "stopped": sorted(set(stopped + swept))}


def list_archived_wolts(*, wolts_dir: Path) -> list[dict]:
    wolts = []
    for wolt_json in sorted(archive_dir(wolts_dir).glob("*/wolt/wolt.json")):
        name = wolt_json.parent.parent.name
        if not _NAME_RE.fullmatch(name):
            continue
        try:
            config = json.loads(wolt_json.read_text())
        except (json.JSONDecodeError, OSError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        # An unreadable wolt.json still lists, so it can be unarchived or deleted.
        wolts.append({"name": name, "display_name": config.get("display_name") or None,
                      "type": config.get("type") or "rodent"})
    return wolts
