"""Lodge tunnel — starts cloudflared at server boot, writes URL + PID to state."""

import json
import logging
import os
from pathlib import Path

from .config import SPACE_PLATFORM_DIR, WOLTS_DIR, WOLTSPACE_DIR

log = logging.getLogger("woltspace.tunnel")

TUNNEL_STATE_FILE = SPACE_PLATFORM_DIR / "tunnel.json"

_tunnel_url: str = ""
_tunnel_domain: str = ""   # e.g. "woltspace.com" — parent domain for app subdomains
_tunnel_hostname: str = ""  # e.g. "jerpint.woltspace.com" — the lodge itself

# Lazy import helper — container/lib is on sys.path at runtime
_lib_imported = False


def _import_lib():
    global _lib_imported
    if not _lib_imported:
        import sys
        sys.path.insert(0, str(WOLTSPACE_DIR / "container" / "lib"))
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
    """Stamp every tunnel record with the instance that started it.

    Without the stamp nothing can tell "my own tunnel" from "somebody else's",
    which is how a stray control plane came to delete the incumbent's state on
    its way out.
    """
    stamped = dict(state)
    if stamped:
        stamped.setdefault("instance_id", os.environ.get("WOLTSPACE_INSTANCE_ID", ""))
    TUNNEL_STATE_FILE.write_text(json.dumps(stamped))


def start_tunnel():
    """Start the lodge tunnel. Called once at server boot."""
    global _tunnel_url
    _import_lib()
    from tunnel import start_cloudflared, start_named_tunnel, stop_cloudflared

    if os.environ.get("WOLTSPACE_PUBLIC_TUNNEL", "true").lower() != "true":
        log.info("tunnel disabled")
        return

    SPACE_PLATFORM_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any orphaned tunnel from a previous server run. `stop_cloudflared`
    # validates that the pid is still cloudflared before signalling, so a
    # recycled pid is simply discarded rather than shot: the state file names a
    # number, which is not evidence that the process it named still exists.
    old_state = _read_state()
    old_pid = old_state.get("pid")
    if old_pid:
        if stop_cloudflared(old_pid):
            log.info(f"stopped orphaned tunnel (pid {old_pid})")
        else:
            log.info(f"discarding stale tunnel state (pid {old_pid} is not cloudflared)")

    tunnel_token = os.environ.get("CLOUDFLARE_TUNNEL_TOKEN")
    tunnel_url = os.environ.get("CLOUDFLARE_TUNNEL_URL")

    # Parse domain for wildcard subdomain routing
    _parse_tunnel_domain(tunnel_url or "")

    try:
        if tunnel_token and tunnel_url:
            # Named tunnel — permanent URL, pre-configured on Cloudflare
            result = start_named_tunnel(token=tunnel_token, host_header=None)
            _tunnel_url = tunnel_url
            _write_state({"pid": result["pid"], "url": _tunnel_url, "type": "named"})
            log.info(f"named tunnel ready: {_tunnel_url}")
        else:
            # Quick tunnel — random URL, zero config
            result = start_cloudflared(port=7777, host_header=None)
            _tunnel_url = result["url"]
            _write_state({"pid": result["pid"], "url": _tunnel_url, "type": "quick"})
            log.info(f"quick tunnel ready: {_tunnel_url}")
    except RuntimeError as e:
        log.error(f"tunnel failed: {e}")
        _write_state({})


def stop_tunnel():
    """Stop the lodge tunnel. Called at server shutdown.

    Only ever tears down a tunnel this instance started. An unstamped record
    predates the stamp and is treated as ours for compatibility; one carrying
    somebody else's id is left exactly as found.
    """
    _import_lib()
    from tunnel import stop_cloudflared

    state = _read_state()
    owner = state.get("instance_id")
    mine = os.environ.get("WOLTSPACE_INSTANCE_ID", "")
    if owner and mine and owner != mine:
        log.warning(
            f"leaving tunnel state alone: started by instance {owner}, not {mine}"
        )
        return

    pid = state.get("pid")
    if pid:
        stop_cloudflared(pid)

    TUNNEL_STATE_FILE.unlink(missing_ok=True)
