"""
🦅 Vulture — Session Reaper

The vulture circles the lodge, cleaning up dead sessions before they
pile up and choke the system. Runs as a platform-level background
process — not managed by wolves.

It does three things on each pass:
  0. Rests running sessions nobody has used for the owner-selected window,
     unless they hold. Use is the later of the record's last_activity (every
     lodge write: a message in or out, a wake, a page push) and tmux's
     session_activity (an attach or a keystroke); output is not use. A wolt
     keeps a session up for a background task with `woltspace session hold`.
  1. Reconciles the registry — a "running" session whose tmux session is
     gone rests if it can resume, and is marked "reaped" if it cannot. An
     engine that picks its own conversation id (codex, opencode, hermes)
     may never have had it stamped; it is recovered from the engine's
     files first. A hold does not keep a dead session.
  2. Kills zombie tmux sessions — lodge sessions where the agent has
     exited but the tmux session lingers. A tmux session the registry
     does not know is never touched: natively it is the owner's own.
  Passes 1 and 2 settle under the session lock, after a second look, so a
  session a message woke in the meantime is never written resting.

Config: sessions.idle_timeout_seconds in woltspace.json (default: 1 day; null = never).
Logs:   .space/vulture/vulture.log
State:  .space/vulture/last-run

Usage:
  python -m creatures.vulture              # Run as background service (default: every 10 min)
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
import lodge_auth
from sessions import (
    SessionRegistry, claim_resume_id, recover_resume_id, stamp_resume_id,
    still_as_seen, stop_and_settle, stored_resume_id, taken_resume_ids,
)
from harnesses import session_has_agent_process
from paths import space_vulture_dir
from session_runtime import RuntimeHandle, get_runtime
from session_expiry import get_idle_timeout, hold_active, last_use
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

# Records already done whose tmux may linger; pass 2 cleans that tmux up.
LEFTOVER_STATUSES = ("reaped", "orphaned", "completed", "failed", "resting")

DEFAULT_INTERVAL = 600  # 10 minutes

# How long the first pass waits for the lodge API at start.
API_WAIT_SECONDS = 60

# A held session is named in the log at most this often, so a forgotten
# forever hold stays visible without flooding the log.
HELD_LOG_EVERY = 3600
_held_logged: dict[str, int] = {}


def _send_notify(message: str):
    """Send a vulture notification via the server — only when there's something to report."""
    full_message = f"🦅 {message}"
    payload = json.dumps({"message": full_message, "session": ""})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST",
             os.environ.get("WOLTSPACE_API", "http://localhost:7777") + "/notify",
             "-H", "Content-Type: application/json",
             "-d", payload, "-K", "-"],
            input=lodge_auth.curl_config(),
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
    lodge_auth.sign(req)
    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            return 200 <= response.status < 300
    except (OSError, urlerror.URLError):
        return False


def _api_ready() -> bool:
    """Whether the lodge API answers GET /health."""
    base = os.environ.get("WOLTSPACE_API", "http://localhost:7777").rstrip("/")
    try:
        with urlrequest.urlopen(f"{base}/health", timeout=3) as response:
            return 200 <= response.status < 300
    except (OSError, urlerror.URLError, ValueError):
        return False


def _wait_for_api(limit: float = API_WAIT_SECONDS, step: float = 2.0,
                  clock=time.monotonic, sleep=time.sleep) -> bool:
    """Wait for the lodge API before the first pass: idle rests go through it.

    At a lodge restart the vulture starts before the API answers, and every
    rest of the first pass failed. Gives up after `limit` seconds and circles
    anyway: deaths need no API, and the next pass retries the rests.
    """
    deadline = clock() + limit
    while not _api_ready():
        if clock() >= deadline:
            log(f"🦅 lodge API not answering after {int(limit)}s; circling anyway")
            return False
        sleep(step)
    return True


def _settle_dead(data: dict, now: int, reason: str) -> str:
    """Mark a session whose runtime is gone. One that can come back rests.

    "resting" sessions wake when someone messages them; "reaped" ones refuse.
    A dead session that still has its conversation id is no different from
    one the vulture rested for being idle, so it gets the same status. A hold
    does not keep it: the task it covered died with the session, and the
    registry drops the hold on this write.
    """
    status = "resting" if stored_resume_id(data) else "reaped"
    data["status"] = status
    if status == "resting":
        data["rested_at"] = now
        data["rest_reason"] = reason
    else:
        data["finished_at"] = now
    data["last_activity"] = now
    return status


def _settle_if_still_dead(reg: SessionRegistry, wolt: str, name: str, now: int,
                          reason: str, still_dead, stop=None) -> str | None:
    """Settle one session, if it is still running and still dead.

    The pass works from a snapshot taken at its start; a message can resume a
    session in the meantime (resume_session spawns a new tmux and writes
    running). Read again and ask the runtime again, then settle only if
    nothing that decides it changed since (registry.claim), so a woken
    session is never written resting while it runs. Returns the new status,
    or None when the session was left alone.

    `stop` (pass 2's tmux kill) goes through stop_and_settle, as /rest does: the
    record is `stopping` while tmux dies, so a message waits for it and then
    wakes the session in a fresh one, instead of reviving the agent in a pane
    about to be killed. A failed stop leaves the session running.

    A session without a stored conversation id first gets one recovered from
    its engine's files, as an idle rest does, so it rests instead of being
    reaped; outside the write, since recovery reads the engine's files.
    """
    data = reg.get(name, wolt=wolt, check_alive=False)
    if data is not None and data.get("status") == "running" and not stored_resume_id(data):
        claim_resume_id(reg, data, lambda d: stamp_resume_id(reg, wolt, name, d["harness_session_id"]))
    current = reg.get(name, wolt=wolt, check_alive=False)
    if current is None or current.get("status") != "running" or not still_dead(current):
        return None
    settled = []
    if stop is None:
        with reg.edit(name, wolt=wolt) as record:
            if still_as_seen(record, current):
                settled.append(_settle_dead(record, now, reason))
    elif not stop_and_settle(reg, name, current, lambda seen: stop(),
                          lambda record: settled.append(_settle_dead(record, now, reason)),
                          wolt=wolt):
        return None
    return settled[0] if settled else None


