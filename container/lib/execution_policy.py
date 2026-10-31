"""Explicit session permissions and repository-scoped Auto consent."""

from __future__ import annotations

import json
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import lodge_db
from session_targets import SessionTarget


# POLICY_VERSION versions the host consent (grants); RECORD_VERSION versions a
# session's stored policy. Record version 1 had two modes, and its "auto" ran
# every engine with no checks at all: that is "bypass" now.
POLICY_VERSION = 1
RECORD_VERSION = 2
VALID_ISOLATION = {"external", "host"}
VALID_MODES = {"prompt", "auto", "bypass"}
GRANTED_MODES = {"auto", "bypass"}  # what the host grant allows
_V1_MODES = {"auto": "bypass"}


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
    policy_version: int = RECORD_VERSION

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
        """Read a stored policy of any version as a current one.

        A bare string or a dict without a version is version 1, and no policy
        at all is the historical implicit Auto: in version 1, "auto" means
        bypass. A version this build does not know is refused, never guessed.
        """
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            version = int(value.get("policy_version", 1))
            mode = value.get("mode", "auto")
            isolation = value.get("isolation", default_isolation)
            if version == 1:
                mode = _V1_MODES.get(mode, mode)
            elif version != RECORD_VERSION:
                raise ValueError(f"unknown execution policy version: {version}")
        elif isinstance(value, str) and value:
            mode, isolation = _V1_MODES.get(value, value), default_isolation
        else:
            mode, isolation = "bypass", "external"
        return cls(mode=mode, isolation=isolation)


class AutoGrantStore:
    """Auto consents, keyed by wolt, canonical path, and version: the lodge
    DB's `grants` rows of kind 'auto'."""

    def __init__(self, wolts_dir: str | Path):
        self.wolts_dir = Path(wolts_dir)

    def _select(self, where: str, args: tuple) -> list[AutoGrant]:
        # A lodge with no DB has no grants; reading it must not create one.
        if not lodge_db.path(self.wolts_dir).exists():
            return []
        with closing(lodge_db.connect(self.wolts_dir)) as conn:
            rows = conn.execute(
                "SELECT wolt_id, scope, policy_version, approved_at FROM grants"
                f" WHERE kind='auto'{where}", args,
            ).fetchall()
        return [AutoGrant(r["wolt_id"], Path(r["scope"]), r["policy_version"],
                          r["approved_at"]) for r in rows]

    @staticmethod
    def _key(target: SessionTarget) -> tuple:
        return (target.wolt_id, str(target.canonical_workdir), POLICY_VERSION)

    def list(self) -> list[AutoGrant]:
        return self._select("", ())

    def find(self, target: SessionTarget) -> AutoGrant | None:
        found = self._select(
            " AND wolt_id=? AND scope=? AND policy_version=?", self._key(target)
        )
        return found[0] if found else None

    def grant(self, target: SessionTarget) -> AutoGrant:
        approved = AutoGrant(
            wolt_id=target.wolt_id,
            canonical_workdir=target.canonical_workdir,
            approved_at=int(time.time()),
        )
        with lodge_db.write(self.wolts_dir) as conn:
            conn.execute(
                "INSERT INTO grants(kind, wolt_id, scope, policy_version, approved_at)"
                " VALUES ('auto', ?, ?, ?, ?) ON CONFLICT(kind, wolt_id, scope) DO UPDATE"
                " SET policy_version=excluded.policy_version, approved_at=excluded.approved_at",
                self._key(target) + (approved.approved_at,),
            )
        return approved

    def revoke(self, target: SessionTarget) -> bool:
        with lodge_db.write(self.wolts_dir) as conn:
            return conn.execute(
                "DELETE FROM grants WHERE kind='auto' AND wolt_id=? AND scope=?"
                " AND policy_version=?", self._key(target),
            ).rowcount > 0

    def revoke_wolt(self, wolt_id: str) -> int:
        """Revoke every grant for a wolt, whatever its workdir; return how many."""
        with lodge_db.write(self.wolts_dir) as conn:
            return conn.execute(
                "DELETE FROM grants WHERE kind='auto' AND wolt_id=?", (wolt_id,)
            ).rowcount


