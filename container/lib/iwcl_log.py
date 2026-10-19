"""One lodge-wide log of wolt-to-wolt traffic: `.space/logs/iwcl.jsonl`.

One JSON line per message attempt or wolt-started spawn, written after the
outcome is known. Wolt-to-wolt traffic comes from the control plane; a
human's Telegram message delivered to a wolt session (pasted, or in a new
session's opening prompt) from the adapter, with `from_human` and `via` set. Writers may be separate processes, so each line is written
under an exclusive flock. Readers tail the file: a line is complete once it ends in a
newline, and at ROTATE_BYTES the file is renamed to `iwcl.jsonl.1` (one
generation kept), so a follower reopens on rename.

`WOLTSPACE_IWCL_LOG` picks what is written: `full` (default), `meta` (no
message text) or `off`. Sender fields are what the sender claimed; nothing
here authenticates them. Full text is sensitive: the file is kept 0600.
Logging never fails a delivery.
"""

import fcntl
import json
import os
import threading
import time
import uuid
from pathlib import Path

VERSION = 1
ROTATE_BYTES = 20 * 1024 * 1024
MODES = ("full", "meta", "off")

_lock = threading.Lock()


def log_mode() -> str:
    mode = os.environ.get("WOLTSPACE_IWCL_LOG", "full").strip().lower()
    return mode if mode in MODES else "full"


def log_path(wolts_dir: Path) -> Path:
    return Path(wolts_dir) / ".space" / "logs" / "iwcl.jsonl"


def record(wolts_dir: Path, event: str, **fields) -> None:
    """Append one line; never raises (logging never fails a delivery)."""
    try:
        _record(wolts_dir, event, **fields)
    except Exception as exc:
        print(f"[iwcl-log] not written: {exc}")


def _record(wolts_dir: Path, event: str, *, from_wolt: str = "", from_session: str = "",
           to_wolt: str = "", to_session: str = "", text: str = "", status: str = "",
           error: str = "", from_human: str = "", via: str = "") -> None:
    mode = log_mode()
    if mode == "off":
        return
    line = {
        "v": VERSION,
        "ts": round(time.time(), 3),
        "id": uuid.uuid4().hex,
        "event": event,
        "from_wolt": from_wolt or "",
        "from_session": from_session or "",
        "from_human": from_human or "",
        "via": via or "",
        "claimed": True,
        "to_wolt": to_wolt or "",
        "to_session": to_session or "",
        "status": status,
    }
    if mode == "full":
        line["text"] = text or ""
    if error:
        line["error"] = str(error)
    data = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    path = log_path(wolts_dir)
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(path.with_name(path.name + ".lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                if path.stat().st_size + len(data) > ROTATE_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:  # a short write must not leave half a line
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
        finally:
            os.close(lock_fd)
