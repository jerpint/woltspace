"""Lodge tunnel — managed cloudflared process with auto-restart."""

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

from .config import SPACE_PLATFORM_DIR, WOLTS_DIR

log = logging.getLogger("woltspace.tunnel")

TUNNEL_URL_FILE = SPACE_PLATFORM_DIR / "tunnel-url"
LEGACY_TUNNEL_FILE = WOLTS_DIR / ".state" / "tunnel-url"
TUNNEL_LOG_FILE = SPACE_PLATFORM_DIR / "tunnel.log"

_tunnel_proc: subprocess.Popen | None = None
_tunnel_url: str = ""
_watch_task: asyncio.Task | None = None


def get_tunnel_url() -> str:
    return _tunnel_url


async def start_tunnel():
    """Start the cloudflared tunnel and begin watching it."""
    global _watch_task
    if os.environ.get("WOLTSPACE_PUBLIC_TUNNEL", "true").lower() != "true":
        log.info("tunnel disabled")
        return

    SPACE_PLATFORM_DIR.mkdir(parents=True, exist_ok=True)
    TUNNEL_URL_FILE.unlink(missing_ok=True)
    LEGACY_TUNNEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    LEGACY_TUNNEL_FILE.unlink(missing_ok=True)

    await _launch_tunnel()
    _watch_task = asyncio.create_task(_watch_tunnel())


async def stop_tunnel():
    """Stop the tunnel and watchdog."""
    global _watch_task, _tunnel_proc
    if _watch_task:
        _watch_task.cancel()
        try:
            await _watch_task
        except asyncio.CancelledError:
            pass
        _watch_task = None
    if _tunnel_proc:
        _tunnel_proc.terminate()
        try:
            _tunnel_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _tunnel_proc.kill()
        _tunnel_proc = None


async def _launch_tunnel():
    """Launch cloudflared and wait for the URL."""
    global _tunnel_proc, _tunnel_url

    log.info("starting tunnel...")

    log_fh = open(TUNNEL_LOG_FILE, "w")
    _tunnel_proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://localhost:7777"],
        stdout=log_fh,
        stderr=subprocess.STDOUT,
    )

    # Wait up to 30s for the URL
    for _ in range(30):
        await asyncio.sleep(1)
        if _tunnel_proc.poll() is not None:
            log.error("tunnel process exited unexpectedly")
            return
        try:
            content = TUNNEL_LOG_FILE.read_text()
            match = re.search(r'https://[^\s]*trycloudflare\.com', content)
            if match:
                _tunnel_url = match.group(0)
                TUNNEL_URL_FILE.write_text(_tunnel_url)
                LEGACY_TUNNEL_FILE.write_text(_tunnel_url)
                log.info(f"tunnel ready: {_tunnel_url}")
                return
        except Exception:
            pass

    log.warning("tunnel URL not found after 30s")


async def _watch_tunnel():
    """Watchdog: restart tunnel if it dies."""
    while True:
        await asyncio.sleep(10)
        if _tunnel_proc and _tunnel_proc.poll() is not None:
            log.warning(f"tunnel died (exit code {_tunnel_proc.returncode}), restarting...")
            await _launch_tunnel()
