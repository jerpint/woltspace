"""Sharing an app with anyone who has its link: an explicit opt-in, per app.

Apps are the owner's. Sharing one lets anyone with the link use that app, and
do whatever the app can do; nothing else. The lodge, wolt sites and other apps
are never shared, and a visitor is never the owner: the lodge tells the Mill
`public`, and filters the visitor's request as it does the owner's.

Two links, both through the lodge's port and the Mill:

- **A quick tunnel**, which needs no domain: `cloudflared tunnel --url
  http://localhost:<lodge port> --http-host-header <app>--<token>.share.localhost:<lodge port>`.
  Each share has a fresh random token, and that host is only ever public: the
  lodge serves it to this app as `public` and never as the owner, and refuses
  it once the token isn't the app's current share. A tunnel that outlives its
  share reaches nothing, even if the app is shared again. Its trycloudflare
  address changes when the tunnel restarts.
- **The lodge's domain**, `https://<app>.<domain>/`, once the owner's Access
  policy lets visitors through for that host. The lodge can't change Access.

State is machine-local, under `<data root>/.space/run/sharing/` (folder 0700,
files 0600): `shared.json` holds the shares and the retired tunnels not yet
confirmed stopped. Every share and unshare is appended to
`<data root>/.space/logs/sharing.log` (0600). A share goes live only after its
log entry is written; an unshare takes effect first and is logged after, so
revocation never waits on the log.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from tunnel import is_cloudflared, process_command, start_cloudflared, stop_cloudflared

PUBLIC = "public"
WARNING = ("Anyone with this link can use this app, and do whatever the app can do. "
           "The lodge, wolt sites and other apps stay private.")
SHARE_HOST_SUFFIX = ".share.localhost"
_SHARE_HOST_RE = re.compile(r"^(?P<name>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)--(?P<token>[0-9a-f]{24})"
                            r"\.share\.localhost(?::\d+)?$")
STOP_WAIT_SECONDS = 2.0
# A launch the sweep leaves alone: cloudflared takes up to 15 s to start.
LAUNCH_GRACE_SECONDS = 300
# This lodge process. A launch belongs to the process that wrote it; one
# lodge serves a data root at a time, so a launch from any other instance (an
# earlier lodge, before a crash or restart) is never in flight here.
INSTANCE = secrets.token_hex(8)


# --- Storage -----------------------------------------------------------------------

def sharing_dir(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "run" / "sharing"


def _store(wolts_dir) -> Path:
    return sharing_dir(wolts_dir) / "shared.json"


def log_path(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "logs" / "sharing.log"


class Corrupt(Exception):
    """shared.json exists but isn't a valid sharing state."""


