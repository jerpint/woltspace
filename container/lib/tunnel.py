"""Shared cloudflared tunnel helpers.

Used by:
- apps.py (per-app tunnels)
- server/tunnel.py (lodge tunnel)
"""

import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path


def start_cloudflared(port: int, host_header: str | None = "localhost") -> dict:
    """Start a cloudflared quick tunnel to a local port.

    Args:
        port: local port to tunnel to
        host_header: value for --http-host-header (None to skip)

    Returns:
        {"url": str, "pid": int, "log_file": str}

    Raises:
        RuntimeError: if tunnel fails to start within 15s
    """
    log_file = tempfile.mktemp(suffix="-cloudflared.log")
    cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
    if host_header:
        cmd += ["--http-host-header", host_header]

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Poll log for tunnel URL (up to 15s)
    url = ""
    for _ in range(30):
        time.sleep(0.5)
        if proc.poll() is not None:
            raise RuntimeError(f"cloudflared exited with code {proc.returncode}")
        try:
            with open(log_file) as f:
                content = f.read()
            m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", content)
            if m:
                url = m.group(0)
                break
        except Exception:
            pass

    if not url:
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError("cloudflared tunnel failed to start — is cloudflared installed?")

    return {"url": url, "pid": proc.pid, "log_file": log_file}


def stop_cloudflared(pid: int) -> bool:
    """Stop a cloudflared process by PID. Returns True if it was alive."""
    if not _is_pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False
