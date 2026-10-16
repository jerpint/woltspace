"""
🦅 Vulture — Session Reaper

The vulture circles the lodge, cleaning up dead sessions before they
pile up and choke the system. Runs as a platform-level background
process — not managed by wolves.

It does three things on each pass:
  0. Rests sessions whose pane has printed nothing for the owner-selected window
     (tmux's own window_activity stamp, so it works for every harness).
  1. Reconciles the registry — marks "running" sessions as "reaped"
     if their tmux session is gone.
  2. Kills zombie tmux sessions — lodge sessions where the agent has
     exited but the tmux session lingers. A tmux session the registry
     does not know is never touched: natively it is the owner's own.

Config: sessions.idle_timeout_seconds in woltspace.json (default: 1 day; null = never).
Logs:   .space/vulture/vulture.log
State:  .space/vulture/last-run

Usage:
  python -m creatures.vulture              # Run as background service (default: every 30 min)
  python -m creatures.vulture --once       # Single pass and exit
  python -m creatures.vulture --interval N # Custom interval in seconds
  python -m creatures.vulture --dry-run    # Show what would be reaped, don't touch anything
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from env_compat import get_env
from sessions import SessionRegistry, recover_resume_id, stored_resume_id, taken_resume_ids
from harnesses import session_has_agent_process
from paths import space_vulture_dir
from session_runtime import RuntimeHandle, get_runtime
from session_expiry import get_idle_timeout
from urllib import error as urlerror
from urllib import request as urlrequest

WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))
STATE_DIR = space_vulture_dir(WOLTS_DIR)
LOG_FILE = STATE_DIR / "vulture.log"
LAST_RUN_FILE = STATE_DIR / "last-run"

# Sessions younger than this are never reaped (grace period for startup)
GRACE_PERIOD_SECONDS = 120

# Don't kill the main tmux session
PROTECTED_SESSIONS = {"main"}

DEFAULT_INTERVAL = 1800  # 30 minutes


def _send_notify(message: str):
    """Send a vulture notification via the server — only when there's something to report."""
    full_message = f"🦅 {message}"
    payload = json.dumps({"message": full_message, "session": ""})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             os.environ.get("WOLTSPACE_API", "http://localhost:7777") + "/notify",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        resp = json.loads(result.stdout) if result.stdout else {}
        if resp.get("ok"):
            print(f"[vulture] notified: {message}")
        else:
            print(f"[vulture] notify failed: {resp.get('error', result.stdout)}", file=sys.stderr)
    except Exception as e:
        print(f"[vulture] notify error: {e}", file=sys.stderr)


