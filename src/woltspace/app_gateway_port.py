"""One safe resolver for the app gateway's effective port."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path


BLOCKED_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
    69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117,
    119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514,
    515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989,
    990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
    6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
}

_WARNED: set[tuple[str, str, int]] = set()


def derived_gateway_port(lodge_port: int) -> int:
    port = lodge_port - 660
    if not 1024 <= port <= 65535 or port == lodge_port or port in BLOCKED_PORTS:
        raise ValueError(f"lodge port {lodge_port!r} cannot derive a safe app gateway port")
    return port


def validate_gateway_port(value: object, lodge_port: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1024 <= value <= 65535:
        raise ValueError("port must be an integer from 1024 to 65535")
    if value == lodge_port:
        raise ValueError("app gateway port must differ from the lodge port")
    if value in BLOCKED_PORTS:
        raise ValueError(f"port {value} is blocked by web browsers; choose a safe port")
    return value


def _warn_bad(source: str, value: object, fallback: int) -> None:
    rendered = repr(value)
    key = (source, rendered, fallback)
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(
        f"WARNING: invalid app gateway port from {source}: {rendered}; using {fallback}",
        file=sys.stderr,
        flush=True,
    )


def resolve_app_gateway_port(
    wolts_dir: Path, lodge_port: int, *, env: Mapping[str, str] | None = None,
) -> int:
    """Resolve env > Settings > derived, falling safely back on bad input."""
    fallback = derived_gateway_port(lodge_port)
    values = os.environ if env is None else env
    raw_env = values.get("WOLTSPACE_APP_GATEWAY_PORT", "").strip()
    if raw_env:
        try:
            return validate_gateway_port(int(raw_env), lodge_port)
        except (ValueError, TypeError):
            _warn_bad("WOLTSPACE_APP_GATEWAY_PORT", raw_env, fallback)
            return fallback

    path = Path(wolts_dir) / "woltspace.json"
    try:
        root = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        root = {}
    gateway = root.get("app_gateway") if isinstance(root, dict) else None
    configured = gateway.get("port") if isinstance(gateway, dict) else None
    if configured is None:
        return fallback
    try:
        return validate_gateway_port(configured, lodge_port)
    except ValueError:
        _warn_bad("woltspace.json app_gateway.port", configured, fallback)
        return fallback
