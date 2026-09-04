"""Explicit session permissions and repository-scoped Auto consent."""

from __future__ import annotations

import fcntl
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from session_targets import SessionTarget


POLICY_VERSION = 1
VALID_ISOLATION = {"external", "host"}
VALID_MODES = {"prompt", "auto"}


@dataclass(frozen=True)
class AutoGrant:
    wolt_id: str
    canonical_workdir: Path
    policy_version: int = POLICY_VERSION
    approved_at: int = 0

    def to_record(self) -> dict:
        return {
            "wolt_id": self.wolt_id,
            "canonical_workdir": str(self.canonical_workdir),
            "policy_version": self.policy_version,
            "approved_at": self.approved_at,
        }

    @classmethod
    def from_record(cls, data: dict) -> "AutoGrant":
        return cls(
            wolt_id=str(data.get("wolt_id", "")),
            canonical_workdir=Path(data.get("canonical_workdir", "")).resolve(
                strict=False
            ),
            policy_version=int(data.get("policy_version", 0)),
            approved_at=int(data.get("approved_at", 0)),
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    """The effective permissions for one harness process."""

    mode: str
    isolation: str
    policy_version: int = POLICY_VERSION

    def __post_init__(self):
        if self.mode not in VALID_MODES:
            raise ValueError(f"unknown execution policy: {self.mode}")
        if self.isolation not in VALID_ISOLATION:
            raise ValueError(f"unknown runtime isolation: {self.isolation}")

    def to_record(self) -> dict:
        return {
            "mode": self.mode,
            "isolation": self.isolation,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_record(cls, value, *, default_isolation: str = "external"):
        """Read current records plus the historical implicit-Auto behavior."""
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls(
                mode=value.get("mode", "auto"),
                isolation=value.get("isolation", default_isolation),
                policy_version=int(value.get("policy_version", POLICY_VERSION)),
            )
        if isinstance(value, str) and value:
            return cls(mode=value, isolation=default_isolation)
        return cls(mode="auto", isolation="external")


class AutoGrantStore:
    """Atomic lodge-level store keyed by wolt, canonical path, and version."""

    def __init__(self, wolts_dir: str | Path):
        self.wolts_dir = Path(wolts_dir)
        self.path = self.wolts_dir / ".space" / "auto-grants.json"
        self.lock_path = self.path.with_suffix(".lock")

    @contextmanager
    def _lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("w") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield

    def _read(self) -> list[AutoGrant]:
        try:
            payload = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        records = payload.get("grants", []) if isinstance(payload, dict) else []
        return [AutoGrant.from_record(item) for item in records if isinstance(item, dict)]

    def _write(self, grants: list[AutoGrant]):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"grants": [g.to_record() for g in grants]}, indent=2) + "\n")
        tmp.chmod(0o600)
        tmp.replace(self.path)

    @staticmethod
    def _matches(grant: AutoGrant, target: SessionTarget) -> bool:
        return (
            grant.wolt_id == target.wolt_id
            and grant.canonical_workdir == target.canonical_workdir
            and grant.policy_version == POLICY_VERSION
        )

    def list(self) -> list[AutoGrant]:
        return self._read()

    def find(self, target: SessionTarget) -> AutoGrant | None:
        return next((g for g in self._read() if self._matches(g, target)), None)

    def grant(self, target: SessionTarget) -> AutoGrant:
        approved = AutoGrant(
            wolt_id=target.wolt_id,
            canonical_workdir=target.canonical_workdir,
            approved_at=int(time.time()),
        )
        with self._lock():
            grants = [g for g in self._read() if not self._matches(g, target)]
            grants.append(approved)
            self._write(grants)
        return approved

    def revoke(self, target: SessionTarget) -> bool:
        with self._lock():
            grants = self._read()
            kept = [g for g in grants if not self._matches(g, target)]
            changed = len(kept) != len(grants)
            if changed:
                self._write(kept)
        return changed


def resolve_execution_policy(
    requested: str | None,
    *,
    isolation: str,
    target: SessionTarget,
    grants: AutoGrantStore,
) -> tuple[ExecutionPolicy, AutoGrant | None]:
    """Resolve defaults and enforce host Auto consent for the exact target."""
    mode = requested or ("auto" if isolation == "external" else "prompt")
    policy = ExecutionPolicy(mode=mode, isolation=isolation)
    grant = grants.find(target) if mode == "auto" and isolation == "host" else None
    if mode == "auto" and isolation == "host" and grant is None:
        raise PermissionError(
            "Auto is not approved for "
            f"wolt '{target.wolt_id}' in '{target.canonical_workdir}'. "
            "Grant that exact wolt and directory before spawning."
        )
    return policy, grant


def policy_mode(value) -> str:
    """Return a command-builder mode, preserving old implicit-Auto callers."""
    return ExecutionPolicy.from_record(value).mode
