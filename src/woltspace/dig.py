"""Owner-approved SSH destinations for Woltspace digging v0."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


STORE_VERSION = "woltspace.digs/v0"
DEFAULT_BOOTSTRAP_DIR = ".woltspace/bootstrap"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_DESTINATION_RE = re.compile(r"^[A-Za-z0-9_.@:\[\]-]+$")
_REMOTE_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


class DigError(ValueError):
    pass


@dataclass(frozen=True)
class ResolvedSSH:
    hostname: str
    user: str
    port: int

    def to_record(self) -> dict:
        return {"hostname": self.hostname, "user": self.user, "port": self.port}


def _validate_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise DigError("dig name must use lowercase letters, numbers, '_' or '-'")
    return name


def _validate_destination(destination: str) -> str:
    if not destination or destination.startswith("-") or not _DESTINATION_RE.fullmatch(destination):
        raise DigError("SSH destination must be one host/alias without whitespace or options")
    return destination


def _validate_bootstrap_dir(value: str) -> str:
    path = Path(value)
    if (
        not value
        or not _REMOTE_PATH_RE.fullmatch(value)
        or path.is_absolute()
        or ".." in path.parts
    ):
        raise DigError("bootstrap directory must be relative to the remote user's home")
    return value


def resolve_ssh(
    destination: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> ResolvedSSH:
    destination = _validate_destination(destination)
    ssh_config = os.environ.get("WOLTSPACE_DIG_SSH_CONFIG", "").strip()
    config_args = ["-F", ssh_config] if ssh_config else []
    result = runner(
        ["ssh", *config_args, "-G", "--", destination],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        detail = (result.stderr or "ssh configuration could not be resolved").strip()
        raise DigError(detail)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if separator and key in {"hostname", "user", "port"} and key not in values:
            values[key] = value.strip()
    if not values.get("hostname") or not values.get("user"):
        raise DigError("ssh -G did not return a hostname and user")
    try:
        port = int(values.get("port", "22"))
    except ValueError as exc:
        raise DigError("ssh -G returned an invalid port") from exc
    if not 1 <= port <= 65535:
        raise DigError("ssh -G returned an invalid port")
    return ResolvedSSH(values["hostname"], values["user"], port)


class DigStore:
    def __init__(self, state_root: str | Path):
        self.root = Path(state_root) / "digs"
        self.path = self.root / "grants.json"
        self.lock_path = self.root / "grants.lock"

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"version": STORE_VERSION, "grants": []}
        try:
            payload = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise DigError("dig grant store is unreadable") from exc
        if payload.get("version") != STORE_VERSION or not isinstance(payload.get("grants"), list):
            raise DigError("dig grant store has an unsupported shape")
        return payload

    def _write(self, payload: dict) -> None:
        temp = self.path.with_suffix(".tmp")
        data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def grant(
        self,
        *,
        name: str,
        wolt: str,
        destination: str,
        resolved: ResolvedSSH,
        bootstrap_dir: str = DEFAULT_BOOTSTRAP_DIR,
        now: int | None = None,
    ) -> dict:
        name = _validate_name(name)
        destination = _validate_destination(destination)
        bootstrap_dir = _validate_bootstrap_dir(bootstrap_dir)
        now = int(time.time()) if now is None else int(now)
        record = {
            "name": name,
            "wolt": wolt,
            "destination": destination,
            "resolved": resolved.to_record(),
            "bootstrap_dir": bootstrap_dir,
            "created_at": now,
            "connect_count": 0,
            "last_connected_at": None,
            "last_exit_code": None,
        }
        with self._locked():
            payload = self._read()
            if any(item.get("name") == name for item in payload["grants"]):
                raise DigError(f"dig already exists: {name}")
            payload["grants"].append(record)
            self._write(payload)
        return dict(record)

    def get(self, name: str) -> dict:
        _validate_name(name)
        with self._locked():
            for record in self._read()["grants"]:
                if record.get("name") == name:
                    return dict(record)
        raise DigError(f"unknown dig: {name}")

    def list(self) -> list[dict]:
        with self._locked():
            return [dict(item) for item in self._read()["grants"]]

    def revoke(self, name: str) -> bool:
        _validate_name(name)
        with self._locked():
            payload = self._read()
            kept = [item for item in payload["grants"] if item.get("name") != name]
            if len(kept) == len(payload["grants"]):
                return False
            payload["grants"] = kept
            self._write(payload)
            return True

    def record_connection(self, name: str, *, exit_code: int, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with self._locked():
            payload = self._read()
            for record in payload["grants"]:
                if record.get("name") == name:
                    record["connect_count"] = int(record.get("connect_count", 0)) + 1
                    record["last_connected_at"] = now
                    record["last_exit_code"] = int(exit_code)
                    self._write(payload)
                    return
        raise DigError(f"unknown dig: {name}")


def connect(
    store: DigStore,
    name: str,
    *,
    command: Sequence[str] = (),
    actor_wolt: str = "",
    resolver: Callable[[str], ResolvedSSH] = resolve_ssh,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    grant = store.get(name)
    if actor_wolt and actor_wolt != grant["wolt"]:
        raise DigError(f"dig belongs to wolt {grant['wolt']}, not {actor_wolt}")
    current = resolver(grant["destination"])
    if current.to_record() != grant["resolved"]:
        raise DigError("SSH destination no longer resolves to the approved host, user, and port")
    argv = [
        "ssh",
        *(
            ["-F", os.environ["WOLTSPACE_DIG_SSH_CONFIG"]]
            if os.environ.get("WOLTSPACE_DIG_SSH_CONFIG", "").strip()
            else []
        ),
        "-o", "StrictHostKeyChecking=yes",
        "--",
        grant["destination"],
    ]
    if command:
        # OpenSSH concatenates every remaining local argv item into one remote
        # shell command without preserving argument boundaries. Quote once
        # here so spaces and metacharacters inside an intended argument remain
        # data when the remote login shell parses it.
        argv.append(shlex.join(command))
    try:
        result = runner(argv, check=False)
    except KeyboardInterrupt:
        store.record_connection(name, exit_code=130)
        raise
    except OSError as exc:
        store.record_connection(name, exit_code=126)
        raise DigError(f"could not start ssh: {exc}") from exc
    store.record_connection(name, exit_code=result.returncode)
    return int(result.returncode)
