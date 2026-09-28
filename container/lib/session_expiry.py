"""Lodge-owned idle-session policy.

The owner-facing setting lives in ``woltspace.json`` and is only written by
the FastAPI control plane.  Consumers (the vulture and session views) share
this reader so "never" has one fail-safe meaning everywhere.
"""

import json
from pathlib import Path

from env_compat import get_env
from paths import space_vulture_dir


ALLOWED_IDLE_TIMEOUTS = {None, 3600, 14400, 86400}
# A lodge nobody has configured closes sessions idle for a day; "Never" is an explicit null.
DEFAULT_IDLE_TIMEOUT = 86400


def _config_path() -> Path:
    return Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts")) / "woltspace.json"


def get_idle_timeout() -> int | None:
    path = _config_path()
    if not path.exists():
        return DEFAULT_IDLE_TIMEOUT
    try:
        sessions = json.loads(path.read_text()).get("sessions", {})
        if not isinstance(sessions, dict):
            return None
        if "idle_timeout_seconds" not in sessions:
            return DEFAULT_IDLE_TIMEOUT
        value = sessions["idle_timeout_seconds"]
    except (json.JSONDecodeError, OSError, AttributeError, TypeError):
        # an unreadable config never closes anything
        return None
    return value if value in ALLOWED_IDLE_TIMEOUTS else None


def set_idle_timeout(value: int | None) -> int | None:
    """Persist a validated value. Callers must be control-plane routes."""
    if value not in ALLOWED_IDLE_TIMEOUTS:
        raise ValueError("idle timeout must be 3600, 14400, 86400, or null")
    path = _config_path()
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(config, dict):
            config = {}
    except (json.JSONDecodeError, OSError):
        config = {}
    config.setdefault("sessions", {})["idle_timeout_seconds"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(config, indent=2) + "\n")
    tmp.replace(path)
    return value


def get_pane_activity() -> dict:
    path = space_vulture_dir(_config_path().parent) / "pane-activity.json"
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}
