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


def dialable_host(host: str) -> str:
    """The bind host, rewritten into something a child can actually connect to.

    A bind address and a client address are not the same thing. `0.0.0.0` and
    `::` mean "every interface" to a listener and nothing useful to a caller,
    and an IPv6 literal has to be bracketed before it can go in a URL at all —
    `http://::1:7777` is unparseable. Everything a child dials (its endpoint,
    the API URL stamped into session and connector environments) goes through
    here, so a control plane bound to a LAN or IPv6 address is still reachable
    by the wolf, the sessions, and `notify`.
    """
    raw = (host or "").strip().strip("[]")
    if raw in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if raw == "::":
        return "[::1]"
    return f"[{raw}]" if ":" in raw else raw


def resolve_install_root(raw: str | None = None) -> Path:
    """Honour a WOLTSPACE_DIR that still names an install; ignore a corpse.

    The env override is a supported knob, but it is also the most
    self-perpetuating way to break a machine: tmux hands its server environment
    to every session it spawns, so a value that once pointed at a since-deleted
    or emptied install root outlives the install and gets inherited back on
    every restart. When the pointed-at directory no longer looks like a
    platform, the install we are actually running from wins — a fresh install
    can then heal a host without anyone hunting down the stale export.
    """
    if raw:
        candidate = Path(raw).expanduser().resolve(strict=False)
        if looks_like_install_root(candidate):
            return candidate
    return Path(installation_root()).expanduser().resolve(strict=False)


def looks_like_install_root(path: Path) -> bool:
    """Whether a path still holds a platform runtime.

    The same signature `installation_root` tests for: a directory is only an
    install root while `container/lib` lives inside it.
    """
    return (Path(path) / "container" / "lib").is_dir()


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
        """Where a caller on this machine reaches this instance's API."""
        return f"http://{dialable_host(self.host)}:{self.port}"

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, isolation: str | None = None
    ) -> "RuntimeLayout":
        values = os.environ if env is None else env
        raw_wolts = values.get("WOLTS_DIR", "~/.woltspace/wolts")
        wolts_dir = Path(raw_wolts).expanduser().resolve(strict=False)
        root = cls._resolve_install_root(values.get("WOLTSPACE_DIR"))
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

    _resolve_install_root = staticmethod(resolve_install_root)

    @property
    def is_entrypoint(self) -> bool:
        """True only when this process was launched as the platform entrypoint.

        `woltspace container-entrypoint` sets WOLTSPACE_ENTRYPOINT; `woltspace start` sets it
        for the control plane it launches. Anything else — a `serve` typed in a
        worktree, a smoke test, an agent poking around — is a *guest*, and a
        guest must not take over the data root, the tunnel, or the bot token
        just because the ambient environment happens to name them.
        """
        return os.environ.get("WOLTSPACE_ENTRYPOINT", "").strip().lower() in {
            "1", "true", "yes", "on",
        }

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
        os.environ["WOLTSPACE_PORT"] = str(self.port)
        # The one address every child is told. `notify`, `push-view`,
        # `send-to-session` and the in-container `woltspace` CLI all used to
        # bake in http://localhost:7777, which is a lie on any instance started
        # with `--port 8080`: the call either reached the *wrong* colony or
        # nothing at all. Stamped here, inherited by sessions (see
        # `_SESSION_ENV_KEYS` in session_runtime) and by connectors.
        os.environ["WOLTSPACE_API"] = self.endpoint
