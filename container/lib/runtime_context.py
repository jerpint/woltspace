"""Runtime settings resolved at the application boundary.

The extraction is intentionally small: callers can inject this immutable
context in tests while the existing environment variables remain the defaults
in production. It holds only what the runtime actually reads — paths belong
here once a call site needs them injected, not before.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuntimeContext:
    """OS-facing settings shared by runtime implementations."""

    tmux_bin: str = "tmux"
    ps_bin: str = "ps"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "RuntimeContext":
        values = os.environ if env is None else env
        return cls(
            tmux_bin=values.get("WOLTSPACE_TMUX_BIN", "tmux"),
            ps_bin=values.get("WOLTSPACE_PS_BIN", "ps"),
        )