def import_grants(conn, wolts_dir: str | Path) -> tuple[dict, list[Path]]:
    """One-time import of `.space/auto-grants.json` and `.space/digs/grants.json`.

    Returns the counts and the files read, which `lodge_db.run_imports`
    retires after the commit. One that cannot be read is logged and kept.
    """
    space = Path(wolts_dir) / ".space"
    counts = {"auto": 0, "dig": 0, "skipped": 0}
    retire = []
    for kind, source in (("auto", space / "auto-grants.json"),
                         ("dig", space / "digs" / "grants.json")):
        if not source.exists():
            continue
        try:
            records = json.loads(source.read_text())["grants"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"[lodge-db] {source} not imported: {exc}", file=sys.stderr)
            continue
        for item in records:
            if not isinstance(item, dict) or (kind == "dig" and not item.get("name")):
                continue
            # OR IGNORE: a row the DB already has is newer than any file.
            if kind == "auto":
                grant = AutoGrant.from_record(item)
                row = (grant.wolt_id, str(grant.canonical_workdir), grant.policy_version,
                       grant.approved_at, None)
            else:
                row = (str(item.get("wolt", "")), item["name"], 0,
                       int(item.get("created_at", 0)), json.dumps(item))
            inserted = conn.execute(
                "INSERT OR IGNORE INTO grants(kind, wolt_id, scope, policy_version,"
                " approved_at, record) VALUES (?, ?, ?, ?, ?, ?)", (kind, *row),
            ).rowcount
            counts[kind if inserted else "skipped"] += 1
        retire.append(source)
    return counts, retire


def default_mode(isolation: str, granted: bool, lodge_default: str | None = None) -> str:
    """The level of a session that asks for none: the lodge's own choice where it
    needs no grant or has one, else Bypass where granted or isolated, else Ask."""
    if lodge_default in VALID_MODES and (
        lodge_default not in GRANTED_MODES or isolation != "host" or granted
    ):
        return lodge_default
    return "bypass" if isolation == "external" or granted else "prompt"


def resolve_execution_policy(
    requested: str | None,
    *,
    isolation: str,
    target: SessionTarget,
    grants: AutoGrantStore,
    lodge_default: str | None = None,
) -> tuple[ExecutionPolicy, AutoGrant | None]:
    """Resolve defaults and enforce host consent for the exact target.

    On the host the grant *is* the consent, so it also decides the default: a
    caller that asks for nothing gets Bypass exactly where someone has already
    approved this wolt in this directory, and prompt everywhere else. Without
    that, every unattended native session — the bot's spawns, the lodge's —
    booted prompt-mode with nobody there to answer, because no spawn path asks
    for a level by name.

    One grant allows both Auto and Bypass. Explicit still beats implicit in
    both directions: `requested="prompt"` keeps asking even where the grant
    exists, and Auto or Bypass without a grant is refused rather than quietly
    downgraded. `lodge_default` (woltspace.json sessions.default_execution_policy,
    already checked against the engine) replaces that default; see default_mode.
    """
    grant = grants.find(target) if isolation == "host" else None
    mode = requested or default_mode(isolation, grant is not None, lodge_default)
    policy = ExecutionPolicy(mode=mode, isolation=isolation)
    if mode in GRANTED_MODES and isolation == "host" and grant is None:
        raise PermissionError(
            "Auto and Bypass are not approved for "
            f"wolt '{target.wolt_id}' in '{target.canonical_workdir}'. "
            "Grant that exact wolt and directory before spawning."
        )
    # A prompt-mode session records no grant: the registry says what the
    # session actually runs under, not what it could have run under.
    return policy, grant if mode in GRANTED_MODES else None
