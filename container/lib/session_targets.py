"""Session ownership and working-directory resolution.

A wolt owns durable identity and memory. A session independently chooses the
existing directory where its harness runs. Keeping that pair in one value
prevents callers from accidentally deriving identity from the process cwd.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionTarget:
    """The durable owner and canonical execution directory for one session."""

    wolt_id: str
    canonical_workdir: Path

    @classmethod
    def resolve(
        cls,
        wolt_id: str,
        workdir: str | Path | None,
        *,
        wolts_dir: str | Path,
    ) -> "SessionTarget":
        """Validate a new target and resolve symlinks before persistence."""
        if not wolt_id:
            raise ValueError("wolt is required for session creation")

        wolt_home = Path(wolts_dir) / wolt_id
        if not wolt_home.is_dir():
            raise ValueError(f"wolt '{wolt_id}' not found at {wolt_home}")

        requested = Path(workdir).expanduser() if workdir else wolt_home
        try:
            canonical = requested.resolve(strict=True)
        except (FileNotFoundError, OSError) as exc:
            raise ValueError(f"workdir does not exist: {requested}") from exc
        if not canonical.is_dir():
            raise ValueError(f"workdir is not a directory: {canonical}")
        return cls(wolt_id=wolt_id, canonical_workdir=canonical)

    @classmethod
    def from_record(
        cls,
        record: dict,
        *,
        wolts_dir: str | Path,
        fallback_wolt: str = "",
    ) -> "SessionTarget":
        """Read new records and legacy ``wolt``/``dir`` records safely.

        Historical directories may no longer exist. Reading registry history
        must still work, so compatibility normalization is non-strict; only new
        session creation requires an existing directory.
        """
        raw_target = record.get("target")
        target = raw_target if isinstance(raw_target, dict) else {}
        wolt_id = str(
            target.get("wolt_id")
            or record.get("wolt_id")
            or record.get("wolt")
            or fallback_wolt
        )
        raw_workdir = (
            target.get("canonical_workdir")
            or record.get("workdir")
            or record.get("dir")
            or (Path(wolts_dir) / wolt_id if wolt_id else Path(wolts_dir))
        )
        canonical = Path(raw_workdir).expanduser().resolve(strict=False)
        return cls(wolt_id=wolt_id, canonical_workdir=canonical)

    def to_record(self) -> dict[str, str]:
        return {
            "wolt_id": self.wolt_id,
            "canonical_workdir": str(self.canonical_workdir),
        }


def normalize_session_target(
    record: dict,
    *,
    wolts_dir: str | Path,
    fallback_wolt: str = "",
) -> dict:
    """Return one compatibility-safe record with a first-class target.

    New writers persist the target and legacy aliases together. Old records
    receive the same normalized view and are persisted in this shape the next
    time they are updated; reads alone never rewrite historical files.
    """
    normalized = dict(record)
    target = SessionTarget.from_record(
        normalized, wolts_dir=wolts_dir, fallback_wolt=fallback_wolt
    )
    normalized["target"] = target.to_record()
    normalized["wolt_id"] = target.wolt_id
    normalized["workdir"] = str(target.canonical_workdir)
    # Compatibility aliases for all existing consumers and scripts.
    normalized["wolt"] = target.wolt_id
    normalized["dir"] = str(target.canonical_workdir)
    return normalized
