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


CLOUDFLARED_NEEDLE = "cloudflared"


def process_command(pid: int, *, ps_bin: str | None = None, runner=subprocess.run) -> str:
    """The full command line of a pid, on Linux and macOS alike.

    `-ww` is load-bearing: without it ps truncates to the terminal width, so a
    long argv stops matching whenever the window is narrow — or absent.
    """
    if pid is None or pid <= 0:
        return ""
    binary = ps_bin or os.environ.get("WOLTSPACE_PS_BIN", "ps")
    try:
        result = runner(
            [binary, "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    return stdout.strip()


def process_executable(pid: int, *, ps_bin: str | None = None, runner=subprocess.run) -> str:
    """The basename of what `pid` is executing, via `ps -o comm=`.

    `comm` is the executable, not the argument vector — which is the whole
    point. macOS reports a full path here and Linux a bare name, so take the
    basename of either.
    """
    if pid is None or pid <= 0:
        return ""
    binary = ps_bin or os.environ.get("WOLTSPACE_PS_BIN", "ps")
    try:
        result = runner(
            [binary, "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    return os.path.basename(stdout.strip())


def is_cloudflared(pid: int, *, ps_bin: str | None = None, runner=subprocess.run) -> bool:
    """Whether `pid` is alive *and* the program it runs is cloudflared.

    A pid recorded in state is not evidence that the thing it named is still
    there — pids are recycled hard and fast across a reboot. Nor is the word
    "cloudflared" appearing somewhere in a command line: `tail -f
    something-cloudflared.log` contains it, and this helper's own log files are
    named `*-cloudflared.log`, so that collision is ordinary rather than
    contrived. Identity is the executable, matched whole.
    """
    if not _is_pid_alive(pid):
        return False
    return process_executable(pid, ps_bin=ps_bin, runner=runner) == CLOUDFLARED_NEEDLE


def stop_cloudflared(pid: int, *, ps_bin: str | None = None, runner=subprocess.run) -> bool:
    """Stop a cloudflared process by PID. Returns True if it was signalled.

    Validates at the point of the kill, not only at whatever decided to call
    this. A caller acting on stale state would otherwise SIGTERM whichever
    innocent process inherited the number.
    """
    if not is_cloudflared(pid, ps_bin=ps_bin, runner=runner):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def start_named_tunnel(token: str, host_header: str | None = None) -> dict:
    """Start a named cloudflared tunnel with a pre-configured token.

    Args:
        token: tunnel token from Cloudflare
        host_header: value for --http-host-header (None to skip)

    Returns:
        {"pid": int, "log_file": str}

    Raises:
        RuntimeError: if tunnel fails to start
    """
    log_file = tempfile.mktemp(suffix="-cloudflared.log")
    cmd = ["cloudflared", "tunnel", "run", "--token", token]
    if host_header:
        cmd += ["--http-host-header", host_header]

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd,
            stdout=lf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # Give it a moment — check it didn't crash immediately
    time.sleep(2)
    if proc.poll() is not None:
        raise RuntimeError(f"cloudflared exited with code {proc.returncode}")

    return {"pid": proc.pid, "log_file": log_file}


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, TypeError):
        return False
