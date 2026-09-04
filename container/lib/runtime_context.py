"""Runtime paths and executable names resolved at the application boundary.

The first extraction is intentionally small: callers can inject this immutable
context in tests while the existing environment variables and container paths
remain the defaults in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeContext:
    """OS-facing settings shared by runtime implementations."""

    install_root: Path
    wolts_root: Path
    run_session_script: Path
    tmux_bin: str = "tmux"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        install_root: str | Path | None = None,
        wolts_root: str | Path | None = None,
        run_session_script: str | Path | None = None,
    ) -> "RuntimeContext":
        values = os.environ if env is None else env
        root = Path(install_root or Path(__file__).resolve().parents[2])
        wolts = Path(wolts_root or values.get("WOLTS_DIR", "/workspace/wolts"))
        script = Path(
            run_session_script
            or values.get("WOLTSPACE_RUN_SESSION_SCRIPT", root / "container" / "bin" / "run-session.sh")
        )
        return cls(
            install_root=root,
            wolts_root=wolts,
            run_session_script=script,
            tmux_bin=values.get("WOLTSPACE_TMUX_BIN", "tmux"),
        )
