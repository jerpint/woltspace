"""Private, durable handoff from ``notify`` to the E2EE Matrix client.

The API process cannot encrypt a Matrix event: the connector owns the device
and its crypto store.  A tiny local outbox keeps that ownership honest.  Bodies
are plaintext only on the lodge owner's disk, mode 0600, until the connector
confirms the send and removes the entry.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path


def validate_room_id(room_id: object) -> str:
    value = str(room_id or "")
    if (
        not value.startswith("!")
        or ":" not in value[1:]
        or len(value) > 512
        or any(char in value for char in ("/", "\\", "\0", "\n", "\r"))
    ):
        raise ValueError("invalid Matrix room ID")
    return value


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def enqueue(outbox: Path, *, room_id: object, message: object, session: object = "") -> Path:
    room = validate_room_id(room_id)
    body = str(message or "")
    if not body:
        raise ValueError("Matrix message must not be empty")
    ensure_private_dir(outbox)
    name = f"{time.time_ns()}-{secrets.token_hex(8)}.json"
    temporary = outbox / f".{name}.partial"
    destination = outbox / name
    payload = json.dumps({
        "v": 1,
        "room_id": room,
        "message": body,
        "session": str(session or ""),
        "created_at": int(time.time()),
    }, separators=(",", ":"))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def pending(outbox: Path) -> list[Path]:
    try:
        return sorted(path for path in outbox.glob("*.json") if path.is_file())
    except OSError:
        return []


def read_entry(path: Path) -> dict:
    payload = json.loads(path.read_text())
    if payload.get("v") != 1:
        raise ValueError("unsupported Matrix outbox entry")
    payload["room_id"] = validate_room_id(payload.get("room_id"))
    if not isinstance(payload.get("message"), str) or not payload["message"]:
        raise ValueError("invalid Matrix outbox message")
    return payload
