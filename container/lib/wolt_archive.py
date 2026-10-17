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
import shutil
import stat
import sys
import threading
import time
from pathlib import Path

from execution_policy import AutoGrantStore
from paths import space_wolf_dir
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


def _unlinked(path: Path, wolts_dir: Path) -> bool:
    """No symlink on the path below the wolts folder, so it can't lead out of it.

    Both sides resolved: a lodge whose whole wolts folder is a link (on another
    disk) still passes.
    """
    return os.path.realpath(path) == os.path.join(os.path.realpath(wolts_dir), path.relative_to(wolts_dir))


def _no_link(path: Path, wolts_dir: Path) -> Path:
    if not _unlinked(path, wolts_dir):
        raise ArchiveError(409, f"wolts/{path.relative_to(wolts_dir)} leads through a symlink; "
                                "archive, unarchive and delete never follow one")
    return path


def _archive_root(wolts_dir: Path) -> Path:
    """wolts/.archive, refused when it is a symlink: a move or delete there would follow it out."""
    return _no_link(archive_dir(wolts_dir), wolts_dir)


def is_archived(name: str, *, wolts_dir: Path) -> bool:
    return bool(_NAME_RE.fullmatch(name or "")) and (
        archive_dir(wolts_dir) / name / "wolt" / "wolt.json").is_file()


def in_archive(name: str, *, wolts_dir: Path) -> bool:
    """A real folder holds the name in the archive, whole or half-deleted."""
    folder = archive_dir(wolts_dir) / name
    return bool(_NAME_RE.fullmatch(name or "")) and folder.is_dir() and not folder.is_symlink()


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
    with _LOCK:
        target = _archive_root(wolts_dir) / name
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
    try:
        root = _archive_root(Path(wolts_dir))
    except ArchiveError:
        return []
    wolts = []
    for wolt_json in sorted(root.glob("*/wolt/wolt.json")):
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


def _ghost_entry(path: Path) -> bool:
    """What a ghost may hold: folders, and the empty <s>.lock a session lock leaves."""
    if path.is_symlink():
        return False
    return path.is_dir() or (path.suffix == ".lock" and path.is_file()
                             and path.stat().st_size == 0)


def _clear_ghost(home: Path) -> None:
    """Remove a wolts/<name> left holding only folders and empty *.lock files.

    A registry lookup creates the sessions folder and a session lock leaves
    <s>.lock, so a message racing the move can leave one behind. It must not
    block the wolt coming back. Anything else in it is a 409.
    """
    if home.is_symlink() or not home.is_dir():
        raise ArchiveError(409, f"wolts/{home.name} already exists")
    entries = list(home.rglob("*"))
    if not all(_ghost_entry(p) for p in entries):
        raise ArchiveError(409, f"wolts/{home.name} already exists")
    try:
        for path in sorted(entries, key=lambda p: len(p.parts), reverse=True) + [home]:
            path.rmdir() if path.is_dir() else path.unlink()
    except OSError as exc:
        raise ArchiveError(409, f"{home} is in the way: {exc}")


def unarchive_wolt(name: str, *, wolts_dir: Path) -> dict:
    check_wolt_name(name)
    wolts_dir = Path(wolts_dir)
    home = wolts_dir / name
    with _LOCK:
        source = _no_link(_archive_root(wolts_dir) / name, wolts_dir)
        if not is_archived(name, wolts_dir=wolts_dir):
            raise ArchiveError(404, f"no archived wolt named {name}")
        if home.exists() or home.is_symlink():
            _clear_ghost(home)
        try:
            os.rename(source, home)
        except OSError as exc:
            raise ArchiveError(500, f"could not move {name} back: {exc}")
    return {"unarchived": name}


def _add_rwx(path) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IRWXU)
    except OSError:
        pass


def _open_up(folder: Path) -> None:
    """Make every real folder in the tree u+rwx, top-down, never through a symlink.

    A folder with no read permission (a Go module cache, an unpacked tarball,
    a test that chmods one) can't be listed, so rmtree can't empty it.
    """
    _add_rwx(folder)
    for root, dirs, _files in os.walk(folder):
        for name in dirs:
            path = os.path.join(root, name)
            if not os.path.islink(path):
                _add_rwx(path)


def _retry_removal(func, path, exc):
    """rmtree's error handler: retry a removal once, its folder made writable.

    Anything else (an open, a scandir) re-raises the original error.
    """
    if func not in (os.unlink, os.rmdir, os.remove):
        raise exc[1] if isinstance(exc, tuple) else exc  # onerror passes exc_info
    _add_rwx(os.path.dirname(path))
    func(path)


_ON_ERROR = "onexc" if sys.version_info >= (3, 12) else "onerror"


def delete_archived_wolt(name: str, *, wolts_dir: Path) -> dict:
    check_wolt_name(name)
    wolts_dir = Path(wolts_dir)
    with _LOCK:
        target = _archive_root(wolts_dir) / name
        # Only from the archive: a live wolt is archived first. No wolt.json
        # needed, so a delete that failed halfway can run again.
        if not in_archive(name, wolts_dir=wolts_dir):
            raise ArchiveError(404, f"no archived wolt named {name}")
        # Grants and cron stamps are keyed by name: a later wolt with this
        # name must not inherit them. They go first, so a delete that fails
        # halfway never leaves a grant behind.
        revoked = AutoGrantStore(wolts_dir).revoke_wolt(name)
        shutil.rmtree(space_wolf_dir(wolts_dir) / name, ignore_errors=True)
        _open_up(target)
        try:
            shutil.rmtree(target, **{_ON_ERROR: _retry_removal})
        except OSError as exc:
            raise ArchiveError(500, f"could not delete all of {name} ({exc}); what is left "
                                    f"stays in {ARCHIVE_DIRNAME}, run delete again to retry")
    return {"deleted": name, "grants_revoked": revoked}
