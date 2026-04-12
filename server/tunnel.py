"""Lodge tunnel — starts cloudflared at server boot, writes URL + PID to state."""

import json
import logging
import os
from pathlib import Path

from .config import SPACE_PLATFORM_DIR, WOLTS_DIR

log = logging.getLogger("woltspace.tunnel")

TUNNEL_STATE_FILE = SPACE_PLATFORM_DIR / "tunnel.json"

_tunnel_url: str = ""

# Lazy import helper — container/lib is on sys.path at runtime
_lib_imported = False


def _import_lib():
    global _lib_imported
    if not _lib_imported:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
        _lib_imported = True


def get_tunnel_url() -> str:
    return _tunnel_url


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
    from tunnel import start_cloudflared, stop_cloudflared

    if os.environ.get("WOLTSPACE_PUBLIC_TUNNEL", "true").lower() != "true":
        log.info("tunnel disabled")
        return

    SPACE_PLATFORM_DIR.mkdir(parents=True, exist_ok=True)

    # Kill any orphaned tunnel from a previous server run
    old_state = _read_state()
    old_pid = old_state.get("pid")
    if old_pid:
        stop_cloudflared(old_pid)

    try:
        result = start_cloudflared(port=7777, host_header=None)
        _tunnel_url = result["url"]

        _write_state({"pid": result["pid"], "url": _tunnel_url})
        log.info(f"tunnel ready: {_tunnel_url}")
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
