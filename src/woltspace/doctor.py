"""Read-only native preflight with actionable remediation."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
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


def _tunnel_token_present(layout: RuntimeLayout) -> bool:
    if (os.environ.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip():
        return True
    try:
        from dotenv import dotenv_values

        return bool((dotenv_values(layout.wolts_dir / ".env").get(
            "CLOUDFLARE_TUNNEL_TOKEN"
        ) or "").strip())
    except (OSError, ImportError):
        return False


def _cli_path_check() -> DoctorCheck | None:
    """Warn when this console script will disappear in the next shell."""
    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name != "woltspace":
        return None
    if not invoked.is_absolute():
        located = shutil.which(str(invoked))
        if located:
            return DoctorCheck("cli-path", "pass", str(Path(located).parent))
        return None

    executable_dir = invoked.absolute().parent
    path_dirs = {
        Path(part).expanduser().absolute()
        for part in os.environ.get("PATH", "").split(os.pathsep)
        if part
    }
    if executable_dir in path_dirs:
        return DoctorCheck("cli-path", "pass", f"{executable_dir} is on PATH")
    return DoctorCheck(
        "cli-path",
        "warn",
        f"running woltspace from {executable_dir}, which is not on PATH",
        "Run `uv tool update-shell`, then open a new terminal.",
    )


def _path_python_check() -> DoctorCheck | None:
    """Describe an old ambient python without mistaking it for our runtime."""
    executable = shutil.which("python3")
    if not executable:
        return None
    try:
        output = subprocess.check_output(
            [executable, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
            text=True,
            timeout=5,
            stderr=subprocess.DEVNULL,
        ).strip()
        version = tuple(int(part) for part in output.split("."))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if version >= (3, 11):
        return DoctorCheck("path-python", "pass", f"{executable} is Python {output}")
    return DoctorCheck(
        "path-python",
        "warn",
        f"{executable} is Python {output}; platform helpers use {sys.executable}",
        "Keep WOLTSPACE_PYTHON set to the lodge interpreter; do not use this python3 for platform helpers.",
    )


def _default_harness_check() -> DoctorCheck | None:
    name = (os.environ.get("WOLTSPACE_DEFAULT_HARNESS") or "").strip()
    if not name:
        return None
    try:
        from harnesses import HARNESSES
    except ImportError:
        return DoctorCheck(
            "default-harness", "warn", f"cannot validate {name!r}",
            "Reinstall Woltspace so its harness registry is available.",
        )
    if name in HARNESSES:
        return DoctorCheck("default-harness", "pass", f"{name} is registered")
    registered = ", ".join(sorted(HARNESSES))
    return DoctorCheck(
        "default-harness", "warn",
        f"WOLTSPACE_DEFAULT_HARNESS={name!r} is not registered",
        f"Use one of: {registered}. The first-run harness question will remain visible.",
    )


_FALLBACK_HARNESSES = ("claude", "codex", "opencode", "pi", "hermes")


def _harness_table() -> dict:
    """The harness registry, or {} when it cannot be imported."""
    try:
        from harnesses import HARNESSES
    except ImportError:
        return {}
    return HARNESSES


def _auth_paths(home: Path) -> dict[str, Path]:
    codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex"))
    xdg_data = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    pi_home = Path(os.environ.get("PI_CODING_AGENT_DIR", home / ".pi" / "agent"))
    return {
        "claude": home / ".claude" / ".credentials.json",
        "codex": codex_home / "auth.json",
        "opencode": xdg_data / "opencode" / "auth.json",
        "pi": pi_home / "auth.json",
    }


def _claude_oauth_account_present(home: Path) -> bool:
    """Recognize Claude's non-secret account marker used with macOS Keychain."""
    if sys.platform != "darwin":
        return False
    try:
        config = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        isinstance(config, dict)
        and isinstance(config.get("oauthAccount"), dict)
        and bool(config["oauthAccount"])
    )


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
        f"(or point WOLTSPACE_WOLTS_DIR at the mount you use)."
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
    "(`WOLTSPACE_WOLTS_DIR=~/.woltspace/native-wolts woltspace start`), or set "
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


def _graft_runtime_lib(layout: RuntimeLayout) -> None:
    """Make the install's `container/lib` importable, as `serve` does.

    The harness registry lives there. Without the graft an installed CLI
    (uvx, uv tool) cannot import it, and every harness check falls back to
    names alone: no default-harness validation, no `auth_env` keys.
    """
    lib_dir = str(layout.runtime_lib)
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)


