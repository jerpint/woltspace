"""Safe background lifecycle built around the foreground supervisor."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid

from .doctor import doctor_ok, run_doctor
from .envvars import export_both
from .hooks import normalize_platform_hooks
from .instance import (
    clear_owner_if_unlocked,
    inspect_instance,
    pid_alive,
    read_health,
    read_owner,
)
from .layout import RuntimeLayout
from .skills import sync_platform_skills


#: How long `tunnel_report` will wait for a quick tunnel's random URL to land
#: in the state file. A named tunnel needs none of this — its URL is config.
QUICK_TUNNEL_ATTEMPTS = 16
QUICK_TUNNEL_INTERVAL = 0.5


def tunnel_settings(layout: RuntimeLayout) -> dict:
    """Resolve publishing the way the control plane will resolve it.

    Same precedence the supervisor applies when it boots: an exported shell
    variable wins, then the data root's `.env`, and a native run publishes
    nothing unless somebody said otherwise. Read here so `woltspace start` can
    name the public URL from configuration alone — a named tunnel's address is
    known before cloudflared has drawn breath, and nothing has to be scraped
    out of a log to say it.
    """
    values = dict(os.environ)
    env_file = layout.wolts_dir / ".env"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values

            for key, value in dotenv_values(env_file).items():
                if value is not None and key not in values:
                    values[key] = value
        except (OSError, ImportError):  # a broken .env must not break start
            pass
    default = "false" if layout.isolation == "host" else "true"
    enabled = (values.get("WOLTSPACE_PUBLIC_TUNNEL") or default).lower() == "true"
    named = (values.get("CLOUDFLARE_TUNNEL_URL") or "").strip()
    token = (values.get("CLOUDFLARE_TUNNEL_TOKEN") or "").strip()
    return {
        "enabled": enabled,
        # A named tunnel is a token *and* a URL: with only one of the pair the
        # server falls back to a quick tunnel, whose URL is random.
        "kind": "named" if (named and token) else "quick",
        "url": named if (named and token) else "",
    }


def read_tunnel_url(layout: RuntimeLayout) -> str:
    """The URL the running control plane published, if it has published one."""
    state_file = layout.platform_state / "tunnel.json"
    try:
        data = json.loads(state_file.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    url = data.get("url") if isinstance(data, dict) else None
    return url if isinstance(url, str) else ""


def tunnel_report(layout: RuntimeLayout, *, wait: bool = True) -> dict:
    """What to tell the human about public access.

    Three fields, and the difference between two of them matters:

    * ``url`` — the address the lodge publishes at, from config or from the
      running tunnel. For a named tunnel this is known before cloudflared has
      drawn breath.
    * ``live`` — the address a tunnel is *actually* serving right now, as
      written to ``tunnel.json`` by the control plane. Empty when nothing is
      published, which is why `status` can tell "configured" from "up".

    A quick tunnel's URL is assigned by Cloudflare at run time, so with
    ``wait=True`` (a fresh `start`) the state file is briefly watched. With
    ``wait=False`` (a `status`, which is a question about right now) nothing
    is ever waited on.
    """
    settings = tunnel_settings(layout)
    if not settings["enabled"]:
        return {**settings, "live": ""}
    attempts = QUICK_TUNNEL_ATTEMPTS if (wait and not settings["url"]) else 1
    for attempt in range(attempts):
        live = read_tunnel_url(layout)
        if live:
            return {**settings, "live": live, "url": settings["url"] or live}
        if attempt + 1 < attempts:
            time.sleep(QUICK_TUNNEL_INTERVAL)
    return {**settings, "live": ""}


def start(layout: RuntimeLayout, *, timeout: float = 15.0) -> tuple[int, dict]:
    current = inspect_instance(layout)
    if current["state"] == "healthy":
        return 0, {**current, "detail": "already running"}
    if current["state"] == "starting":
        return 0, {**current, "detail": "already starting; no second instance launched"}
    if current["state"] == "conflict":
        return 1, {**current, "error": "endpoint belongs to another control plane"}

    # Typing `woltspace start` against a named data root IS the deliberate act
    # that makes this its owner, so doctor judges it as the entrypoint rather
    # than as a guest that found live sessions lying around.
    checks = run_doctor(layout, check_port=True, as_entrypoint=True)
    if not doctor_ok(checks):
        return 1, {
            "state": "doctor-failed",
            "checks": [check.to_record() for check in checks],
        }

    # The container refreshes every wolt's woltspace-* skills on boot, and a
    # native start is that boot. Skills going stale is worth saying out loud;
    # it is never worth refusing to start over.
    skills_error = None
    try:
        sync_platform_skills(layout)
    except Exception as error:  # noqa: BLE001 — a broken sync must not block start
        skills_error = f"{type(error).__name__}: {error}"

    # The claude hooks the platform used to write are retired; sweep their baked
    # paths out of wolts made before the change so they stop spamming errors.
    # Like the skill sync, a failure here is worth naming but never fatal.
    hooks_error = None
    try:
        normalize_platform_hooks(layout)
    except Exception as error:  # noqa: BLE001 — a broken sweep must not block start
        hooks_error = f"{type(error).__name__}: {error}"

    layout.logs_dir.mkdir(parents=True, exist_ok=True)
    instance_id = uuid.uuid4().hex
    log_path = layout.logs_dir / "control-plane.log"
    command = [
        sys.executable,
        "-m",
        "woltspace",
        "serve",
        "--host",
        layout.host,
        "--port",
        str(layout.port),
        "--isolation",
        layout.isolation,
        "--instance-id",
        instance_id,
        "--no-doctor",
    ]
    env = dict(os.environ)
    env.update(export_both({
        # `woltspace start` is a deliberate act on a named data root, so the
        # control plane it launches is the owner.
        "WOLTSPACE_ENTRYPOINT": "1",
        "WOLTSPACE_WOLTS_DIR": str(layout.wolts_dir),
        "WOLTSPACE_DIR": str(layout.install_root),
        "WOLTSPACE_ISOLATION": layout.isolation,
        "WOLTSPACE_HOST": layout.host,
        "WOLTSPACE_PORT": str(layout.port),
    }))
    with log_path.open("a") as log:
        process = subprocess.Popen(
            command,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        health = read_health(layout.endpoint)
        if health and health.get("instance_id") == instance_id:
            started = {
                "state": "healthy",
                "detail": "started",
                "pid": process.pid,
                "instance_id": instance_id,
                "endpoint": layout.endpoint,
                "wolts_dir": str(layout.wolts_dir),
                "log": str(log_path),
                "health": health,
            }
            if skills_error:
                started["skills_sync_error"] = skills_error
            if hooks_error:
                started["hooks_normalize_error"] = hooks_error
            return 0, started
        if process.poll() is not None:
            return 1, {
                "state": "failed",
                "error": f"control plane exited with {process.returncode}",
                "log": str(log_path),
            }
        time.sleep(0.1)
    return 1, {
        "state": "starting",
        "error": f"health did not become ready within {timeout:g}s",
        "pid": process.pid,
        "instance_id": instance_id,
        "log": str(log_path),
    }


def stop(layout: RuntimeLayout, *, timeout: float = 10.0) -> tuple[int, dict]:
    current = inspect_instance(layout)
    if current["state"] == "stopped":
        return 0, {**current, "detail": "already stopped; tmux sessions untouched"}
    if current["state"] == "stale":
        owner_record = current.get("owner") or {}
        instance_id = owner_record.get("instance_id", "")
        cleared = bool(instance_id and clear_owner_if_unlocked(layout, instance_id))
        detail = "stale metadata cleared" if cleared else "stale metadata left unchanged"
        return 0, {**current, "detail": f"{detail}; no process signalled; tmux sessions untouched"}
    if current["state"] != "healthy":
        return 1, {
            **current,
            "error": "refusing to signal a control plane without matching health identity",
        }

    owner = read_owner(layout)
    if owner is None:
        return 1, {**current, "error": "owner metadata disappeared before stop"}
    endpoint = owner.endpoint or layout.endpoint
    verified = read_health(endpoint)
    if not verified or verified.get("instance_id") != owner.instance_id:
        return 1, {**current, "error": "instance identity changed before stop; nothing signalled"}

    os.kill(owner.pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_alive(owner.pid) and not read_health(endpoint):
            clear_owner_if_unlocked(layout, owner.instance_id)
            return 0, {
                "state": "stopped",
                "detail": "control plane stopped; tmux sessions untouched",
                "instance_id": owner.instance_id,
                "pid": owner.pid,
            }
        time.sleep(0.1)
    return 1, {
        "state": "stopping",
        "error": "control plane did not exit; no force signal was sent",
        "instance_id": owner.instance_id,
        "pid": owner.pid,
    }