def log(msg: str):
    """Append to vulture log with timestamp."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
        # Keep log from growing forever — trim to last 500 lines
        _trim_log()
    except OSError:
        pass


def _trim_log(max_lines: int = 500):
    """Keep the log file from growing unbounded."""
    try:
        lines = LOG_FILE.read_text().splitlines()
        if len(lines) > max_lines:
            LOG_FILE.write_text("\n".join(lines[-max_lines:]) + "\n")
    except OSError:
        pass


def _tmux_sessions() -> set[str]:
    """Get all live named sessions through the process-control boundary."""
    return get_runtime().list_session_names(include_main=True)


def _kill_tmux_session(session_name: str) -> bool:
    """Stop one exact named session through the process-control boundary."""
    return get_runtime().stop(RuntimeHandle(session_name, session_name))


def _rest_via_api(session_name: str, idle_since: int) -> bool:
    """Ask the FastAPI authority to perform the lifecycle/state change."""
    base = os.environ.get("WOLTSPACE_API", "http://localhost:7777").rstrip("/")
    req = urlrequest.Request(
        f"{base}/sessions/{session_name}/rest",
        data=json.dumps({"idle_since": idle_since}).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            return 200 <= response.status < 300
    except (OSError, urlerror.URLError):
        return False


def _settle_dead(data: dict, now: int) -> str:
    """Mark a session whose runtime is gone. One that can come back rests.

    "resting" sessions wake when someone messages them; "reaped" ones refuse.
    A dead session that still has its conversation id is no different from
    one the vulture rested for being idle, so it gets the same status.
    """
    status = "resting" if stored_resume_id(data) else "reaped"
    data["status"] = status
    if status == "resting":
        data["rested_at"] = now
    else:
        data["finished_at"] = now
    data["last_activity"] = now
    return status


def _rest_idle_sessions(reg: SessionRegistry, live_tmux: set[str], now: int,
                        timeout: int | None, dry_run: bool) -> list[str]:
    """Rest sessions whose pane has printed nothing for the configured window.

    tmux stamps each window with the time of its last output; a working agent
    keeps it fresh (spinner, tool output), one idle at its prompt leaves it.
    """
    if timeout is None:
        return []
    try:
        last_outputs = get_runtime().last_outputs()
    except Exception:
        # Unknown activity is active: never close on missing evidence.
        return []
    rested = []
    records = reg.list()
    taken = taken_resume_ids(records)
    for data in records:
        name = data.get("name", "")
        if (not name or name in PROTECTED_SESSIONS or name not in live_tmux
                or data.get("status") != "running"):
            continue
        if now - (data.get("created_at") or now) < GRACE_PERIOD_SECONDS:
            continue
        # Resting must be reversible: never stop a session resume_session could not bring back.
        if not recover_resume_id(data, taken):
            continue
        idle_since = last_outputs.get(RuntimeHandle.from_record(data).tmux_session_name)
        if idle_since is None or now - idle_since < timeout:
            continue
        if dry_run:
            log(f"[dry-run] would rest idle session: {name}")
            rested.append(name)
        elif _rest_via_api(name, idle_since):
            log(f"🦅 rested idle session: {name}")
            rested.append(name)
        else:
            log(f"could not rest idle session through API: {name}")
    return rested


def reap(dry_run: bool = False) -> dict:
    """
    Single reaper pass. Returns stats dict:
      { "registry_reaped": [...], "tmux_killed": [...], "errors": [...] }
    """
    reg = SessionRegistry(WOLTS_DIR)
    now = int(time.time())
    live_tmux = _tmux_sessions()
    stats = {"registry_reaped": [], "tmux_killed": [], "sessions_rested": [], "errors": []}

    stats["sessions_rested"] = _rest_idle_sessions(
        reg, live_tmux, now, get_idle_timeout(), dry_run
    )

    # --- Pass 1: Registry reconciliation ---
    # Find "running" registry entries whose tmux session is dead.
    # Iterate all wolts' .state/sessions/ dirs (per-wolt model).
    for wolt_entry in sorted(WOLTS_DIR.iterdir()) if WOLTS_DIR.exists() else []:
        if not wolt_entry.is_dir() or wolt_entry.name.startswith("."):
            continue
        sessions_dir = wolt_entry / ".state" / "sessions"
        if not sessions_dir.exists():
            continue
        for path in sessions_dir.glob("*.json"):
            if path.name.endswith(".tmp"):
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue

            name = data.get("name", path.stem)
            status = data.get("status", "")
            created_at = data.get("created_at", 0)
            wolt_name = data.get("wolt", wolt_entry.name)

            # Only reap sessions marked as running
            if status != "running":
                continue

            # Grace period — don't reap sessions that just started
            if now - created_at < GRACE_PERIOD_SECONDS:
                continue

            # If tmux session is gone, mark it resting (or reaped if it can't resume)
            if name not in live_tmux:
                if dry_run:
                    log(f"[dry-run] would reap registry: {name}")
                else:
                    status = _settle_dead(data, now)
                    reg._write(wolt_name, name, data)
                    log(f"🦅 {status} registry: {name}")
                stats["registry_reaped"].append(name)

    # --- Pass 2: Zombie tmux sessions ---
    # Kill tmux sessions that exist but have no claude process and no registry
    # entry marked "running" (they're leftover shells)
    for session_name in live_tmux:
        if session_name in PROTECTED_SESSIONS:
            continue

        # Look up the exact persisted handle before inspecting its process tree.
        reg_data = reg.get(session_name, check_alive=False)

        # Check if this session has an active agent process (any harness).
        # None here means "still booting or tmux vanished" — treat as alive,
        # never kill on uncertainty.
        if session_has_agent_process(reg_data or session_name) is not False:
            continue

        if reg_data and reg_data.get("status") == "running":
            wolt_name = reg_data.get("wolt", "")
            # The agent exited but tmux lingers — kill tmux, rest or reap the record
            if dry_run:
                log(f"[dry-run] would kill zombie tmux + reap: {session_name}")
            else:
                status = _settle_dead(reg_data, now)
                if wolt_name:
                    reg._write(wolt_name, session_name, reg_data)
                _kill_tmux_session(session_name)
                log(f"🦅 killed zombie tmux, {status}: {session_name}")
            stats["tmux_killed"].append(session_name)
        elif reg_data and reg_data.get("status") in ("reaped", "orphaned", "completed", "failed", "resting"):
            # Already reaped/done but tmux still exists — clean up tmux
            if dry_run:
                log(f"[dry-run] would kill leftover tmux: {session_name}")
            else:
                _kill_tmux_session(session_name)
                log(f"🦅 killed leftover tmux: {session_name}")
            stats["tmux_killed"].append(session_name)
        # A session with no registry entry is not the lodge's. Natively the
        # lodge shares the owner's own tmux server, so "no agent, no record"
        # describes their terminals; the vulture used to kill them.

    # Write last-run timestamp
    if not dry_run:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_RUN_FILE.write_text(str(now))

    return stats


def run_loop(interval: int = DEFAULT_INTERVAL, dry_run: bool = False):
    """Run the vulture on a loop."""
    log(f"🦅 vulture started — interval {interval}s, dry_run={dry_run}")
    while True:
        try:
            stats = reap(dry_run=dry_run)
            total = sum(len(stats[key]) for key in ("registry_reaped", "tmux_killed", "sessions_rested"))
            if total > 0:
                log(f"🦅 pass done — rested {len(stats['sessions_rested'])}, reaped {len(stats['registry_reaped'])} registry, killed {len(stats['tmux_killed'])} tmux")
                # Resting is routine and reversible; only a real cleanup is worth a ping.
                cleaned = len(stats["registry_reaped"]) + len(stats["tmux_killed"])
                if cleaned and not dry_run:
                    _send_notify(f"cleaned up {cleaned} dead sessions — keep on wolting")
        except Exception as e:
            log(f"🦅 error during reap: {e}")
        time.sleep(interval)


def main():
    args = sys.argv[1:]

    dry_run = "--dry-run" in args
    once = "--once" in args
    interval = DEFAULT_INTERVAL

    for i, arg in enumerate(args):
        if arg == "--interval" and i + 1 < len(args):
            interval = int(args[i + 1])

    if once:
        stats = reap(dry_run=dry_run)
        total = sum(len(stats[key]) for key in ("registry_reaped", "tmux_killed", "sessions_rested"))
        if total == 0:
            log("🦅 nothing to reap")
        else:
            log(f"🦅 rested {len(stats['sessions_rested'])}, reaped {len(stats['registry_reaped'])} registry, killed {len(stats['tmux_killed'])} tmux")
    else:
        run_loop(interval=interval, dry_run=dry_run)


if __name__ == "__main__":
    main()
