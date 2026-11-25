"""Launch-scoped Claude limits status line and its silent snapshot writer."""

from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import sys
import time

from usage_collector import claude_limits_path, limit_window, timestamp, write_json

WINDOWS = {"five_hour": 300, "seven_day": 10080}
INPUT_LIMIT = 128 * 1024
LOCK_SECONDS = 0.2
MAX_CLOCK_SKEW = timedelta(minutes=5)
MANAGED = {
    "linux": Path("/etc/claude-code"),
    "darwin": Path("/Library/Application Support/ClaudeCode"),
}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate settings key")
        result[key] = value
    return result


def _settings(path, *, missing=True):
    try:
        data = json.loads(path.read_bytes(), object_pairs_hook=_object)
    except FileNotFoundError:
        if missing and not path.is_symlink():
            return {}
        raise
    if not isinstance(data, dict):
        raise ValueError("settings must be an object")
    return data


def enabled(root):
    try:
        options = _settings(Path(root) / "woltspace.json").get("usage", {})
        value = options.get("claude_limits", {}).get("enabled", True)
        return value is True
    except (OSError, ValueError, AttributeError, RecursionError):
        return False


def overlay(root, workdir, native_settings=None, *, environ=None, managed=None):
    """Return inline launch settings when local precedence can be determined."""
    env = os.environ if environ is None else environ
    managed = MANAGED.get(sys.platform) if managed is None else managed
    if not enabled(root) or managed is None:
        return None
    try:
        home = Path(env.get("HOME") or Path.home())
        wolt_home = Path(env["WOLTSPACE_WOLT_HOME"]) if env.get("WOLTSPACE_WOLT_HOME") else None
        if env.get("WOLTSPACE_ISOLATION") != "host":
            if wolt_home is None or not wolt_home.is_absolute():
                return None
            if (wolt_home / ".claude").is_dir():
                home = wolt_home
        engine = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        if not engine.is_absolute() or not Path(workdir).is_absolute():
            return None
        project = Path(workdir) / ".claude"
        files = [engine / "settings.json", project / "settings.json",
                 project / "settings.local.json", managed / "managed-settings.json"]
        try:
            files.extend(sorted(p for p in (managed / "managed-settings.d").iterdir()
                                if p.suffix == ".json"))
        except FileNotFoundError:
            pass
        explicit = _settings(native_settings, missing=False) if native_settings else {}
        if "statusLine" in explicit or any("statusLine" in _settings(path) for path in files):
            return None
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), str(Path(root).resolve())])
        return json.dumps({**explicit, "statusLine": {"type": "command", "command": command}})
    except (OSError, ValueError, RecursionError):
        return None


def publish(root, read_at, data):
    """Publish strictly newer readings while holding a stable, bounded lock."""
    if not isinstance(data, dict) or not isinstance(data.get("rate_limits"), dict):
        return
    windows = {}
    for key, minutes in WINDOWS.items():
        window = limit_window(data["rate_limits"].get(key), "used_percentage", minutes)
        if window and window["used_percent"] <= 100 and type(window["resets_at"]) is int:
            windows[key] = {"used_percentage": window["used_percent"], "resets_at": window["resets_at"]}
    if not windows or not enabled(root):
        return
    path = claude_limits_path(root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        return
    deadline = time.monotonic() + LOCK_SECONDS
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.01)
        if not enabled(root):
            return
        try:
            current = _settings(path)
        except (ValueError, RecursionError):
            current = {}
        observed = timestamp(current.get("read_at"))
        # A delayed writer's own timestamp cannot establish clock skew.
        too_far_ahead = datetime.now(timezone.utc) + MAX_CLOCK_SKEW
        if observed is None or read_at > observed or observed > too_far_ahead:
            write_json(path, {"read_at": read_at.isoformat(), "rate_limits": windows}, 0o600, sync=True)


def main():
    read_at = datetime.now(timezone.utc)
    try:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(raw) <= INPUT_LIMIT:
            publish(sys.argv[1], read_at, json.loads(raw))
    except (OSError, ValueError, RecursionError, IndexError, OverflowError):
        pass


if __name__ == "__main__":
    main()
