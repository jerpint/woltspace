"""Read-only, mtime-cached app gateway settings."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from woltspace.app_gateway_port import resolve_app_gateway_port


@dataclass(frozen=True)
class GatewaySettings:
    apps_domain: str | None
    port: int
    lodge_port: int = 7777


_CACHE: dict[tuple[Path, int, str], tuple[tuple[int, int] | None, GatewaySettings]] = {}
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
            return cached[1]
    try:
        root = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        root = {}
    if not isinstance(root, dict):
        root = {}
    domain = root.get("apps_domain")
    domain = domain.strip().lower().rstrip(".") if isinstance(domain, str) else None
    port = resolve_app_gateway_port(wolts_dir, lodge_port)
    settings = GatewaySettings(domain or None, port, lodge_port)
    with _LOCK:
        _CACHE[cache_key] = (signature, settings)
    return settings


def lodge_origin(settings: GatewaySettings, wolts_dir: Path, *, local: bool) -> str | None:
    """Where the lodge is for a visitor of a content host, or None.

    Locally the lodge is on loopback at its own port. Remotely it is the named
    tunnel's address when the control plane recorded one; without it a site
    page simply carries no links back to the lodge.
    """
    if local:
        return f"http://localhost:{settings.lodge_port}"
    url = os.environ.get("CLOUDFLARE_TUNNEL_URL", "").strip()
    if not url:
        try:
            state = json.loads((wolts_dir / ".space" / "platform" / "tunnel.json").read_text())
            url = state.get("url", "") if isinstance(state, dict) else ""
        except (OSError, json.JSONDecodeError):
            url = ""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.path not in {"", "/"}:
        return None
    return f"https://{parsed.netloc.lower()}"