_TOKEN_RE = re.compile(r"[0-9a-f]{24}")
_LAUNCH_ID_RE = re.compile(r"[0-9a-f]{16}")
_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def _is_pid(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_url(value) -> bool:
    return value is None or (isinstance(value, str) and value.startswith("https://") and " " not in value)


def _is_time(value) -> bool:
    import math
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _check(data) -> dict:
    """The state exactly as written, or Corrupt: every field a reader uses is
    checked, and nothing is normalised away."""
    if not isinstance(data, dict) or set(data) - {"shares", "retired", "launching"}:
        raise Corrupt("not a sharing state")
    shares, retired, launching = data.get("shares", {}), data.get("retired", []), data.get("launching", [])
    if not isinstance(shares, dict) or not isinstance(retired, list) or not isinstance(launching, list):
        raise Corrupt("shares, retired or launching has the wrong type")
    for name, record in shares.items():
        ok = (isinstance(name, str) and _NAME_RE.fullmatch(name) and isinstance(record, dict)
              and isinstance(record.get("token"), str) and _TOKEN_RE.fullmatch(record["token"])
              and (record.get("tunnel_pid") is None or _is_pid(record.get("tunnel_pid")))
              and _is_url(record.get("tunnel_url")) and _is_url(record.get("domain_url"))
              and all(isinstance(record.get(k, ""), str) for k in ("by", "said_by", "since")))
        if not ok:
            raise Corrupt(f"bad share record for {name!r}")
    for entry in retired:
        if not (isinstance(entry, dict) and isinstance(entry.get("name"), str) and _NAME_RE.fullmatch(entry["name"])
                and _is_pid(entry.get("pid")) and isinstance(entry.get("token"), str)
                and _TOKEN_RE.fullmatch(entry["token"])):
            raise Corrupt("bad retired entry")
    for entry in launching:
        if not (isinstance(entry, dict) and isinstance(entry.get("id"), str) and _LAUNCH_ID_RE.fullmatch(entry["id"])
                and isinstance(entry.get("name"), str) and _NAME_RE.fullmatch(entry["name"])
                and isinstance(entry.get("token"), str) and _TOKEN_RE.fullmatch(entry["token"])
                and _is_time(entry.get("at")) and isinstance(entry.get("instance"), str)
                and _LAUNCH_ID_RE.fullmatch(entry["instance"])):
            raise Corrupt("bad launching entry")
    return {"shares": shares, "retired": retired, "launching": launching}


def state_problem(wolts_dir) -> str | None:
    """Why the current shared.json is invalid, or None (for `woltspace doctor`)."""
    try:
        _read(wolts_dir, strict=True)
    except Corrupt as exc:
        return str(exc)
    return None


def _read(wolts_dir, *, strict: bool = False) -> dict:
    """The sharing state. An invalid file reads as no shares (every app
    private) but is never written over: `_locked` moves it aside first."""
    try:
        text = _store(wolts_dir).read_text()
    except FileNotFoundError:
        return {"shares": {}, "retired": [], "launching": []}
    except OSError as exc:
        if strict:
            raise Corrupt(str(exc)) from exc
        return {"shares": {}, "retired": [], "launching": []}
    try:
        return _check(json.loads(text))
    except (ValueError, Corrupt) as exc:
        if strict:
            raise Corrupt(str(exc)) from exc
        return {"shares": {}, "retired": [], "launching": []}


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(wolts_dir, data: dict) -> None:
    """Replace the state durably: the new file is synced before it replaces
    the old, and the folder after, so an acknowledged change survives a crash."""
    path = _store(wolts_dir)
    _check(data)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(wolts_dir):
    directory = sharing_dir(wolts_dir)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(directory / ".lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            _read(wolts_dir, strict=True)
        except Corrupt as exc:
            # Keep it for the owner, and start clean: shares fail closed to
            # private, and the orphan sweep stops any tunnel it tracked.
            aside = _store(wolts_dir).with_name(
                f"shared.json.corrupt-{time.strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(4)}")
            os.replace(_store(wolts_dir), aside)
            _fsync_dir(directory)
            print(f"[sharing] {_store(wolts_dir)} was invalid ({exc}); kept as {aside.name}")
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _log(wolts_dir, event: str, name: str, **fields) -> None:
    """Append one line to the sharing log, private from its first byte. Raises
    when it can't: a share must not go live unlogged."""
    path = log_path(wolts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event, "app": name, **fields}
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        text = json.dumps(line) + "\n"
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            text = "\n" + text          # a partial line before this one stays its own line
        data = memoryview(text.encode())
        while data:
            written = os.write(fd, data)
            if written <= 0:
                raise OSError("sharing log: nothing written")
            data = data[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)       # the new file's entry, too


def _log_quietly(wolts_dir, event: str, name: str, **fields) -> bool:
    try:
        _log(wolts_dir, event, name, **fields)
        return True
    except OSError as exc:
        print(f"[sharing] {name}: could not log {event}: {exc}")
        return False


def corrupt_files(wolts_dir) -> list[Path]:
    """Unreadable sharing states moved aside, for `woltspace doctor`."""
    directory = sharing_dir(wolts_dir)
    return sorted(directory.glob("shared.json.corrupt-*")) if directory.is_dir() else []


# --- Reading -----------------------------------------------------------------------

def shared(wolts_dir, name: str) -> dict | None:
    """The app's share record, or None when it's private."""
    return _read(wolts_dir)["shares"].get(name)


def all_shared(wolts_dir) -> dict[str, dict]:
    return _read(wolts_dir)["shares"]


def share_host(name: str, token: str, lodge_port: int) -> str:
    return f"{name}--{token}{SHARE_HOST_SUFFIX}:{lodge_port}"


def parse_share_host(host: str):
    """(app, token) for a quick tunnel's share host, None for anything else."""
    match = _SHARE_HOST_RE.match((host or "").strip().lower())
    return (match["name"], match["token"]) if match else None


def is_share_host_shaped(host: str) -> bool:
    """Anything under `.share.localhost`: refused unless it's a current share."""
    hostname = (host or "").strip().lower()
    if hostname.count(":") == 1:
        hostname = hostname.rsplit(":", 1)[0]
    hostname = hostname.rstrip(".")
    return hostname == SHARE_HOST_SUFFIX[1:] or hostname.endswith(SHARE_HOST_SUFFIX)


def current_share(wolts_dir, name: str, token: str) -> dict | None:
    """The app's record if `token` is its current share's, else None."""
    record = shared(wolts_dir, name)
    if record is None or not secrets.compare_digest(str(record.get("token", "")), token):
        return None
    return record


def public_origins(record: dict) -> set[str]:
    """The exact origins of a shared app's public pages."""
    return {url for url in ((record or {}).get("tunnel_url"), (record or {}).get("domain_url")) if url}


# --- Tunnels -----------------------------------------------------------------------

def is_sharing_tunnel(pid, name: str, lodge_port: int, token: str, *, command=process_command,
                      cloudflared=is_cloudflared) -> bool:
    """Whether a pid is this share's tunnel: a cloudflared whose command targets
    the lodge's port with this share's host."""
    if not pid or not cloudflared(pid):
        return False
    try:
        parts = command(pid).split()
    except Exception:
        return False

    def arg(flag):
        return parts[parts.index(flag) + 1] if flag in parts[:-1] else None
    return (arg("--url") == f"http://localhost:{lodge_port}"
            and arg("--http-host-header") == share_host(name, token, lodge_port))


def _stop_and_confirm(pid, name, lodge_port, token, *, stop, is_ours) -> bool:
    """Stop a share's tunnel; True once it's confirmed gone (or was never ours)."""
    if not is_ours(pid, name, lodge_port, token):
        return True
    stop(pid)
    deadline = time.monotonic() + STOP_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not is_ours(pid, name, lodge_port, token):
            return True
        time.sleep(0.1)
    return False


def _cloudflared_processes() -> list[tuple[int, str]]:
    try:
        out = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and "cloudflared" in command:
            found.append((int(pid), command))
    return found


def sweep_orphans(wolts_dir, lodge_port: int, *, processes=None, stop=None, is_ours=None) -> list[int]:
    """Stop any sharing tunnel for this lodge whose token isn't a current
    share's: one a crash left untracked, or whose debt couldn't be saved. Such
    a tunnel reaches nothing (the lodge refuses its host), but it shouldn't run."""
    processes = processes or _cloudflared_processes
    stop = stop or stop_cloudflared
    is_ours = is_ours or is_sharing_tunnel
    candidates = []
    for pid, command in processes():
        parts = command.split()
        if "--http-host-header" not in parts[:-1]:
            continue
        host = parts[parts.index("--http-host-header") + 1]
        parsed = parse_share_host(host)
        if parsed is not None and host.endswith(f":{lodge_port}"):
            candidates.append((pid, parsed))
    stopped = []
    # Which tokens are current, and the signals, under one lock: a share can't
    # be published between the check and the stop. An expired launch is
    # dropped here, and its starter can no longer publish (see `share`).
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        now = time.time()
        # A launch counts only while its process lives and within the grace:
        # one a crashed or earlier lodge left behind is dropped, so its
        # tunnel is swept, and its starter (if any) can no longer publish.
        live = [r for r in data["launching"] if _launch_live(r, now)]
        if live != data["launching"]:
            data["launching"] = live
            _write(wolts_dir, data)
        # Kept: a share's own recorded tunnel, and any launch in flight. A
        # tunnel with a current token but another pid (a restart whose result
        # was never recorded) is an orphan too.
        recorded = {(name, r.get("token", ""), r.get("tunnel_pid")) for name, r in data["shares"].items()}
        launching = {(r.get("name", ""), r.get("token", "")) for r in data["launching"]}
        for pid, (name, token) in candidates:
            if ((name, token, pid) in recorded or (name, token) in launching
                    or not is_ours(pid, name, lodge_port, token)):
                continue
            if _stop_and_confirm(pid, name, lodge_port, token, stop=stop, is_ours=is_ours):
                stopped.append(pid)
            else:
                # Still running: tracked as debt, so doctor fails until it's gone.
                if not any(r.get("pid") == pid for r in data["retired"]):
                    _retire(data, name, {"tunnel_pid": pid, "token": token})
                _write(wolts_dir, data)
    return stopped


def settle_retired(wolts_dir, lodge_port: int, *, stop=None, is_ours=None) -> list[dict]:
    """Stop every retired tunnel not yet confirmed stopped; return the ones
    still running. A retired tunnel reaches nothing (its token is spent), but
    it's tracked until it's gone."""
    stop = stop or stop_cloudflared
    is_ours = is_ours or is_sharing_tunnel
    with _locked(wolts_dir):
        pending = list(_read(wolts_dir)["retired"])
    still = [r for r in pending
             if not _stop_and_confirm(r.get("pid"), r.get("name", ""), lodge_port, r.get("token", ""),
                                      stop=stop, is_ours=is_ours)]
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        settled = [r for r in pending if r not in still]
        data["retired"] = [r for r in data["retired"] if r not in settled]
        _write(wolts_dir, data)
        return list(data["retired"])


def _new_launch(name: str, token: str) -> dict:
    """A launch in flight, owned by this process: when this process is gone,
    so is the launch, and the sweep may stop its tunnel."""
    return {"id": secrets.token_hex(8), "name": name, "token": token, "at": time.time(), "instance": INSTANCE}


def _launch_live(entry: dict, now: float) -> bool:
    return entry["instance"] == INSTANCE and now - entry["at"] < LAUNCH_GRACE_SECONDS


def _retire(data: dict, name: str, record: dict) -> None:
    if record.get("tunnel_pid"):
        data["retired"].append({"name": name, "pid": record["tunnel_pid"], "token": record.get("token", ""),
                                "since": datetime.now(timezone.utc).isoformat(timespec="seconds")})


# --- Sharing -----------------------------------------------------------------------

def share(wolts_dir, name: str, lodge_port: int, *, by: str, said_by: str = "",
          domain_url: str | None = None, start=None, stop=None, is_ours=None) -> dict:
    """Share an app; return its record. Sharing a shared app returns it as is.

    Starts a quick tunnel for the app. Without one, the share holds only when
    there's a domain address; otherwise nothing is shared and RuntimeError says
    why. The log entry is written before the share goes live: if it can't be,
    nothing is shared.
    """
    if not _NAME_RE.fullmatch(name):
        raise RuntimeError(f"nothing shared: an app's address can't hold the name {name!r}. "
                           "Rename it with only letters, digits and dashes, then share it")
    start, stop = start or start_cloudflared, stop or stop_cloudflared
    is_ours = is_ours or is_sharing_tunnel
    existing = shared(wolts_dir, name)
    if existing is not None:
        return existing
    token = secrets.token_hex(12)
    launch = _new_launch(name, token)
    try:
        with _locked(wolts_dir):
            data = _read(wolts_dir)
            data["launching"].append(launch)
            _write(wolts_dir, data)
    except OSError as exc:
        raise RuntimeError(f"nothing shared: could not record the share: {exc}") from exc

    def landed(data):
        data["launching"] = [r for r in data["launching"] if r.get("id") != launch["id"]]

    try:
        tunnel = start(port=lodge_port, host_header=share_host(name, token, lodge_port))
    except (OSError, RuntimeError) as exc:
        if not domain_url:
            with contextlib.suppress(OSError), _locked(wolts_dir):
                data = _read(wolts_dir)
                landed(data)
                _write(wolts_dir, data)
            raise RuntimeError(f"could not start a quick tunnel: {exc}") from exc
        tunnel = {"url": None, "pid": None}
    record = {
        "token": token,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "by": by,
        "said_by": said_by,
        "tunnel_url": (tunnel.get("url") or "").rstrip("/") or None,
        "tunnel_pid": tunnel.get("pid") or None,
        "domain_url": domain_url,
    }

    def undo():
        # Never leave a tunnel running for a share that didn't go live. One
        # that won't stop is tracked as retired; its token was never current.
        gone = not record["tunnel_pid"] or _stop_and_confirm(
            record["tunnel_pid"], name, lodge_port, token, stop=stop, is_ours=is_ours)
        # If this write fails too, the next sweep still finds the tunnel: its
        # token is no current share's.
        with contextlib.suppress(OSError), _locked(wolts_dir):
            data = _read(wolts_dir)
            landed(data)
            if not gone:
                _retire(data, name, record)
            _write(wolts_dir, data)

    try:
        with _locked(wolts_dir):
            data = _read(wolts_dir)
            if name in data["shares"]:   # shared meanwhile
                raise _AlreadyShared(data["shares"][name])
            if not any(r.get("id") == launch["id"] for r in data["launching"]):
                # An unshare cancelled this launch, or the sweep expired it:
                # it must never be published.
                raise _Expired()
            # The request is logged before the share goes live, and its going
            # live after: a crash between the two reads as attempted, not live.
            _log(wolts_dir, "share-requested", name, by=by, said_by=said_by,
                 tunnel_url=record["tunnel_url"], domain_url=domain_url)
            data["shares"][name] = record
            landed(data)
            _write(wolts_dir, data)
    except _AlreadyShared as already:
        undo()
        return already.record
    except _Expired:
        undo()
        raise RuntimeError("nothing shared: the share was cancelled, or its tunnel took too long to start")
    except OSError as exc:
        # The publishing write may have landed (a failure after the replace):
        # take back exactly this token's share before calling it unshared.
        try:
            with _locked(wolts_dir):
                data = _read(wolts_dir, strict=True)
                if (data["shares"].get(name) or {}).get("token") == token:
                    data["shares"].pop(name)
                    landed(data)
                    _write(wolts_dir, data)
        except (OSError, Corrupt) as again:
            # Say what's on disk now, as far as it can be read. Live: the
            # record tracks its tunnel. Gone: the tunnel is debt. Unreadable:
            # the next sweep reconciles the tunnel against whatever is there.
            live = (shared(wolts_dir, name) or {}).get("token") == token
            if not live:
                undo()
            raise RuntimeError(
                f"the share of {name} {'is live' if live else 'may be live'}: saving failed, then undoing "
                f"failed ({again}); run `woltspace app unshare {name}`") from exc
        undo()
        raise RuntimeError(f"nothing shared: could not record or log the share: {exc}") from exc
    # shared.json says whether a share is live; this line confirms it. If it
    # can't be written, the share stays (the request is logged) and boot
    # writes "live-at-boot" for it.
    logged = _log_quietly(wolts_dir, "shared", name, tunnel_url=record["tunnel_url"], domain_url=domain_url)
    return {**record, "logged_live": logged}


class _Expired(Exception):
    pass


class _AlreadyShared(Exception):
    def __init__(self, record):
        self.record = record


def unshare(wolts_dir, name: str, lodge_port: int, *, by: str, said_by: str = "",
            stop=None, is_ours=None) -> bool:
    """Make an app private again; True if it was shared or being shared. It
    takes effect before anything else: the record and any launch in flight go
    first (a cancelled launch is never published), the log and the tunnel follow."""
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        record = data["shares"].pop(name, None)
        cancelled = [r for r in data["launching"] if r.get("name") == name]
        if record is None and not cancelled:
            return False
        data["launching"] = [r for r in data["launching"] if r.get("name") != name]
        if record is not None:
            _retire(data, name, record)
        _write(wolts_dir, data)
    _log_quietly(wolts_dir, "unshare", name, by=by, said_by=said_by, cancelled_launch=bool(cancelled))
    settle_retired(wolts_dir, lodge_port, stop=stop, is_ours=is_ours)
    return True


def unshare_all(wolts_dir, lodge_port: int, *, by: str, said_by: str = "",
                stop=None, is_ours=None) -> list[str]:
    """Make every app private again, launches in flight included; return the
    names that were shared or being shared."""
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        names = sorted(set(data["shares"]) | {r.get("name", "") for r in data["launching"]})
        for name, record in data["shares"].items():
            _retire(data, name, record)
        data["shares"], data["launching"] = {}, []
        _write(wolts_dir, data)
    for name in names:
        _log_quietly(wolts_dir, "unshare", name, by=by, said_by=said_by, all=True)
    settle_retired(wolts_dir, lodge_port, stop=stop, is_ours=is_ours)
    return names


def _relaunch(wolts_dir, name, token, lodge_port, *, start, stop, is_ours) -> dict:
    """Start a new tunnel for a share that lost its own, the same way `share`
    does: registered before the spawn, published only if this launch and the
    share are both still current, and never left running untracked."""
    launch = _new_launch(name, token)
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        if (data["shares"].get(name) or {}).get("token") != token:
            return {"name": name, "action": "superseded"}
        data["launching"].append(launch)
        _write(wolts_dir, data)
    try:
        tunnel = start(port=lodge_port, host_header=share_host(name, token, lodge_port))
    except (OSError, RuntimeError) as exc:
        tunnel = {"url": None, "pid": None}
        print(f"[sharing] {name}: quick tunnel did not start: {exc}")
    pid = tunnel.get("pid") or None
    try:
        with _locked(wolts_dir):
            data = _read(wolts_dir)
            current = data["shares"].get(name)
            ours = any(r.get("id") == launch["id"] for r in data["launching"])
            data["launching"] = [r for r in data["launching"] if r.get("id") != launch["id"]]
            if current is None or current.get("token") != token or not ours:
                _write(wolts_dir, data)
                raise _Expired()
            current["tunnel_url"] = (tunnel.get("url") or "").rstrip("/") or None
            current["tunnel_pid"] = pid
            _write(wolts_dir, data)
    except Exception as exc:
        # Superseded, cancelled, or the state couldn't be saved: the new
        # tunnel must not run untracked. One that won't stop is debt; if even
        # that can't be saved, the sweep finds it (its pid isn't recorded).
        gone = not pid or _stop_and_confirm(pid, name, lodge_port, token, stop=stop, is_ours=is_ours)
        with contextlib.suppress(OSError, Corrupt), _locked(wolts_dir):
            data = _read(wolts_dir)
            data["launching"] = [r for r in data["launching"] if r.get("id") != launch["id"]]
            current = data["shares"].get(name)
            if current is not None and current.get("token") == token and current.get("tunnel_pid") == pid and pid:
                # The publishing write landed after all: keep it, it's tracked.
                gone = True
            elif not gone:
                _retire(data, name, {"tunnel_pid": pid, "token": token})
            _write(wolts_dir, data)
        if isinstance(exc, _Expired):
            return {"name": name, "action": "superseded"}
        print(f"[sharing] {name}: could not record its new tunnel: {exc}")
        return {"name": name, "action": "failed"}
    _log_quietly(wolts_dir, "tunnel-restarted", name, tunnel_url=tunnel.get("url"))
    return {"name": name, "action": "restarted", "tunnel_url": tunnel.get("url")}


def restore(wolts_dir, lodge_port: int, *, start=None, stop=None, is_ours=None,
            processes=None) -> list[dict]:
    """At boot: settle retired tunnels and sweep orphans; keep each share's
    tunnel if it still runs, else start a new one for the same share (its
    address changes)."""
    start, stop = start or start_cloudflared, stop or stop_cloudflared
    is_ours = is_ours or is_sharing_tunnel
    settle_retired(wolts_dir, lodge_port, stop=stop, is_ours=is_ours)
    sweep_orphans(wolts_dir, lodge_port, processes=processes, stop=stop, is_ours=is_ours)
    actions = []
    for name, record in all_shared(wolts_dir).items():
        if not _log_quietly(wolts_dir, "live-at-boot", name, tunnel_url=record.get("tunnel_url"),
                            domain_url=record.get("domain_url")):
            actions.append({"name": name, "action": "unlogged-at-boot"})
    for name, record in all_shared(wolts_dir).items():
        token = record.get("token", "")
        if record.get("tunnel_pid") and is_ours(record["tunnel_pid"], name, lodge_port, token):
            actions.append({"name": name, "action": "kept"})
            continue
        if not record.get("tunnel_url") and record.get("domain_url"):
            actions.append({"name": name, "action": "domain-only"})
            continue
        actions.append(_relaunch(wolts_dir, name, token, lodge_port, start=start, stop=stop, is_ours=is_ours))
    return actions


def stuck_tunnels(wolts_dir, lodge_port: int, *, is_ours=None) -> list[dict]:
    """Retired tunnels still running (for `woltspace doctor`)."""
    is_ours = is_ours or is_sharing_tunnel
    return [r for r in _read(wolts_dir)["retired"]
            if is_ours(r.get("pid"), r.get("name", ""), lodge_port, r.get("token", ""))]