def _log_held(data: dict, now: int) -> None:
    name = data["name"]
    if name in _held_logged and now - _held_logged[name] < HELD_LOG_EVERY:
        return
    _held_logged[name] = now
    hold = data["hold"]
    until = hold.get("until")
    # UTC, like every line's stamp: local time read as a hold already over.
    span = "forever" if until is None else "until " + datetime.fromtimestamp(until, timezone.utc).strftime("%Y-%m-%d %H:%M")
    log(f"🦅 held ({span}): {name}: {hold.get('reason', '')}")


def _rest_idle_sessions(reg: SessionRegistry, live_tmux: set[str], now: int,
                        timeout: int | None, dry_run: bool) -> list[str]:
    """Rest running sessions nobody has used for the configured window.

    Use is the later of the record's last_activity (every lodge write) and
    tmux's session_activity (an attach or a keystroke). Output is not use.
    """
    if timeout is None:
        return []
    try:
        keys = get_runtime().last_keys()
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
        if hold_active(data, now):
            _log_held(data, now)
            continue
        used = last_use(data, keys)
        if used is None or now - used < timeout:
            continue
        # Resting must be reversible: never stop a session resume_session could not bring back.
        if not recover_resume_id(data, taken):
            continue
        if dry_run:
            log(f"[dry-run] would rest idle session: {name}")
            rested.append(name)
        elif _rest_via_api(name, used):
            log(f"🦅 rested idle session: {name}")
            rested.append(name)
        else:
            log(f"could not rest idle session through API: {name}")
    return rested


def reap(dry_run: bool = False) -> dict:
    """
    Single reaper pass. Returns stats dict:
      { "registry_reaped": [...], "tmux_killed": [...], "sessions_rested": [...],
        "cleaned": [...], "errors": [...] }
    "cleaned" is what is gone for good: deaths settled reaped, and leftover
    tmux killed. A death that rests can still wake, like an idle rest.
    """
    reg = SessionRegistry(WOLTS_DIR)
    now = int(time.time())
    live_tmux = _tmux_sessions()
    stats = {"registry_reaped": [], "tmux_killed": [], "sessions_rested": [], "cleaned": [], "errors": []}

    stats["sessions_rested"] = _rest_idle_sessions(
        reg, live_tmux, now, get_idle_timeout(), dry_run
    )

    # --- Pass 1: Registry reconciliation ---
    # Find "running" registry entries whose tmux session is dead.
    for wolt_name in reg._all_wolts():
        for data in reg.records(wolt=wolt_name):
            name = data.get("name", "")
            status = data.get("status", "")
            created_at = data.get("created_at", 0)

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
                    status = _settle_if_still_dead(
                        reg, wolt_name, name, now, "tmux-session-missing",
                        lambda current: not get_runtime().is_alive(RuntimeHandle.from_record(current)),
                    )
                    if status is None:
                        continue
                    log(f"🦅 {status} registry: {name}")
                    if status == "reaped":
                        stats["cleaned"].append(name)
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
            # The agent exited but tmux lingers — kill tmux, rest or reap the record.
            # The agent is looked for again under the lock: a resume can revive it
            # in this very pane between the look above and the kill.
            if dry_run:
                log(f"[dry-run] would kill zombie tmux + reap: {session_name}")
            else:
                status = _settle_if_still_dead(
                    reg, wolt_name, session_name, now, "agent-process-missing",
                    lambda current: session_has_agent_process(current) is False,
                    stop=lambda: _kill_tmux_session(session_name),
                )
                if status is None:
                    continue
                log(f"🦅 killed zombie tmux, {status}: {session_name}")
                if status == "reaped":
                    stats["cleaned"].append(session_name)
            stats["tmux_killed"].append(session_name)
        elif reg_data and reg_data.get("status") in LEFTOVER_STATUSES:
            # Already reaped/done but tmux still exists — clean up tmux, unless
            # a resume made the record running again since it was read.
            if dry_run:
                log(f"[dry-run] would kill leftover tmux: {session_name}")
            else:
                wolt_name = reg_data.get("wolt", "")
                current = reg.get(session_name, wolt=wolt_name, check_alive=False)
                if not current or current.get("status") not in LEFTOVER_STATUSES:
                    continue
                if not stop_and_settle(reg, session_name, current,
                                    lambda seen: _kill_tmux_session(session_name),
                                    lambda record: record.update(status=current["status"]),
                                    wolt=wolt_name):
                    continue
                log(f"🦅 killed leftover tmux: {session_name}")
                stats["cleaned"].append(session_name)
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
    _wait_for_api()
    while True:
        try:
            stats = reap(dry_run=dry_run)
            total = sum(len(stats[key]) for key in ("registry_reaped", "tmux_killed", "sessions_rested"))
            if total > 0:
                log(f"🦅 pass done — rested {len(stats['sessions_rested'])}, reaped {len(stats['registry_reaped'])} registry, killed {len(stats['tmux_killed'])} tmux")
                # Resting is routine and reversible, a dead session that rests
                # included; only what is gone for good is worth a ping.
                cleaned = len(stats["cleaned"])
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
