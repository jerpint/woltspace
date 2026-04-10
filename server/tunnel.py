"""Lodge tunnel — starts cloudflared at server boot, writes URL to state."""

import logging
import os
from pathlib import Path

from .config import SPACE_PLATFORM_DIR, WOLTS_DIR

log = logging.getLogger("woltspace.tunnel")

TUNNEL_URL_FILE = SPACE_PLATFORM_DIR / "tunnel-url"
LEGACY_TUNNEL_FILE = WOLTS_DIR / ".state" / "tunnel-url"

_tunnel_pid: int | None = None
_tunnel_url: str = ""


def get_tunnel_url() -> str:
    return _tunnel_url


def start_tunnel():
    """Start the lodge tunnel. Called once at server boot."""
    global _tunnel_pid, _tunnel_url

    if os.environ.get("WOLTSPACE_PUBLIC_TUNNEL", "true").lower() != "true":
        log.info("tunnel disabled")
        return

    SPACE_PLATFORM_DIR.mkdir(parents=True, exist_ok=True)
    TUNNEL_URL_FILE.unlink(missing_ok=True)
    LEGACY_TUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEGACY_TUNNEL_FILE.unlink(missing_ok=True)

    # Reuse the shared cloudflared helper
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    from tunnel import start_cloudflared

    try:
        result = start_cloudflared(port=7777, host_header=None)
        _tunnel_url = result["url"]
        _tunnel_pid = result["pid"]

        TUNNEL_URL_FILE.write_text(_tunnel_url)
        LEGACY_TUNNEL_FILE.write_text(_tunnel_url)
        log.info(f"tunnel ready: {_tunnel_url}")
    except RuntimeError as e:
        log.error(f"tunnel failed: {e}")


def stop_tunnel():
    """Stop the lodge tunnel. Called at server shutdown."""
    global _tunnel_pid
    if not _tunnel_pid:
        return

    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
    from tunnel import stop_cloudflared

    stop_cloudflared(_tunnel_pid)
    _tunnel_pid = None
    TUNNEL_URL_FILE.unlink(missing_ok=True)
