"""One lodge-wide log of wolt-to-wolt traffic: `.space/logs/iwcl.jsonl`.

One JSON line per message attempt or wolt-started spawn, written after the
outcome is known. Readers tail the file: a line is complete once it ends in a
newline, and at ROTATE_BYTES the file is renamed to `iwcl.jsonl.1` (one
generation kept), so a follower reopens on rename.

`WOLTSPACE_IWCL_LOG` picks what is written: `full` (default), `meta` (no
message text) or `off`. Sender fields are what the sender claimed; nothing
here authenticates them. Logging never fails a delivery.
"""

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


def record(wolts_dir: Path, event: str, *, from_wolt: str = "", from_session: str = "",
           to_wolt: str = "", to_session: str = "", text: str = "", status: str = "",
           error: str = "") -> None:
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
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.stat().st_size + len(data) > ROTATE_BYTES:
                    os.replace(path, path.with_name(path.name + ".1"))
            except FileNotFoundError:
                pass
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
    except Exception as exc:  # never fail the delivery over the log
        print(f"[iwcl-log] not written: {exc}")
