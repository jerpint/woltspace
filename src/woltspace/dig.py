"""Owner-approved SSH destinations for Woltspace digging v0."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import lodge_db  # container/lib: whoever imports this module grafts it first


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
    """Dig grants: the lodge DB's `grants` rows of kind 'dig', one per name."""

    def __init__(self, state_root: str | Path):
        self.wolts_dir = Path(state_root).parent

    def _records(self, name: str | None = None) -> list[dict]:
        if not lodge_db.path(self.wolts_dir).exists():
            return []
        with closing(lodge_db.connect(self.wolts_dir)) as conn:
            rows = conn.execute(
                "SELECT record FROM grants WHERE kind='dig'"
                + (" AND scope=?" if name else "") + " ORDER BY approved_at, scope",
                (name,) if name else (),
            ).fetchall()
        return [json.loads(row["record"]) for row in rows]

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
        with lodge_db.write(self.wolts_dir) as conn:
            if conn.execute("SELECT 1 FROM grants WHERE kind='dig' AND scope=?",
                            (name,)).fetchone():
                raise DigError(f"dig already exists: {name}")
            conn.execute(
                "INSERT INTO grants(kind, wolt_id, scope, approved_at, record)"
                " VALUES ('dig', ?, ?, ?, ?)",
                (wolt, name, now, json.dumps(record)),
            )
        return dict(record)

    def get(self, name: str) -> dict:
        found = self._records(_validate_name(name))
        if not found:
            raise DigError(f"unknown dig: {name}")
        return found[0]

    def list(self) -> list[dict]:
        return self._records()

    def revoke(self, name: str) -> bool:
        _validate_name(name)
        with lodge_db.write(self.wolts_dir) as conn:
            return conn.execute(
                "DELETE FROM grants WHERE kind='dig' AND scope=?", (name,)
            ).rowcount > 0

    def record_connection(self, name: str, *, exit_code: int, now: int | None = None) -> None:
        now = int(time.time()) if now is None else int(now)
        with lodge_db.write(self.wolts_dir) as conn:
            changed = conn.execute(
                "UPDATE grants SET record=json_set(record,"
                " '$.connect_count', COALESCE(json_extract(record, '$.connect_count'), 0) + 1,"
                " '$.last_connected_at', ?, '$.last_exit_code', ?)"
                " WHERE kind='dig' AND scope=?",
                (now, int(exit_code), name),
            ).rowcount
        if not changed:
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
