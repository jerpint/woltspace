"""
🐺 Wolf — Cron & Scheduler

The wolf runs the pack's routines. Fires tasks on schedule, sends
notifications, and dispatches work to other creatures when needed.

Schedule config: {wolt_dir}/wolt/wolf.json
Last-run state:  {wolt_dir}/.state/wolf/

Usage:
  python -m creatures.wolf              # Run as background service
  python -m creatures.wolf --once       # Fire all due crons once and exit
  python -m creatures.wolf --list       # List registered crons
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Cron expression parser (minimal, no deps) ──────────────────────

def _parse_field(field: str, min_val: int, max_val: int) -> set[int]:
    """Parse a single cron field into a set of valid values."""
    values = set()
    for part in field.split(","):
        if "/" in part:
            base, step = part.split("/", 1)
            step = int(step)
            if base == "*":
                start = min_val
            else:
                start = int(base)
            for v in range(start, max_val + 1, step):
                values.add(v)
        elif "-" in part:
            lo, hi = part.split("-", 1)
            for v in range(int(lo), int(hi) + 1):
                values.add(v)
        elif part == "*":
            return set(range(min_val, max_val + 1))
        else:
            values.add(int(part))
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    """Check if a cron expression matches the given datetime.

    Format: 'minute hour day-of-month month day-of-week'
    Supports: *, ranges (1-5), steps (*/15), lists (1,3,5)
    """
    parts = expr.strip().split()
    if len(parts) != 5:
        return False

    minute = _parse_field(parts[0], 0, 59)
    hour = _parse_field(parts[1], 0, 23)
    dom = _parse_field(parts[2], 1, 31)
    month = _parse_field(parts[3], 1, 12)
    dow = _parse_field(parts[4], 0, 6)  # 0=Sunday

    return (
        dt.minute in minute
        and dt.hour in hour
        and dt.day in dom
        and dt.month in month
        and dt.weekday() in {(d - 1) % 7 for d in dow}  # cron 0=Sun, python 0=Mon
    )


# ── Config & State ──────────────────────────────────────────────────

def get_wolt_dir() -> Path:
    return Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))


def get_schedule_path() -> Path:
    return get_wolt_dir() / "wolt" / "wolf.json"


def get_state_dir() -> Path:
    d = get_wolt_dir() / ".state" / "wolf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_schedule() -> list[dict]:
    """Load wolf.json schedule config.

    Format:
    {
      "crons": [
        {
          "name": "digest",
          "schedule": "0 6 * * *",
          "action": "script",
          "command": "node /workspace/woltspace/cron/digest.mjs",
          "notify": "🐺 digest time — fetching news and papers",
          "timezone": "America/Montreal"
        },
        {
          "name": "weekly-review",
          "schedule": "0 10 * * 1",
          "action": "session",
          "prompt": "Write a weekly review of what we shipped",
          "creature": "beaver",
          "notify": "🐺 weekly review firing up"
        }
      ]
    }
    """
    path = get_schedule_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        return data.get("crons", [])
    except (json.JSONDecodeError, KeyError) as e:
        print(f"[wolf] error reading {path}: {e}", file=sys.stderr)
        return []


def get_last_run(name: str) -> Optional[str]:
    """Get the last run timestamp for a cron entry (YYYY-MM-DD-HH:MM)."""
    p = get_state_dir() / f"{name}.last"
    if p.exists():
        return p.read_text().strip()
    return None


def set_last_run(name: str, dt: datetime):
    """Record that a cron entry fired at this time."""
    stamp = dt.strftime("%Y-%m-%d-%H:%M")
    (get_state_dir() / f"{name}.last").write_text(stamp)


# ── Actions ─────────────────────────────────────────────────────────

def send_wolf_notify(message: str):
    """Send a 🐺 wolf notification via the server."""
    wolt_name = os.environ.get("WOLT_NAME", "wolt")
    full_message = f"🐺 {wolt_name}: {message}"

    # Use the notify endpoint directly (no session context needed)
    payload = json.dumps({"message": full_message, "session": ""})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "http://localhost:3000/notify",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        resp = json.loads(result.stdout) if result.stdout else {}
        if resp.get("ok"):
            adapter = resp.get("adapter", "?")
            print(f"[wolf] notified via {adapter}: {message}")
        else:
            print(f"[wolf] notify failed: {resp.get('error', result.stdout)}", file=sys.stderr)
    except Exception as e:
        print(f"[wolf] notify error: {e}", file=sys.stderr)


def run_script(entry: dict):
    """Run a shell command."""
    command = entry.get("command", "")
    if not command:
        print(f"[wolf] {entry['name']}: no command specified", file=sys.stderr)
        return

    print(f"[wolf] running script: {command}")
    env = {**os.environ}
    try:
        subprocess.Popen(
            command,
            shell=True,
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        print(f"[wolf] script error: {e}", file=sys.stderr)
        send_wolf_notify(f"cron '{entry['name']}' failed: {e}")


def run_session(entry: dict):
    """Dispatch a Claude Code session via the bot's API."""
    prompt = entry.get("prompt", "")
    creature = entry.get("creature", "beaver")
    if not prompt:
        print(f"[wolf] {entry['name']}: no prompt specified", file=sys.stderr)
        return

    print(f"[wolf] dispatching {creature} session: {prompt[:80]}")

    # Use the server's session spawn endpoint
    payload = json.dumps({
        "prompt": prompt,
        "creature": creature,
        "wolt": os.environ.get("WOLT_NAME"),
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "http://localhost:3000/tools/claude_code",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        print(f"[wolf] session response: {result.stdout[:200]}")
    except Exception as e:
        print(f"[wolf] session dispatch error: {e}", file=sys.stderr)
        send_wolf_notify(f"cron '{entry['name']}' failed to dispatch: {e}")


def run_skill(entry: dict):
    """Run a Claude Code skill via a session."""
    skill = entry.get("skill", "")
    prompt = entry.get("prompt", f"/{skill}")
    creature = entry.get("creature", "beaver")

    print(f"[wolf] running skill /{skill}")
    run_session({**entry, "prompt": prompt, "creature": creature})


def fire_cron(entry: dict):
    """Execute a cron entry."""
    name = entry.get("name", "unnamed")
    action = entry.get("action", "script")
    notify_msg = entry.get("notify")

    # Send notification first (deterministic, immediate)
    if notify_msg:
        send_wolf_notify(notify_msg)

    # Execute the action
    if action == "script":
        run_script(entry)
    elif action == "session":
        run_session(entry)
    elif action == "skill":
        run_skill(entry)
    else:
        print(f"[wolf] {name}: unknown action '{action}'", file=sys.stderr)


# ── Main loop ───────────────────────────────────────────────────────

def check_and_fire(crons: list[dict], now: datetime):
    """Check all crons and fire any that are due."""
    for entry in crons:
        name = entry.get("name")
        schedule = entry.get("schedule")
        if not name or not schedule:
            continue

        # Resolve timezone
        tz_name = entry.get("timezone")
        if tz_name:
            from zoneinfo import ZoneInfo
            local_now = now.astimezone(ZoneInfo(tz_name))
        else:
            local_now = now

        if not cron_matches(schedule, local_now):
            continue

        # Idempotency: check if already fired this minute
        stamp = local_now.strftime("%Y-%m-%d-%H:%M")
        last = get_last_run(name)
        if last == stamp:
            continue

        print(f"[wolf] firing: {name} (schedule: {schedule})")
        set_last_run(name, local_now)
        fire_cron(entry)


async def run_loop():
    """Main wolf loop — checks every 30 seconds."""
    print("[wolf] 🐺 wolf scheduler starting")

    schedule_path = get_schedule_path()
    if not schedule_path.exists():
        print(f"[wolf] no schedule found at {schedule_path} — wolf is idle")
        print("[wolf] create wolf.json to register crons")

    while True:
        try:
            crons = load_schedule()
            if crons:
                now = datetime.now().astimezone()
                check_and_fire(crons, now)
        except Exception as e:
            print(f"[wolf] error in run loop: {e}", file=sys.stderr)

        await asyncio.sleep(30)


def list_crons():
    """Print registered crons."""
    crons = load_schedule()
    if not crons:
        path = get_schedule_path()
        print(f"No crons registered. Create {path}")
        return

    for entry in crons:
        name = entry.get("name", "?")
        schedule = entry.get("schedule", "?")
        action = entry.get("action", "?")
        last = get_last_run(name) or "never"
        notify = "🔔" if entry.get("notify") else "  "
        print(f"  {notify} {name:<25} {schedule:<20} {action:<10} last: {last}")


def run_once():
    """Fire all due crons once and exit."""
    crons = load_schedule()
    if not crons:
        print("[wolf] no crons registered")
        return

    now = datetime.now().astimezone()
    fired = 0
    for entry in crons:
        name = entry.get("name")
        schedule = entry.get("schedule")
        if not name or not schedule:
            continue

        tz_name = entry.get("timezone")
        if tz_name:
            from zoneinfo import ZoneInfo
            local_now = now.astimezone(ZoneInfo(tz_name))
        else:
            local_now = now

        if cron_matches(schedule, local_now):
            stamp = local_now.strftime("%Y-%m-%d-%H:%M")
            last = get_last_run(name)
            if last == stamp:
                print(f"  [skip] {name} — already fired at {stamp}")
                continue
            print(f"  [fire] {name}")
            set_last_run(name, local_now)
            fire_cron(entry)
            fired += 1

    if fired == 0:
        print("[wolf] nothing due right now")


# ── Entry point ─────────────────────────────────────────────────────

def run():
    args = sys.argv[1:]
    if "--list" in args:
        list_crons()
    elif "--once" in args:
        run_once()
    else:
        asyncio.run(run_loop())


if __name__ == "__main__":
    run()
