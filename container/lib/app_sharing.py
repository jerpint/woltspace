"""Sharing an app with anyone who has its link: an explicit opt-in, per app.

Apps are the owner's. Sharing one lets anyone with the link use it, and do
whatever the app can do; nothing else. The lodge, wolt sites and other apps
are never shared, and a visitor never carries the owner's identity:

- the visitor reaches the app through the lodge and the Mill, like the owner,
  so the lodge's and Access's credentials are stripped as for the owner;
- the Mill marks the visitor `public`, never `owner`.

Two ways in, both through the lodge's port:

- a quick tunnel for this one app: `cloudflared tunnel --url
  http://localhost:<lodge port> --http-host-header <app>.localhost:<lodge port>`,
  so every request it carries names this app's host and nothing else. It needs
  no domain; its trycloudflare address changes when the tunnel restarts.
- with a named tunnel, `https://<app>.<domain>/`, once the owner's Access
  policy lets visitors through for that host. The lodge can't change Access.

State is machine-local: `<data root>/.space/run/sharing/shared.json`, folder
0700, file 0600. Every share and unshare is appended to
`<data root>/.space/logs/sharing.log`.

A visitor is served only while the app's record exists: unsharing removes the
record before stopping the tunnel, so a failure leaves the app private, never
shared and unlisted.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from tunnel import is_cloudflared, process_command, start_cloudflared, stop_cloudflared

PUBLIC = "public"
WARNING = ("Anyone with this link can use this app, and do whatever the app can do. "
           "The lodge, wolt sites and other apps stay private.")


def sharing_dir(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "run" / "sharing"


def _store(wolts_dir) -> Path:
    return sharing_dir(wolts_dir) / "shared.json"


def log_path(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "logs" / "sharing.log"


def _read(wolts_dir) -> dict:
    try:
        data = json.loads(_store(wolts_dir).read_text())
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _write(wolts_dir, data: dict) -> None:
    path = _store(wolts_dir)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
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
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _log(wolts_dir, event: str, name: str, **fields) -> None:
    path = log_path(wolts_dir)
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        line = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event, "app": name, **fields}
        with open(path, "a") as handle:
            handle.write(json.dumps(line) + "\n")


def shared(wolts_dir, name: str) -> dict | None:
    """The app's share record, or None when it's private."""
    return _read(wolts_dir).get(name)


def all_shared(wolts_dir) -> dict[str, dict]:
    return _read(wolts_dir)


def tunnel_host_header(name: str, lodge_port: int) -> str:
    return f"{name}.localhost:{lodge_port}"


def is_sharing_tunnel(pid, name: str, lodge_port: int, *, command=process_command,
                      cloudflared=is_cloudflared) -> bool:
    """Whether a pid is this app's sharing tunnel: a cloudflared whose command
    targets the lodge's port with this app's host."""
    if not pid or not cloudflared(pid):
        return False
    try:
        parts = command(pid).split()
    except Exception:
        return False

    def arg(flag):
        return parts[parts.index(flag) + 1] if flag in parts[:-1] else None
    return (arg("--url") == f"http://localhost:{lodge_port}"
            and arg("--http-host-header") == tunnel_host_header(name, lodge_port))


def share(wolts_dir, name: str, lodge_port: int, *, by: str, said_by: str = "",
          domain_url: str | None = None, start=None) -> dict:
    """Share an app; return its record. Sharing a shared app returns it as is.

    Starts a quick tunnel for the app. Without cloudflared, the share holds
    only when there's a domain address; otherwise nothing is shared and
    RuntimeError says why.
    """
    start = start or start_cloudflared
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        if name in data:
            return data[name]
        try:
            tunnel = start(port=lodge_port, host_header=tunnel_host_header(name, lodge_port))
        except (OSError, RuntimeError) as exc:
            if not domain_url:
                raise RuntimeError(f"could not start a quick tunnel: {exc}") from exc
            tunnel = {"url": None, "pid": None}
        record = {
            "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": by,
            "said_by": said_by,
            "tunnel_url": tunnel.get("url") or None,
            "tunnel_pid": tunnel.get("pid") or None,
            "domain_url": domain_url,
        }
        data[name] = record
        _write(wolts_dir, data)
    _log(wolts_dir, "share", name, by=by, said_by=said_by,
         tunnel_url=record["tunnel_url"], domain_url=domain_url)
    return record


def _stop_tunnel(pid, name: str, lodge_port: int, stop=None) -> None:
    if is_sharing_tunnel(pid, name, lodge_port):
        (stop or stop_cloudflared)(pid)


def unshare(wolts_dir, name: str, lodge_port: int, *, by: str, said_by: str = "",
            stop=None) -> bool:
    """Make an app private again; True if it was shared."""
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        record = data.pop(name, None)
        if record is None:
            return False
        _write(wolts_dir, data)
    _stop_tunnel(record.get("tunnel_pid"), name, lodge_port, stop)
    _log(wolts_dir, "unshare", name, by=by, said_by=said_by)
    return True


def unshare_all(wolts_dir, lodge_port: int, *, by: str, said_by: str = "",
                stop=None) -> list[str]:
    """Make every app private again; return the names that were shared."""
    with _locked(wolts_dir):
        data = _read(wolts_dir)
        _write(wolts_dir, {})
    for name, record in data.items():
        _stop_tunnel(record.get("tunnel_pid"), name, lodge_port, stop)
        _log(wolts_dir, "unshare", name, by=by, said_by=said_by, all=True)
    return sorted(data)


def restore(wolts_dir, lodge_port: int, *, start=None, is_ours=None) -> list[dict]:
    """At boot: keep each shared app's tunnel if it still runs, else start a
    new one (its address changes). A share whose tunnel won't start stays
    listed, with no address, so the owner sees it."""
    start = start or start_cloudflared
    is_ours = is_ours or is_sharing_tunnel
    actions = []
    for name, record in all_shared(wolts_dir).items():
        if record.get("tunnel_pid") and is_ours(record["tunnel_pid"], name, lodge_port):
            actions.append({"name": name, "action": "kept"})
            continue
        if not record.get("tunnel_url") and record.get("domain_url"):
            actions.append({"name": name, "action": "domain-only"})
            continue
        try:
            tunnel = start(port=lodge_port, host_header=tunnel_host_header(name, lodge_port))
        except (OSError, RuntimeError) as exc:
            tunnel = {"url": None, "pid": None}
            print(f"[sharing] {name}: quick tunnel did not start: {exc}")
        with _locked(wolts_dir):
            data = _read(wolts_dir)
            if name not in data:
                # Unshared meanwhile: never leave its new tunnel running.
                _stop_tunnel(tunnel.get("pid"), name, lodge_port)
                continue
            data[name]["tunnel_url"] = tunnel.get("url") or None
            data[name]["tunnel_pid"] = tunnel.get("pid") or None
            _write(wolts_dir, data)
        _log(wolts_dir, "tunnel-restarted", name, tunnel_url=tunnel.get("url"))
        actions.append({"name": name, "action": "restarted", "tunnel_url": tunnel.get("url")})
    return actions


def public_origins(record: dict) -> set[str]:
    """The public pages a shared app's browser writes may come from."""
    origins = set()
    for key in ("tunnel_url", "domain_url"):
        url = (record or {}).get(key)
        if url:
            origins.add(url.rstrip("/"))
    return origins


__all__ = [
    "PUBLIC", "WARNING", "all_shared", "is_sharing_tunnel", "log_path", "public_origins",
    "restore", "share", "shared", "tunnel_host_header", "unshare", "unshare_all",
]
