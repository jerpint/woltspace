"""Lodge-owned idle-session policy.

The owner-facing setting lives in ``woltspace.json`` and is only written by
the FastAPI control plane.  Consumers (the vulture and session views) share
this reader so "never" has one fail-safe meaning everywhere.

The rule: a running session rests once nobody has used it for the window,
unless it holds. Use is the later of the record's ``last_activity`` (every
lodge write: spawn, wake, a message in or out, push-view, describe) and
tmux's ``session_activity`` (an attach or a keystroke). Nothing else is
read: no screen, no output, no CPU, no hooks, so every engine behaves alike.
"""

import json
from pathlib import Path

from env_compat import get_env
from session_runtime import RuntimeHandle


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


def last_use(record: dict, keys: dict[str, int]) -> int | None:
    """When the session was last used; None when tmux does not list it.

    ``keys`` is ``SessionRuntime.last_keys()``. Unknown is in use: a session
    tmux did not report is never judged idle.
    """
    tmux_used = keys.get(RuntimeHandle.from_record(record).tmux_session_name)
    if not isinstance(tmux_used, int):
        return None
    recorded = record.get("last_activity")
    if not isinstance(recorded, int) or isinstance(recorded, bool):
        recorded = 0
    return max(recorded, tmux_used)


# The longest hold the route takes; null (forever) covers anything longer. An
# until past now + this was never written by the route, and no clock prints it.
HOLD_MAX_SECONDS = 100 * 365 * 86400


def _well_formed(hold, now: int) -> bool:
    """A hold the route could have written: until null, or a whole epoch no
    further than the route's cap. Anything else is malformed: no hold."""
    if not isinstance(hold, dict) or "until" not in hold:
        return False
    until = hold["until"]
    if until is None:
        return True
    return isinstance(until, int) and not isinstance(until, bool) and until <= now + HOLD_MAX_SECONDS


def hold_active(record: dict, now: int) -> bool:
    """A running session holds while its hold has no end or ends after now.

    ``hold = {"until": <epoch> | None, "reason": str, "since": <epoch>}``;
    ``until: None`` is forever. Reads the record only. A malformed hold is
    no hold, never a forever one.
    """
    if record.get("status") != "running":
        return False
    hold = record.get("hold")
    if not _well_formed(hold, now):
        return False
    return hold["until"] is None or hold["until"] > now
