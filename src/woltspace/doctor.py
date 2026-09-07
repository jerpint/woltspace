"""Read-only native preflight with actionable remediation."""

from __future__ import annotations

import json
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


# Kept in step with `container/lib/harness_auth.py`, which the container boot
# and the server share. Duplicated as a literal rather than imported: doctor
# runs before any layout has put `container/lib` on the path.
CLAUDE_TOKEN_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def _claude_token_in_env() -> bool:
    """Whether Claude Code would start logged in from the environment alone."""
    return any((os.environ.get(name) or "").strip() for name in CLAUDE_TOKEN_VARS)


def _auth_paths(home: Path) -> dict[str, Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return {
        "claude": home / ".claude" / ".credentials.json",
        "codex": codex_home / "auth.json",
        "opencode": xdg_data / "opencode" / "auth.json",
    }


class MountError(RuntimeError):
    """A container run is missing a mount it cannot work without."""

    def __init__(self, check: "DoctorCheck"):
        self.check = check
        super().__init__(f"{check.detail}. {check.remedy}")


def _really_writable(path: Path) -> bool:
    """Try it. `os.access(W_OK)` answers yes for uid 0 even on a read-only mount."""
    probe = path / ".woltspace-write-probe"
    try:
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False


def container_mount_check(layout: RuntimeLayout) -> DoctorCheck:
    """The wolts directory is a host mount in container mode, never created here.

    Creating it would silently hand the user an empty, disposable data root
    inside the container — every wolt they own missing, with no error.
    """
    remedy = (
        f"Mount your wolts directory into the container: "
        f"`docker run -v \"$HOME/.woltspace/wolts:{layout.wolts_dir}\" ...` "
        f"(or point WOLTS_DIR at the mount you use)."
    )
    if not layout.wolts_dir.exists():
        return DoctorCheck(
            "mounts", "fail", f"{layout.wolts_dir} is not mounted", remedy
        )
    if not layout.wolts_dir.is_dir():
        return DoctorCheck(
            "mounts", "fail", f"{layout.wolts_dir} is not a directory", remedy
        )
    if not _really_writable(layout.wolts_dir):
        return DoctorCheck(
            "mounts", "fail", f"{layout.wolts_dir} is mounted read-only", remedy
        )
    return DoctorCheck("mounts", "pass", f"{layout.wolts_dir} is mounted and writable")


def ensure_container_mounts(layout: RuntimeLayout) -> None:
    """Fail fast with the remedy, rather than a traceback deeper in boot."""
    if layout.isolation == "host":
        return
    check = container_mount_check(layout)
    if not check.ok:
        raise MountError(check)


class DataRootConflict(RuntimeError):
    """This process was asked to serve a data root something else is using."""

    def __init__(self, check: "DoctorCheck"):
        self.check = check
        super().__init__(f"{check.detail} {check.remedy}")


ALLOW_SHARED_ENV = "WOLTSPACE_ALLOW_SHARED_DATA_ROOT"

SHARED_REMEDY = (
    "Stop that instance first, use a fresh data root "
    "(`WOLTS_DIR=~/.woltspace/native-wolts woltspace start`), or set "
    f"{ALLOW_SHARED_ENV}=1 if you really mean to share it."
)


CONTROL_PLANE_NEEDLE = "woltspace"
TUNNEL_NEEDLE = "cloudflared"


def _live_tunnel_owner(layout: RuntimeLayout, *, runner=None) -> int | None:
    """A cloudflared this data root is publishing through, right now.

    The pid alone proves nothing: a container reboot recycles pids fast, and a
    stale tunnel.json naming a recycled pid would otherwise look live forever.
    Confirm the process is actually cloudflared before believing the file.
    """
    from .processes import pid_runs_program

    try:
        state = json.loads((layout.platform_state / "tunnel.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    if state.get("instance_id") and state["instance_id"] == os.environ.get(
        "WOLTSPACE_INSTANCE_ID"
    ):
        return None  # our own tunnel, from this very instance
    pid = state.get("pid")
    if not isinstance(pid, int):
        return None
    kwargs = {"runner": runner} if runner is not None else {}
    # Executable identity, not a word in the argv: `tail -f x-cloudflared.log`
    # is not a tunnel, and our own logs are named exactly that way.
    return pid if pid_runs_program(pid, TUNNEL_NEEDLE, **kwargs) else None


def _running_session_count(layout: RuntimeLayout) -> int:
    """How many sessions this data root believes are alive right now."""
    try:
        entries = list(layout.wolts_dir.iterdir())
    except OSError:
        return 0
    running = 0
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        sessions = entry / ".state" / "sessions"
        if not sessions.is_dir():
            continue
        for record in sessions.glob("*.json"):
            try:
                data = json.loads(record.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("status") == "running":
                running += 1
    return running


def _owner_is_live_and_foreign(layout: RuntimeLayout, *, runner=None) -> "InstanceOwner | None":
    """An owner record that belongs to a control plane still running elsewhere.

    Three ways to be sure it is *not* a live foreigner, and they matter more
    than the ways to be sure it is — a false positive here bricks every restart:

    * it names this very instance;
    * its endpoint answers /health with its own instance id → live, and if that
      id is ours it is us;
    * its pid is dead, or alive but running something else entirely (a reboot
      recycled the number).

    A foreign *hostname* is not by itself proof of life. Container hostnames
    change on every `docker run`, so treating that as a conflict would refuse
    every rebuild on the container's own data root.
    """
    from .instance import read_health, read_owner
    from .processes import pid_argv_has_token

    owner = read_owner(layout)
    if owner is None:
        return None
    if owner.instance_id and owner.instance_id == os.environ.get("WOLTSPACE_INSTANCE_ID"):
        return None

    health = read_health(owner.endpoint or layout.endpoint)
    if health and health.get("instance_id") == owner.instance_id:
        return owner  # it is up and answering: unambiguously live
    if health:
        return None  # something else holds that endpoint; the record is stale

    kwargs = {"runner": runner} if runner is not None else {}
    # A whole argv token, so a path that merely mentions woltspace is not a
    # control plane. Same defect class as the two matchers above.
    if pid_argv_has_token(owner.pid, CONTROL_PLANE_NEEDLE, **kwargs):
        return owner
    return None


def shared_data_root_check(
    layout: RuntimeLayout, *, as_entrypoint: bool = False, runner=None
) -> DoctorCheck | None:
    """Report when this data root is already in use by something else.

    Deliberately *not* scoped to an isolation mode, and — since the review —
    not skipped for entrypoints either. Being the entrypoint means being
    deliberate, not being alone: a second `woltspace start` on another port is
    every bit as deliberate as the first, and across a Docker bind mount the
    flock cannot tell them apart.

    What changes with `as_entrypoint` is which evidence counts. An entrypoint
    is *expected* to find its own leftovers — sessions it will re-adopt, a
    stale owner record from the run it is replacing — so only live, foreign
    evidence stops it. A guest is stopped by anything suggesting use at all.
    """
    if _sharing_allowed():
        return None

    entrypoint = as_entrypoint or layout.is_entrypoint
    severity = "warn" if (layout.isolation == "host" and not entrypoint) else "fail"

    owner = _owner_is_live_and_foreign(layout, runner=runner)
    if owner is not None:
        return DoctorCheck(
            "data-root-sharing",
            severity,
            f"{layout.wolts_dir} is owned by a live control plane "
            f"(pid {owner.pid} on {owner.hostname}, instance {owner.instance_id}, "
            f"serving {owner.endpoint}).",
            SHARED_REMEDY,
        )

    tunnel_pid = _live_tunnel_owner(layout, runner=runner)
    if tunnel_pid:
        return DoctorCheck(
            "data-root-sharing",
            severity,
            f"{layout.wolts_dir} is publishing through a live tunnel "
            f"(cloudflared pid {tunnel_pid}), so a control plane is already using it.",
            SHARED_REMEDY,
        )

    if entrypoint:
        # Running sessions are what an entrypoint exists to re-adopt. They are
        # evidence of a previous run, not of a concurrent one.
        return None

    running = _running_session_count(layout)
    if running:
        return DoctorCheck(
            "data-root-sharing",
            severity,
            f"{layout.wolts_dir} has {running} session(s) marked running, so a "
            f"control plane is already using it.",
            SHARED_REMEDY,
        )
    return None


def _sharing_allowed() -> bool:
    return os.environ.get(ALLOW_SHARED_ENV, "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def ensure_data_root_available(
    layout: RuntimeLayout, *, as_entrypoint: bool = False, runner=None
) -> None:
    """Refuse to serve someone else's live data root. Runs even with --no-doctor.

    This is the guard, not the doctor check: `serve` is normally invoked with
    `--no-doctor`, so a check that only runs inside doctor protects nothing.
    """
    check = shared_data_root_check(layout, as_entrypoint=as_entrypoint, runner=runner)
    if check is not None and check.status == "fail":
        raise DataRootConflict(check)


def run_doctor(
    layout: RuntimeLayout, *, check_port: bool = True, as_entrypoint: bool = False
) -> list[DoctorCheck]:
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
    # A token in the environment is auth too — Claude Code prefers it over the
    # file. Reporting "no supported auth file detected" at a host that is in
    # fact logged in sends people to re-run a login they do not need. It is
    # named as a token rather than folded in silently: presence is not validity,
    # and someone whose sessions fail while doctor says "pass" needs to know
    # which credential answered before they can remove the dead one.
    if "claude" in installed and "claude" not in authenticated and _claude_token_in_env():
        authenticated.append("claude (env token)")
    checks.append(DoctorCheck(
        "host-auth",
        "pass" if authenticated else "warn",
        ", ".join(authenticated) if authenticated else "no supported auth file detected",
        "Log in with the selected harness; Woltspace will use its existing host auth."
        if not authenticated else "",
    ))

    # The browser terminal needs the pty bridge. Plan it as the entrypoint
    # would, so doctor reports what `woltspace start` will actually do.
    from .channels import TuiBridgeConnector

    bridge = TuiBridgeConnector().plan(layout, {**os.environ, "WOLTSPACE_ENTRYPOINT": "1"})
    checks.append(DoctorCheck(
        "tui-bridge",
        "pass" if bridge.enabled else "warn",
        bridge.detail,
        "" if bridge.enabled else bridge.remedy,
    ))

    if layout.isolation != "host":
        checks.append(container_mount_check(layout))

    sharing = shared_data_root_check(layout, as_entrypoint=as_entrypoint)
    if sharing is not None:
        checks.append(sharing)

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