def _allowed_hosts_values(layout: RuntimeLayout) -> list[str]:
    """WOLTSPACE_ALLOWED_HOSTS as the lodge will see it: shell, then the data root .env."""
    raw = os.environ.get("WOLTSPACE_ALLOWED_HOSTS")
    if raw is None:
        env_file = layout.wolts_dir / ".env"
        try:
            from dotenv import dotenv_values

            raw = dotenv_values(env_file).get("WOLTSPACE_ALLOWED_HOSTS") if env_file.is_file() else None
        except (OSError, ImportError):
            raw = None
    return [name.strip() for name in (raw or "").split(",") if name.strip()]


def _allowed_hosts_check(names: list[str]) -> DoctorCheck | None:
    """Name WOLTSPACE_ALLOWED_HOSTS entries the lodge can never match."""
    import ipaddress

    def never_matches(name: str) -> bool:
        try:
            ipaddress.ip_address(name)
            return False  # a bare IP, IPv6 included, is matched as written
        except ValueError:
            pass
        lowered = name.lower().rstrip(".")
        return (not lowered or any(ch in name for ch in "/:*[]@ ") or lowered == "localhost"
                or lowered.endswith(".localhost"))

    bad = [name for name in names if never_matches(name)]
    if not bad:
        return None
    return DoctorCheck(
        "allowed-hosts", "warn",
        "WOLTSPACE_ALLOWED_HOSTS has entries the lodge ignores or can never match: "
        + ", ".join(bad),
        "Use exact host names only: no scheme, port, path, brackets or wildcards "
        "(IPv6 as fd00::1). localhost names are reserved.",
    )


# Kept in step with `server/content_hosts.py`: the host label prefix that
# names a wolt's site on the app domain. An app of that name is never served.
SITE_HOST_PREFIX = "site-"


def _reserved_app_names_check(layout: RuntimeLayout) -> DoctorCheck | None:
    """Apps whose names the gateway reserves for wolt sites (`site-<wolt>`)."""
    found = []
    for folder in ("apps", "projects"):
        for manifest in sorted((layout.wolts_dir / folder).glob("*/woltspace.json")):
            names = {manifest.parent.name}
            try:
                data = json.loads(manifest.read_text())
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    names.add(data["name"])
            except (OSError, json.JSONDecodeError):
                pass
            found.extend(sorted(name for name in names if name.lower().startswith(SITE_HOST_PREFIX)))
    if not found:
        return None
    return DoctorCheck(
        "app-names", "warn",
        f"app name(s) reserved for wolt sites, not served: {', '.join(dict.fromkeys(found))}",
        f"Give the app a name without the {SITE_HOST_PREFIX} prefix (directory and "
        f"woltspace.json). Its old host stays reserved: browsers may still hold its "
        f"storage and workers, so no site will ever be served there.",
    )


