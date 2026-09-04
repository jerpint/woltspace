"""Read-only native preflight with actionable remediation."""

from __future__ import annotations

import os
import shutil
import socket
import sys
from dataclasses import dataclass
from pathlib import Path

from .layout import RuntimeLayout


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    detail: str
    remedy: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"

    def to_record(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "remedy": self.remedy or None,
        }


def _writable_ancestor(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.exists() else None


def _port_available(host: str, port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
        return True
    except OSError:
        return False


def _auth_paths(home: Path) -> dict[str, Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return {
        "claude": home / ".claude" / ".credentials.json",
        "codex": codex_home / "auth.json",
        "opencode": xdg_data / "opencode" / "auth.json",
    }


def run_doctor(layout: RuntimeLayout, *, check_port: bool = True) -> list[DoctorCheck]:
    checks = []
    version = sys.version_info
    checks.append(DoctorCheck(
        "python",
        "pass" if version >= (3, 11) else "fail",
        f"Python {version.major}.{version.minor}.{version.micro}",
        "Install Python 3.11 or newer." if version < (3, 11) else "",
    ))

    required_assets = (
        layout.install_root / "server",
        layout.install_root / "container" / "lib" / "sessions.py",
        layout.install_root / "public" / "static",
        layout.install_root / "templates" / "home.html",
    )
    missing = [str(path) for path in required_assets if not path.exists()]
    checks.append(DoctorCheck(
        "package",
        "fail" if missing else "pass",
        "runtime and web assets present" if not missing else f"missing: {', '.join(missing)}",
        "Reinstall the woltspace wheel." if missing else "",
    ))

    ancestor = _writable_ancestor(layout.wolts_dir)
    writable = bool(ancestor and ancestor.is_dir() and os.access(ancestor, os.W_OK))
    checks.append(DoctorCheck(
        "data-root",
        "pass" if writable else "fail",
        f"{layout.wolts_dir} (nearest existing parent: {ancestor})",
        f"Create a writable directory or set WOLTS_DIR to one you own." if not writable else "",
    ))

    tmux = shutil.which("tmux")
    checks.append(DoctorCheck(
        "tmux",
        "pass" if tmux else "fail",
        tmux or "not found on PATH",
        "Install tmux and ensure it is on PATH." if not tmux else "",
    ))

    harnesses = {name: shutil.which(name) for name in ("claude", "codex", "opencode")}
    installed = {name: path for name, path in harnesses.items() if path}
    checks.append(DoctorCheck(
        "harness",
        "pass" if installed else "fail",
        ", ".join(f"{name}={path}" for name, path in installed.items()) or "none found",
        "Install at least one supported CLI: claude, codex, or opencode." if not installed else "",
    ))

    home = Path.home()
    auth = _auth_paths(home)
    authenticated = [name for name in installed if auth[name].is_file()]
    checks.append(DoctorCheck(
        "host-auth",
        "pass" if authenticated else "warn",
        ", ".join(authenticated) if authenticated else "no supported auth file detected",
        "Log in with the selected harness; Woltspace will use its existing host auth."
        if not authenticated else "",
    ))

    if check_port:
        available = _port_available(layout.host, layout.port)
        checks.append(DoctorCheck(
            "port",
            "pass" if available else "fail",
            f"{layout.host}:{layout.port} is {'available' if available else 'already in use'}",
            "Stop the existing service or choose --port <free-port>." if not available else "",
        ))
    return checks


def doctor_ok(checks: list[DoctorCheck]) -> bool:
    return all(check.ok for check in checks)
