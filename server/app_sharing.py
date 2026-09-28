"""Lodge-owned per-app share lists."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN_RE = re.compile(r"^@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")
_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()
_CACHE: dict[Path, tuple[tuple[int, int] | None, dict]] = {}


def _path(wolts_dir: Path) -> Path:
    return wolts_dir / ".space" / "platform" / "app-sharing.json"


def normalize_entry(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("share entries must be email addresses or @domains")
    entry = value.strip().lower()
    if entry.startswith("@"):
        domain = entry[1:]
        if "." not in domain or ".." in domain or not _DOMAIN_RE.fullmatch(entry):
            raise ValueError(f"invalid share domain: {value}")
    elif not _EMAIL_RE.fullmatch(entry):
        raise ValueError(f"invalid share email: {value}")
    return entry


def read_app_shares(wolts_dir: Path, app_name: str) -> list[str]:
    path = _path(wolts_dir)
    data = _read_state(path)
    entries = data.get(app_name, [])
    if not isinstance(entries, list):
        raise RuntimeError("app sharing state is unreadable")
    return sorted({normalize_entry(entry) for entry in entries})


def _read_state(path: Path) -> dict:
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    with _CACHE_LOCK:
        cached = _CACHE.get(path)
        if cached and cached[0] == signature:
            return cached[1]
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("app sharing state is unreadable") from exc
    if not isinstance(data, dict):
        raise RuntimeError("app sharing state is unreadable")
    with _CACHE_LOCK:
        _CACHE[path] = (signature, data)
    return data


def write_app_shares(wolts_dir: Path, app_name: str, values: object) -> list[str]:
    if not isinstance(values, list):
        raise ValueError("entries must be a list")
    entries = sorted({normalize_entry(entry) for entry in values})
    path = _path(wolts_dir)
    with _LOCK:
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("app sharing state is unreadable") from exc
        if not isinstance(data, dict):
            raise RuntimeError("app sharing state is unreadable")
        if entries:
            data[app_name] = entries
        else:
            data.pop(app_name, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            pass
        os.replace(tmp, path)
        with _CACHE_LOCK:
            _CACHE.pop(path, None)
    return entries


def email_is_shared(email: str, entries: list[str]) -> bool:
    normalized = email.strip().lower()
    domain = normalized.partition("@")[2]
    return normalized in entries or (bool(domain) and f"@{domain}" in entries)
