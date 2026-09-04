"""Host paths resolved before importing the server or session runtime."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def installation_root() -> Path:
    """Return the source root or the self-contained wheel bundle."""
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "server").is_dir() and (source_root / "container" / "lib").is_dir():
        return source_root
    return Path(__file__).resolve().parent / "_bundle"


@dataclass(frozen=True)
class RuntimeLayout:
    wolts_dir: Path
    install_root: Path
    host: str = "127.0.0.1"
    port: int = 7777
    isolation: str = "host"

    @property
    def state_root(self) -> Path:
        return self.wolts_dir / ".space"

    @property
    def platform_state(self) -> Path:
        return self.state_root / "platform"

    @property
    def logs_dir(self) -> Path:
        return self.state_root / "logs"

    @property
    def runtime_lib(self) -> Path:
        return self.install_root / "container" / "lib"

    @property
    def endpoint(self) -> str:
        return f"http://{self.host}:{self.port}"

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, isolation: str | None = None
    ) -> "RuntimeLayout":
        values = os.environ if env is None else env
        raw_wolts = values.get("WOLTS_DIR", "~/.woltspace/wolts")
        wolts_dir = Path(raw_wolts).expanduser().resolve(strict=False)
        root = Path(values.get("WOLTSPACE_DIR", installation_root())).expanduser().resolve(
            strict=False
        )
        resolved_isolation = isolation or values.get("WOLTSPACE_ISOLATION", "host")
        if resolved_isolation not in {"host", "external"}:
            raise ValueError("isolation must be 'host' or 'external'")
        return cls(
            wolts_dir=wolts_dir,
            install_root=root,
            host=values.get("WOLTSPACE_HOST", "127.0.0.1"),
            port=int(values.get("WOLTSPACE_PORT") or values.get("PORT") or "7777"),
            isolation=resolved_isolation,
        )

    def apply_environment(self) -> None:
        """Freeze paths before modules with import-time configuration load."""
        for path in (self.install_root, self.runtime_lib):
            resolved = str(path)
            if resolved in sys.path:
                sys.path.remove(resolved)
            sys.path.insert(0, resolved)
        os.environ["WOLTS_DIR"] = str(self.wolts_dir)
        os.environ.setdefault("WOLT_DIR", str(self.wolts_dir))
        os.environ["WOLTSPACE_DIR"] = str(self.install_root)
        os.environ["WOLTSPACE_ISOLATION"] = self.isolation
        os.environ["WOLTSPACE_HOST"] = self.host
        os.environ["PORT"] = str(self.port)
