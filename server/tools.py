// should be in its own route maybe?
"""Tool proxy registry — spawn, track, and proxy to tool processes."""

import json
import os
import signal
import subprocess
import time

from .config import STATE_DIR, TOOL_REGISTRY_FILE, WOLT_DIR

_registry: dict[str, dict] = {}


def _save():
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        TOOL_REGISTRY_FILE.write_text(json.dumps(_registry))
    except Exception as e:
        print(f"[tools] save failed: {e}")


def register(name: str, port: int, pid: int, command: str):
    _registry[name] = {"port": port, "pid": pid, "command": command, "startedAt": int(time.time() * 1000)}
    _save()
    print(f"[tools] registered {name} on port {port} (pid {pid})")


def unregister(name: str):
    info = _registry.pop(name, None)
    if info:
        try:
            os.kill(info["pid"], signal.SIGTERM)
        except Exception:
            pass
        _save()
        print(f"[tools] unregistered {name}")


def get(name: str) -> dict | None:
    return _registry.get(name)


def list_all() -> list[dict]:
    return [
        {"name": name, "port": info["port"], "pid": info["pid"], "uptime": int(time.time() * 1000) - info["startedAt"]}
        for name, info in _registry.items()
    ]


def spawn(name: str, command: str, port: int) -> dict:
    if name in _registry:
        raise ValueError(f"{name} already running")
    child = subprocess.Popen(
        ["sh", "-c", command],
        cwd=str(WOLT_DIR),
        env={**os.environ, "PORT": str(port)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    register(name, port, child.pid, command)
    return {"name": name, "port": port, "pid": child.pid}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def restore():
    """Restore tool registry from disk, respawn dead tools."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if not TOOL_REGISTRY_FILE.exists():
        return
    try:
        data = json.loads(TOOL_REGISTRY_FILE.read_text())
        for name, info in data.items():
            if _pid_alive(info["pid"]):
                _registry[name] = info
                print(f"[tools] restored {name} (pid {info['pid']} still alive)")
            elif info.get("command"):
                child = subprocess.Popen(
                    ["sh", "-c", info["command"]],
                    cwd=str(WOLT_DIR),
                    env={**os.environ, "PORT": str(info["port"])},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                _registry[name] = {**info, "pid": child.pid, "startedAt": int(time.time() * 1000)}
                print(f"[tools] respawned {name} on port {info['port']} (new pid {child.pid})")
        _save()
    except Exception as e:
        print(f"[tools] restore failed: {e}")


def gc():
    """Garbage-collect dead tools."""
    dead = [name for name, info in _registry.items() if not _pid_alive(info["pid"])]
    for name in dead:
        del _registry[name]
    if dead:
        _save()