def _remote_access_check(layout: RuntimeLayout, tunnel: dict) -> DoctorCheck | None:
    """Say plainly what a published lodge's Access settings mean for it.

    A warning, not a failure: the way in is the owner's choice. The lodge
    still starts, and our own Cloudflare setup turns the check on. Reads the
    settings with the lodge's own loader, so doctor and server always agree.
    """
    hosts = _allowed_hosts_values(layout)
    if not tunnel.get("enabled") and not hosts:
        return None
    where = tunnel.get("url") or (", ".join(hosts) if hosts else "its public address")
    root = str(layout.install_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from server.access import load_access_settings
    except ImportError:
        return None
    try:
        settings = load_access_settings(layout.wolts_dir)
    except RuntimeError as exc:
        return DoctorCheck(
            "remote-access", "warn",
            f"the lodge's Access settings are unusable ({exc}), so it refuses every "
            "remote request",
            "Repair them with `woltspace access set`, or restore woltspace.json from a "
            "backup. This machine still reaches the lodge.",
        )
    if settings is not None:
        return DoctorCheck(
            "remote-access", "pass", "remote requests are checked with Cloudflare Access",
        )
    return DoctorCheck(
        "remote-access", "warn",
        "the lodge is published without its own Access check: anyone who reaches "
        f"{where} is treated as you, unless whatever guards that way in "
        "(e.g. Cloudflare Access) stops them first",
        "Run `woltspace access set` on this machine (or open Settings > Access) so the "
        "lodge verifies every remote request itself. The cloudflare skill explains "
        "where each value comes from.",
    )


def run_doctor(
    layout: RuntimeLayout, *, check_port: bool = True, as_entrypoint: bool = False
) -> list[DoctorCheck]:
    _graft_runtime_lib(layout)
    checks = []
    version = sys.version_info
    checks.append(DoctorCheck(
        "python",
        "pass" if version >= (3, 11) else "fail",
        f"Python {version.major}.{version.minor}.{version.micro}",
        "Install Python 3.11 or newer." if version < (3, 11) else "",
    ))

    cli_path = _cli_path_check()
    if cli_path is not None:
        checks.append(cli_path)
    path_python = _path_python_check()
    if path_python is not None:
        checks.append(path_python)

    from .lifecycle import tunnel_settings

    if _tunnel_token_present(layout) and not tunnel_settings(layout)["enabled"]:
        if tunnel_settings(layout)["kind"] != "named":
            checks.append(DoctorCheck(
                "public-tunnel", "warn",
                "CLOUDFLARE_TUNNEL_TOKEN is set but CLOUDFLARE_TUNNEL_URL is missing, "
                "so the lodge is not published",
                "Set CLOUDFLARE_TUNNEL_URL to the lodge's address and restart the lodge.",
            ))
        else:
            checks.append(DoctorCheck(
                "public-tunnel", "warn",
                "CLOUDFLARE_TUNNEL_TOKEN is configured but the tunnel is disabled",
                "Set WOLTSPACE_PUBLIC_TUNNEL=true and restart the lodge.",
            ))
    remote = _remote_access_check(layout, tunnel_settings(layout))
    if remote is not None:
        checks.append(remote)
    hosts_check = _allowed_hosts_check(_allowed_hosts_values(layout))
    if hosts_check is not None:
        checks.append(hosts_check)
    checks.append(check_connector_ledger(layout))
    reserved_apps = _reserved_app_names_check(layout)
    if reserved_apps is not None:
        checks.append(reserved_apps)
    default_harness = _default_harness_check()
    if default_harness is not None:
        checks.append(default_harness)

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
        f"Create a writable directory or set WOLTSPACE_WOLTS_DIR to one you own." if not writable else "",
    ))

    tmux = shutil.which("tmux")
    checks.append(DoctorCheck(
        "tmux",
        "pass" if tmux else "fail",
        tmux or "not found on PATH",
        "Install tmux and ensure it is on PATH." if not tmux else "",
    ))

    table = _harness_table()
    names = tuple(table) or _FALLBACK_HARNESSES
    harnesses = {name: shutil.which(name) for name in names}
    installed = {name: path for name, path in harnesses.items() if path}
    checks.append(DoctorCheck(
        "harness",
        "pass" if installed else "fail",
        ", ".join(f"{name}={path}" for name, path in installed.items()) or "none found",
        f"Install at least one supported CLI: {', '.join(names)}." if not installed else "",
    ))

    home = Path.home()
    auth = _auth_paths(home)
    authenticated = [name for name in installed if name in auth and auth[name].is_file()]
    # A harness that authenticates from the lodge environment declares the
    # variables it needs (`auth_env` in its table entry).
    for name in installed:
        if name in authenticated:
            continue
        keys = [key for key in table.get(name, {}).get("auth_env", ()) if os.environ.get(key)]
        if keys:
            authenticated.append(f"{name} ({keys[0]})")
    if (
        "claude" in installed
        and "claude" not in authenticated
        and _claude_oauth_account_present(home)
    ):
        authenticated.append("claude (macOS Keychain)")
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

    checks.append(DoctorCheck(
        "browser-terminal",
        "pass" if tmux else "fail",
        "embedded Python PTY bridge" if tmux else "tmux unavailable",
        "Install tmux so browser sessions can attach." if not tmux else "",
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


def check_connector_ledger(layout: RuntimeLayout) -> DoctorCheck:
    """Revocation debts a supervisor could not settle, or a ledger nobody can read."""
    from .handoff import DebtLedger

    status = DebtLedger(str(layout.platform_state)).status()
    if not status["readable"]:
        return DoctorCheck(
            "connector-ledger", "warn", f"revocation ledger unreadable: {status['error']}",
            remedy="Inspect .space/platform/run/debts.json; it is kept as it is until it reads, and recovery fails until then.",
        )
    if status["debts"]:
        owed = ", ".join(f"{i}/{g}" for i, g in status["debts"])
        return DoctorCheck(
            "connector-ledger", "warn", f"connector revocations still owed: {owed}",
            remedy="They are settled at the next `woltspace start`; `woltspace connector list` shows them.",
        )
    return DoctorCheck("connector-ledger", "pass", "no connector revocation owed")


def doctor_ok(checks: list[DoctorCheck]) -> bool:
    return all(check.ok for check in checks)
