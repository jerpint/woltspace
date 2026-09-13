"""Runtime settings resolved at the application boundary.

The extraction is intentionally small: callers can inject this immutable
context in tests while the existing environment variables remain the defaults
in production. It holds only what the runtime actually reads — paths belong
here once a call site needs them injected, not before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class RuntimeContext:
    """OS-facing settings shared by runtime implementations."""

    tmux_bin: str = "tmux"
    ps_bin: str = "ps"
    isolation: str = "external"
    #: Where cross-process delivery locks are kept. None means the colony's
    #: own `.space/locks/`, which is what production wants: the lock only does
    #: its job if every process that delivers picks the same file. A test
    #: points it at a tmp dir so a unit test with a fake tmux never reaches
    #: for real colony state.
    lock_dir: str | Path | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeContext":
        values = os.environ if env is None else env
        isolation = values.get("WOLTSPACE_ISOLATION", "external")
        if isolation not in {"external", "host"}:
            raise ValueError(
                "WOLTSPACE_ISOLATION must be 'external' or 'host'"
            )
        return cls(
            tmux_bin=values.get("WOLTSPACE_TMUX_BIN", "tmux"),
            ps_bin=values.get("WOLTSPACE_PS_BIN", "ps"),
            isolation=isolation,
        )
