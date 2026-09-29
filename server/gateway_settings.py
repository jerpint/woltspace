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


_CACHE: dict[tuple[Path, int, str], tuple[tuple[int, int] | None, GatewaySettings]] = {}
_LOCK = threading.Lock()

# Chromium/Firefox refuse these ports for HTTP. Keep this explicit so a
# configured gateway cannot start successfully but remain unusable in browsers.
_BLOCKED_PORTS = {
    1, 7, 9, 11, 13, 15, 17, 19, 20, 21, 22, 23, 25, 37, 42, 43, 53,
    69, 77, 79, 87, 95, 101, 102, 103, 104, 109, 110, 111, 113, 115, 117,
    119, 123, 135, 137, 139, 143, 161, 179, 389, 427, 465, 512, 513, 514,
    515, 526, 530, 531, 532, 540, 548, 554, 556, 563, 587, 601, 636, 989,
    990, 993, 995, 1719, 1720, 1723, 2049, 3659, 4045, 5060, 5061, 6000,
    6566, 6665, 6666, 6667, 6668, 6669, 6697, 10080,
}


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
    port = lodge_port - 660
    if not 1024 <= port <= 65535:
        raise ValueError("derived app gateway port must be from 1024 to 65535")
    return port


def _validate_port(settings: GatewaySettings, lodge_port: int) -> GatewaySettings:
    if settings.port == lodge_port:
        raise ValueError("app gateway port must differ from the lodge port")
    if settings.port in _BLOCKED_PORTS:
        raise ValueError(
            f"app gateway port {settings.port} is blocked by web browsers; choose a safe port"
        )
    return settings


def load_gateway_settings(
    wolts_dir: Path, *, lodge_port: int | None = None,
) -> GatewaySettings:
    path = wolts_dir / "woltspace.json"
    lodge_port = _resolve_lodge_port(lodge_port)
    env_port = os.environ.get("WOLTSPACE_APP_GATEWAY_PORT", "").strip()
    cache_key = (path, lodge_port, env_port)
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
    configured = gateway.get("port", default_port) if isinstance(gateway, dict) else default_port
    try:
        port = int(env_port) if env_port else configured
    except ValueError:
        raise ValueError("WOLTSPACE_APP_GATEWAY_PORT must be an integer from 1024 to 65535")
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        port = default_port
    settings = GatewaySettings(domain or None, port)
    _validate_port(settings, lodge_port)
    with _LOCK:
        _CACHE[cache_key] = (signature, settings)
    return settings
