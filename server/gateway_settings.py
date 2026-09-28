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
    port: int


_CACHE: dict[tuple[Path, int], tuple[tuple[int, int] | None, GatewaySettings]] = {}
_LOCK = threading.Lock()


def _resolve_lodge_port(lodge_port: int | None) -> int:
    if lodge_port is None:
        try:
            lodge_port = int(
                os.environ.get("WOLTSPACE_PORT") or os.environ.get("PORT") or "7777"
            )
        except ValueError:
            lodge_port = 7777
    return lodge_port


def _default_gateway_port(lodge_port: int) -> int:
    port = lodge_port - 1110
    if not 1024 <= port <= 65535:
        raise ValueError("derived app gateway port must be from 1024 to 65535")
    return port


def _validate_port(settings: GatewaySettings, lodge_port: int) -> GatewaySettings:
    if settings.port == lodge_port:
        raise ValueError("app gateway port must differ from the lodge port")
    return settings


def load_gateway_settings(
    wolts_dir: Path, *, lodge_port: int | None = None,
) -> GatewaySettings:
    path = wolts_dir / "woltspace.json"
    lodge_port = _resolve_lodge_port(lodge_port)
    cache_key = (path, lodge_port)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    with _LOCK:
        cached = _CACHE.get(cache_key)
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
    default_port = _default_gateway_port(lodge_port)
    port = gateway.get("port", default_port) if isinstance(gateway, dict) else default_port
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        port = default_port
    settings = GatewaySettings(domain or None, port)
    _validate_port(settings, lodge_port)
    with _LOCK:
        _CACHE[cache_key] = (signature, settings)
    return settings
