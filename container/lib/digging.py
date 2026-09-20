"""Destination-owned authorization state for experimental cross-colony digs.

Wire authenticates and encrypts messages.  This module deliberately does not:
it consumes the peer identity already verified by a transport adapter and owns
the separate Woltspace decision about whether that peer may start one guest
session.

The first proof is intentionally only an authorization kernel.  It neither
opens a network listener nor starts SSH.  A later session adapter may consume
an approved grant exactly once and map its target into a disposable workspace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


DIG_VERSION = "woltspace-dig/v0"
MAX_LIFETIME_SECONDS = 15 * 60
_ID_BYTES = 16
_CAP_BYTES = 32
_STATES = frozenset({"pending", "approved", "active", "completed", "revoked", "expired"})


class DigError(ValueError):
    """An invalid request or lifecycle transition."""


@dataclass(frozen=True)
class DigRequest:
    version: str
    request_id: str
    source_colony: str
    source_wolt: str
    task: str
    target: str
    created_at: int
    expires_at: int

    @classmethod
    def create(
        cls,
        *,
        source_colony: str,
        source_wolt: str,
        task: str,
        target: str,
        now: int | None = None,
        lifetime_seconds: int = 10 * 60,
    ) -> "DigRequest":
        now = int(time.time()) if now is None else int(now)
        if not 1 <= lifetime_seconds <= MAX_LIFETIME_SECONDS:
            raise DigError("dig request lifetime must be between 1 and 900 seconds")
        values = {
            "source_colony": source_colony,
            "source_wolt": source_wolt,
            "task": task,
            "target": target,
        }
        for name, value in values.items():
            if not isinstance(value, str) or not value.strip():
                raise DigError(f"{name} must be a non-empty string")
        if len(task.encode("utf-8")) > 500:
            raise DigError("task exceeds 500 UTF-8 bytes")
        if target.startswith("/") or ".." in Path(target).parts:
            raise DigError("target must be a destination-relative path")
        return cls(
            version=DIG_VERSION,
            request_id=secrets.token_hex(_ID_BYTES),
            source_colony=source_colony,
            source_wolt=source_wolt,
            task=task,
            target=target,
            created_at=now,
            expires_at=now + lifetime_seconds,
        )

    @classmethod
    def from_dict(cls, value: dict) -> "DigRequest":
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise DigError("dig request has unknown or missing fields")
        request = cls(**value)
        if request.version != DIG_VERSION:
            raise DigError("unsupported dig request version")
        for name in ("request_id", "source_colony", "source_wolt", "task", "target"):
            item = getattr(request, name)
            if not isinstance(item, str) or not item.strip():
                raise DigError(f"{name} must be a non-empty string")
        if len(request.request_id) != _ID_BYTES * 2 or any(
            c not in "0123456789abcdef" for c in request.request_id
        ):
            raise DigError("invalid dig request id")
        if len(request.task.encode("utf-8")) > 500:
            raise DigError("task exceeds 500 UTF-8 bytes")
        if request.target.startswith("/") or ".." in Path(request.target).parts:
            raise DigError("target must be a destination-relative path")
        if type(request.created_at) is not int or type(request.expires_at) is not int:
            raise DigError("dig request times must be integers")
        if request.expires_at <= request.created_at:
            raise DigError("dig request expiry must follow creation")
        if request.expires_at - request.created_at > MAX_LIFETIME_SECONDS:
            raise DigError("dig request lifetime exceeds 900 seconds")
        return request

    def to_dict(self) -> dict:
        return asdict(self)


class DigStore:
    """Private destination-side store for pending requests and one-use grants."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _path(self, request_id: str) -> Path:
        if len(request_id) != _ID_BYTES * 2 or any(c not in "0123456789abcdef" for c in request_id):
            raise DigError("invalid dig request id")
        return self.root / f"{request_id}.json"

    @contextmanager
    def _locked(self, request_id: str):
        lock_path = self.root / f".{request_id}.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self._path(request_id)
        finally:
            os.close(fd)

    @staticmethod
    def _cap_digest(capability: str) -> str:
        return hashlib.sha256(capability.encode("ascii")).hexdigest()

    def _write_new(self, path: Path, record: dict) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)

    def _replace(self, path: Path, record: dict) -> None:
        temp = path.with_suffix(f".{secrets.token_hex(8)}.tmp")
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def receive(self, request: DigRequest, *, verified_peer: str, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        if verified_peer != request.source_colony:
            raise DigError("Wire peer does not match dig request source")
        if now >= request.expires_at:
            raise DigError("dig request is expired")
        record = {"request": request.to_dict(), "state": "pending", "received_at": now}
        self._write_new(self._path(request.request_id), record)
        return self.status(request.request_id, now=now)

    def approve(self, request_id: str, *, now: int | None = None, lifetime_seconds: int = 5 * 60) -> str:
        now = int(time.time()) if now is None else int(now)
        if not 1 <= lifetime_seconds <= MAX_LIFETIME_SECONDS:
            raise DigError("dig grant lifetime must be between 1 and 900 seconds")
        with self._locked(request_id) as path:
            record = self._load(path)
            self._expire(record, now)
            if record["state"] != "pending":
                raise DigError(f"cannot approve dig in state {record['state']}")
            capability = secrets.token_urlsafe(_CAP_BYTES)
            record.update(
                state="approved",
                approved_at=now,
                grant_expires_at=min(now + lifetime_seconds, record["request"]["expires_at"]),
                capability_sha256=self._cap_digest(capability),
            )
            self._replace(path, record)
            return capability

    def consume(self, request_id: str, capability: str, *, verified_peer: str, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        with self._locked(request_id) as path:
            record = self._load(path)
            self._expire(record, now)
            if record["state"] != "approved":
                if record["state"] == "active":
                    raise DigError("dig grant was already consumed")
                raise DigError(f"cannot consume dig in state {record['state']}")
            if verified_peer != record["request"]["source_colony"]:
                raise DigError("Wire peer does not match approved source")
            supplied = self._cap_digest(capability)
            if not hmac.compare_digest(supplied, record["capability_sha256"]):
                raise DigError("invalid dig capability")
            record.update(state="active", started_at=now)
            record.pop("capability_sha256", None)
            self._replace(path, record)
            return self._public(record)

    def finish(self, request_id: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        with self._locked(request_id) as path:
            record = self._load(path)
            if record["state"] != "active":
                raise DigError(f"cannot finish dig in state {record['state']}")
            record.update(state="completed", finished_at=now)
            self._replace(path, record)
            return self._public(record)

    def revoke(self, request_id: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        with self._locked(request_id) as path:
            record = self._load(path)
            if record["state"] in {"completed", "revoked", "expired"}:
                raise DigError(f"cannot revoke dig in state {record['state']}")
            record.update(state="revoked", revoked_at=now)
            record.pop("capability_sha256", None)
            self._replace(path, record)
            return self._public(record)

    def status(self, request_id: str, *, now: int | None = None) -> dict:
        now = int(time.time()) if now is None else int(now)
        with self._locked(request_id) as path:
            record = self._load(path)
            if self._expire(record, now):
                self._replace(path, record)
            return self._public(record)

    @staticmethod
    def _public(record: dict) -> dict:
        public = dict(record)
        public.pop("capability_sha256", None)
        return public

    @staticmethod
    def _load(path: Path) -> dict:
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError as exc:
            raise DigError("unknown dig request") from exc
        if record.get("state") not in _STATES:
            raise DigError("invalid stored dig state")
        return record

    @staticmethod
    def _expire(record: dict, now: int) -> bool:
        deadline = record.get("grant_expires_at", record["request"]["expires_at"])
        if record["state"] in {"pending", "approved"} and now >= deadline:
            record.update(state="expired", expired_at=now)
            record.pop("capability_sha256", None)
            return True
        return False
