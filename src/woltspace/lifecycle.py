"""Safe background lifecycle built around the foreground supervisor."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid

from .doctor import doctor_ok, run_doctor
from .instance import (
    clear_owner_if_unlocked,
    inspect_instance,
    pid_alive,
    read_health,
    read_owner,
)
from .layout import RuntimeLayout


def start(layout: RuntimeLayout, *, timeout: float = 15.0) -> tuple[int, dict]:
    current = inspect_instance(layout)
    if current["state"] == "healthy":
        return 0, {**current, "detail": "already running"}
    if current["state"] == "starting":
        return 0, {**current, "detail": "already starting; no second instance launched"}
    if current["state"] == "conflict":
        return 1, {**current, "error": "endpoint belongs to another control plane"}

    checks = run_doctor(layout, check_port=True)
    if not doctor_ok(checks):
        return 1, {
            "state": "doctor-failed",
            "checks": [check.to_record() for check in checks],
        }

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
    env.update({
        "WOLTS_DIR": str(layout.wolts_dir),
        "WOLTSPACE_DIR": str(layout.install_root),
        "WOLTSPACE_ISOLATION": layout.isolation,
        "WOLTSPACE_HOST": layout.host,
        "WOLTSPACE_PORT": str(layout.port),
    })
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
            return 0, {
                "state": "healthy",
                "detail": "started",
                "pid": process.pid,
                "instance_id": instance_id,
                "endpoint": layout.endpoint,
                "wolts_dir": str(layout.wolts_dir),
                "log": str(log_path),
                "health": health,
            }
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
