"""Exclusive data-root ownership and exact-instance inspection."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .layout import RuntimeLayout


@dataclass(frozen=True)
class InstanceOwner:
    instance_id: str
    pid: int
    started_at: int
    endpoint: str
    isolation: str
    hostname: str

    def to_record(self) -> dict:
        return {
            "instance_id": self.instance_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "endpoint": self.endpoint,
            "isolation": self.isolation,
            "hostname": self.hostname,
        }

    @classmethod
    def from_record(cls, data: dict) -> "InstanceOwner":
        return cls(
            instance_id=str(data.get("instance_id", "")),
            pid=int(data.get("pid", 0)),
            started_at=int(data.get("started_at", 0)),
            endpoint=str(data.get("endpoint", "")),
            isolation=str(data.get("isolation", "")),
            hostname=str(data.get("hostname", "")),
        )


class InstanceConflict(RuntimeError):
    def __init__(self, owner: InstanceOwner | None, layout: RuntimeLayout):
        self.owner = owner
        if owner:
            detail = (
                f"PID {owner.pid} on {owner.hostname} owns {layout.wolts_dir} "
                f"at {owner.endpoint} (instance {owner.instance_id})"
            )
        else:
            detail = f"another process owns {layout.wolts_dir}"
        super().__init__(
            f"{detail}. Run `woltspace status --json` or stop that instance first."
        )


class DataRootLock:
    """A held flock plus atomic human-readable owner metadata."""

    def __init__(self, layout: RuntimeLayout, instance_id: str):
        self.layout = layout
        self.instance_id = instance_id
        self.lock_path = layout.platform_state / "control-plane.lock"
        self.owner_path = layout.platform_state / "control-plane.json"
        self._handle = None
        self.owner: InstanceOwner | None = None

    def acquire(self) -> InstanceOwner:
        self.layout.platform_state.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise InstanceConflict(read_owner(self.layout), self.layout) from exc

        owner = InstanceOwner(
            instance_id=self.instance_id,
            pid=os.getpid(),
            started_at=int(time.time()),
            endpoint=self.layout.endpoint,
            isolation=self.layout.isolation,
            hostname=socket.gethostname(),
        )
        try:
            write_owner(self.layout, owner)
        except Exception:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        self._handle = handle
        self.owner = owner
        return owner

    def release(self) -> None:
        if self._handle is None:
            return
        current = read_owner(self.layout)
        if current and current.instance_id == self.instance_id:
            self.owner_path.unlink(missing_ok=True)
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()


def owner_path(layout: RuntimeLayout) -> Path:
    return layout.platform_state / "control-plane.json"


def read_owner(layout: RuntimeLayout) -> InstanceOwner | None:
    try:
        data = json.loads(owner_path(layout).read_text())
        owner = InstanceOwner.from_record(data)
        return owner if owner.instance_id and owner.pid > 0 else None
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def write_owner(layout: RuntimeLayout, owner: InstanceOwner) -> None:
    path = owner_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(owner.to_record(), indent=2) + "\n")
    tmp.chmod(0o600)
    tmp.replace(path)


def clear_owner_if_unlocked(layout: RuntimeLayout, instance_id: str) -> bool:
    """Remove exact stale metadata only while no control plane holds the lock."""
    lock_path = layout.platform_state / "control-plane.lock"
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return False
    try:
        current = read_owner(layout)
        if current and current.instance_id == instance_id:
            owner_path(layout).unlink(missing_ok=True)
            return True
        return False
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def read_health(endpoint: str, *, timeout: float = 0.5) -> dict | None:
    try:
        with urllib.request.urlopen(f"{endpoint.rstrip('/')}/health", timeout=timeout) as response:
            data = json.loads(response.read())
            return data if isinstance(data, dict) else None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def inspect_instance(layout: RuntimeLayout) -> dict:
    owner = read_owner(layout)
    endpoint = owner.endpoint if owner and owner.endpoint else layout.endpoint
    health = read_health(endpoint)
    if owner and health and health.get("instance_id") == owner.instance_id:
        state = "healthy"
    elif health:
        state = "conflict"
    elif owner and pid_alive(owner.pid):
        state = "starting"
    elif owner:
        state = "stale"
    else:
        state = "stopped"
    return {
        "state": state,
        "healthy": state == "healthy",
        "endpoint": endpoint,
        "wolts_dir": str(layout.wolts_dir),
        "owner": owner.to_record() if owner else None,
        "health": health,
    }
