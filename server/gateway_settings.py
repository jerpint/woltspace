"""Read-only, mtime-cached app gateway settings."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GatewaySettings:
    apps_domain: str | None
    port: int = 4444


_CACHE: dict[Path, tuple[tuple[int, int] | None, GatewaySettings]] = {}
_LOCK = threading.Lock()


def _validate_port(settings: GatewaySettings, lodge_port: int | None) -> GatewaySettings:
    if lodge_port is None:
        try:
            lodge_port = int(
                os.environ.get("WOLTSPACE_PORT") or os.environ.get("PORT") or "7777"
            )
        except ValueError:
            lodge_port = 7777
    if settings.port == lodge_port:
        raise ValueError("app gateway port must differ from the lodge port")
    return settings


def load_gateway_settings(
    wolts_dir: Path, *, lodge_port: int | None = None,
) -> GatewaySettings:
    path = wolts_dir / "woltspace.json"
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    with _LOCK:
        cached = _CACHE.get(path)
        if cached and cached[0] == signature:
            return _validate_port(cached[1], lodge_port)
    try:
        root = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        root = {}
    if not isinstance(root, dict):
        root = {}
    domain = root.get("apps_domain")
    domain = domain.strip().lower().rstrip(".") if isinstance(domain, str) else None
    gateway = root.get("app_gateway")
    port = gateway.get("port", 4444) if isinstance(gateway, dict) else 4444
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        port = 4444
    settings = GatewaySettings(domain or None, port)
    _validate_port(settings, lodge_port)
    with _LOCK:
        _CACHE[path] = (signature, settings)
    return settings
