"""A durable record of every content host the gateway has ever served.

A browser origin outlives the thing that was served on it: storage, cookies
and an installed service worker stay with the host name after an app is
renamed or removed. Refusing new `Service-Worker` requests does not evict a
worker that is already installed. So a host that once served an app must not
later serve a wolt's site (or the other way round) as if it were clean.

The record at `.space/platform/content-origins.json` lists each host name the
gateway served, by kind, from the moment this module exists:

    {"notes.owner.woltspace.app": {"app": "<first served, UTC>"},
     "astre.sites.localhost":     {"site": "<first served, UTC>"}}

A host appears here because it really answered a request (identity and ACL
passed). Nothing before this record exists is known, which is why remote site
hosts stay gated until the policy that reads this record ships: a previously
used origin stays reserved for good, and a migration gets a fresh host.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

KINDS = {"app", "site"}
_LOCK = threading.Lock()
# (path, host, kind) already written by this process: one write per host.
_SEEN: set[tuple[Path, str, str]] = set()


def _path(wolts_dir: Path) -> Path:
    return wolts_dir / ".space" / "platform" / "content-origins.json"


def read_origins(wolts_dir: Path) -> dict:
    """The record, or an empty mapping when there is none.

    An unreadable record raises: nothing that relies on it may treat a host as
    clean when the history cannot be read.
    """
    path = _path(wolts_dir)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("content origin record is unreadable") from exc
    if not isinstance(data, dict):
        raise RuntimeError("content origin record is unreadable")
    return data


def host_kinds(wolts_dir: Path, host: str) -> set[str]:
    """Every kind this host name ever served (raises when the record is unreadable)."""
    entry = read_origins(wolts_dir).get(host.lower().rstrip("."), {})
    return {kind for kind in entry if kind in KINDS} if isinstance(entry, dict) else set()


def record_served(wolts_dir: Path, host: str, kind: str) -> None:
    """Note that `host` answered as `kind`. Idempotent; never raises.

    A failed write must not take the request down with it, but it is written
    before the response goes out, so a served host is in the record unless
    the platform directory itself is unwritable.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown content kind: {kind}")
    host = host.lower().rstrip(".")
    path = _path(wolts_dir)
    key = (path, host, kind)
    if key in _SEEN:
        return
    with _LOCK:
        if key in _SEEN:
            return
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (OSError, json.JSONDecodeError):
            # A broken record is left exactly as it is: whatever reads it
            # must keep failing closed, and the owner must get to see it.
            return
        if not isinstance(data, dict):
            return
        entry = data.get(host)
        if not isinstance(entry, dict):
            entry = {}
        if kind not in entry:
            entry[kind] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data[host] = entry
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError:
            return
        _SEEN.add(key)
