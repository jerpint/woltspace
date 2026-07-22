"""Lodge tunnel — starts cloudflared at server boot, writes URL + PID to state."""

import json
import logging
import os
from pathlib import Path

from .config import SPACE_PLATFORM_DIR, WOLTS_DIR

log = logging.getLogger("woltspace.tunnel")

TUNNEL_STATE_FILE = SPACE_PLATFORM_DIR / "tunnel.json"

_tunnel_url: str = ""
_tunnel_domain: str = ""   # e.g. "woltspace.com" — parent domain for app subdomains
_tunnel_hostname: str = ""  # e.g. "jerpint.woltspace.com" — the lodge itself

# Lazy import helper — container/lib is on sys.path at runtime
_lib_imported = False

EXPOSURE_MODES = {"off", "temporary", "authenticated"}


def get_exposure_mode(env: dict | None = None) -> str:
    """Resolve the configured exposure mode.

    WOLTSPACE_EXPOSURE is the canonical setting. The legacy boolean remains
    supported so existing lodges keep their current behavior after upgrading.
    Fresh installs fail closed to ``off``.
    """
    values = env if env is not None else os.environ
    configured = values.get("WOLTSPACE_EXPOSURE", "").strip().lower()
    if configured:
        if configured not in EXPOSURE_MODES:
            log.error("invalid WOLTSPACE_EXPOSURE=%r; exposure disabled", configured)
            return "off"
        return configured

    legacy = values.get("WOLTSPACE_PUBLIC_TUNNEL", "").strip().lower()
    if legacy == "true":
        if values.get("CLOUDFLARE_TUNNEL_TOKEN") and values.get("CLOUDFLARE_TUNNEL_URL"):
            return "authenticated"
        return "temporary"
    return "off"


def _import_lib():
    global _lib_imported
    if not _lib_imported:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
        _lib_imported = True


def get_tunnel_url() -> str:
    return _tunnel_url


def get_tunnel_domain() -> str:
    """Parent domain for wildcard app subdomains (e.g. 'woltspace.com')."""
    return _tunnel_domain


def get_tunnel_hostname() -> str:
    """Lodge hostname (e.g. 'jerpint.woltspace.com') — excluded from app routing."""
    return _tunnel_hostname


def _parse_tunnel_domain(url: str):
    """Extract parent domain from tunnel URL for wildcard subdomain routing.

    "https://jerpint.woltspace.com" → hostname="jerpint.woltspace.com", domain="woltspace.com"
    """
    global _tunnel_domain, _tunnel_hostname
    if not url:
        return
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    _tunnel_hostname = hostname
    parts = hostname.split(".", 1)
    if len(parts) == 2:
        _tunnel_domain = parts[1]
        log.info(f"subdomain routing: *.{_tunnel_domain} (lodge: {_tunnel_hostname})")


def _read_state() -> dict:
    try:
        return json.loads(TUNNEL_STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_state(state: dict):
    TUNNEL_STATE_FILE.write_text(json.dumps(state))


def start_tunnel():
    """Start the lodge tunnel. Called once at server boot."""
    global _tunnel_url
    _import_lib()
    from tunnel import start_cloudflared, start_named_tunnel, stop_cloudflared

    mode = get_exposure_mode()
    if mode == "off":
        log.info("exposure disabled")
        return

    SPACE_PLATFORM_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any orphaned tunnel from a previous server run
    old_state = _read_state()
    old_pid = old_state.get("pid")
    if old_pid:
        stop_cloudflared(old_pid)

    tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    tunnel_url = os.environ.get("CLOUDFLARE_TUNNEL_URL")

    # Parse domain for wildcard subdomain routing
    _parse_tunnel_domain(tunnel_url or "")

    try:
        if mode == "authenticated":
            if not tunnel_token or not tunnel_url:
                log.error(
                    "authenticated exposure requires CLOUDFLARE_TUNNEL_TOKEN "
                    "and CLOUDFLARE_TUNNEL_URL; exposure disabled"
                )
                _write_state({"mode": mode, "status": "misconfigured"})
                return
            # Named tunnel — permanent URL, pre-configured on Cloudflare
            result = start_named_tunnel(token=tunnel_token, host_header=None)
            _tunnel_url = tunnel_url
            _write_state({"pid": result["pid"], "url": _tunnel_url, "type": "named", "mode": mode})
            log.info(f"named tunnel ready: {_tunnel_url}")
        else:
            # Quick tunnel — random URL, zero config
            result = start_cloudflared(port=7777, host_header=None)
            _tunnel_url = result["url"]
            _write_state({"pid": result["pid"], "url": _tunnel_url, "type": "quick", "mode": mode})
            log.info(f"quick tunnel ready: {_tunnel_url}")
    except RuntimeError as e:
        log.error(f"tunnel failed: {e}")
        _write_state({})


def stop_tunnel():
    """Stop the lodge tunnel. Called at server shutdown."""
    _import_lib()
    from tunnel import stop_cloudflared

    state = _read_state()
    pid = state.get("pid")
    if pid:
        stop_cloudflared(pid)

    TUNNEL_STATE_FILE.unlink(missing_ok=True)
