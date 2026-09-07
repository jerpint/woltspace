"""Portable process inspection shared by the reaper and the data-root guard.

`/proc/<pid>/cmdline` does not exist on macOS, which is the platform this ships
to. `ps -o command= -p <pid>` is the portable form and suppresses the header on
both GNU ps (Linux/container) and BSD ps (macOS/native).
"""

from __future__ import annotations

import os
import subprocess


def default_ps_bin() -> str:
    return os.environ.get("WOLTSPACE_PS_BIN", "ps")


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def process_command(pid: int, *, ps_bin: str | None = None, runner=subprocess.run) -> str:
    """The full command line of a pid, or "" if it cannot be read."""
    if pid <= 0:
        return ""
    try:
        result = runner(
            # -ww: unlimited output width. Without it ps truncates the command
            # to the terminal width, so a long argv silently stops matching
            # whenever the window is narrow (or absent, as under a test runner).
            [ps_bin or default_ps_bin(), "-ww", "-o", "command=", "-p", str(pid)],
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
    """The basename of the program `pid` is executing, via `ps -o comm=`.

    `comm` is the executable rather than the argument vector, which is what
    makes it an identity. macOS reports a full path and Linux a bare name, so
    take the basename of either.
    """
    if pid <= 0:
        return ""
    try:
        result = runner(
            [ps_bin or default_ps_bin(), "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    return os.path.basename(stdout.strip())


def pid_runs_program(
    pid: int, program: str, *, ps_bin: str | None = None, runner=subprocess.run
) -> bool:
    """True only when `pid` is alive and the program it runs *is* `program`.

    Not "mentions": `tail -f something-cloudflared.log` mentions cloudflared,
    and our own tunnel logs are named that way.
    """
    if not pid_alive(pid):
        return False
    return process_executable(pid, ps_bin=ps_bin, runner=runner) == program


def pid_argv_has_token(
    pid: int, token: str, *, ps_bin: str | None = None, runner=subprocess.run
) -> bool:
    """True when `pid` is alive and `token` appears as a whole argv token.

    Whole token, not substring — `woltspace.log` is not `woltspace`.
    """
    if not pid_alive(pid):
        return False
    command = process_command(pid, ps_bin=ps_bin, runner=runner)
    return bool(command) and token in command.split()
