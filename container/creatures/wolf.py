"""
🐺 Wolf — Distributed Cron Scheduler

Each wolt registers its own schedule in wolt/wolf.json. The wolf discovers
all schedules, fires crons on time, and spawns sessions for the owning wolt.

Schedule config: {each_wolt}/wolt/wolf.json
Last-run state:  {wolf_wolt}/.state/wolf/  (per-wolt)

Usage:
  python -m creatures.wolf              # Run as background service
  python -m creatures.wolf --once       # Fire all due crons once and exit
  python -m creatures.wolf --list       # List registered crons
  python -m creatures.wolf --fire NAME  # Fire a specific cron by name (ignores schedule)
  python -m creatures.wolf --jobs [N]   # Show last N job log entries (default 20)
"""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# ── Cached helpers ─────────────────────────────────────────────────

WOLTS_DIR = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))



def _get_tunnel_url() -> Optional[str]:
    """Read tunnel URL from .space/platform/tunnel.json."""
    try:
        import json
        from lib.paths import tunnel_state_file
        state = json.loads(tunnel_state_file().read_text())
        url = state.get("url", "").strip()
        return url if url else None
    except Exception:
        return None


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

def get_state_dir() -> Path:
    """Global wolf state directory at .space/wolf/."""
    from paths import space_wolf_dir
    d = space_wolf_dir(WOLTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_job(name: str, action: str, **kwargs):
    """Append a job event to {wolt}/.state/wolf/jobs.jsonl."""
    log_file = get_state_dir() / "jobs.jsonl"
    entry = {
        "ts": datetime.now().isoformat(),
        "cron": name,
        "action": action,
        **kwargs,
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception as e:
        print(f"[wolf] log error: {e}", file=sys.stderr)


def load_schedule() -> list[dict]:
    """Discover crons from all wolts.

    Scans wolts/*/wolt/wolf.json, merges all crons into one list.
    Each cron is tagged with _owner (the wolt name) and _owner_dir (the wolt path).

    Format per wolt:
    {
      "crons": [
        {
          "name": "digest",
          "schedule": "0 6 * * *",
          "prompt": "/digest",
          "notify": "digest time"
        },
        {
          "name": "check-ci",
          "at": "2026-03-21T20:00",
          "prompt": "Check if PR #215 passed CI"
        }
      ]
    }
    """
    all_crons = []
    for wolf_json in sorted(WOLTS_DIR.glob("*/wolt/wolf.json")):
        wolt_dir = wolf_json.parent.parent
        wolt_name = wolt_dir.name
        try:
            data = json.loads(wolf_json.read_text())
            for cron in data.get("crons", []):
                cron["_owner"] = wolt_name
                cron["_owner_dir"] = str(wolt_dir)
                all_crons.append(cron)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[wolf] error reading {wolf_json}: {e}", file=sys.stderr)
    return all_crons


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
    import subprocess
    full_message = f"🐺 *Howl*\n\n{message}"

    # Use the notify endpoint directly (no session context needed)
    payload = json.dumps({"message": full_message, "session": ""})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "http://localhost:7777/notify",
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


def dispatch_session(entry: dict) -> Optional[str]:
    """Spawn a Claude Code session for the owning wolt. Returns session URL if available.

    Uses start_session() from sessions.py — the single entry point for all session creation.
    The session runs in the wolt's directory, with the wolt's identity and skills.
    """
    import subprocess
    prompt = entry.get("prompt", "")
    owner = entry.get("_owner", "")
    if not prompt:
        print(f"[wolf] {entry.get('name', '?')}: no prompt specified", file=sys.stderr)
        return None
    if not owner:
        print(f"[wolf] {entry.get('name', '?')}: no _owner set", file=sys.stderr)
        return None

    print(f"[wolf] dispatching session for {owner}: {prompt[:80]}")

    # Use the server's session spawn endpoint — pass the owning wolt
    payload = json.dumps({
        "prompt": prompt,
        "wolt": owner,
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "http://localhost:7777/sessions/new/lodge",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        print(f"[wolf] session response: {result.stdout[:200]}")
        resp = json.loads(result.stdout) if result.stdout else {}
        session_name = resp.get("name")
        session_url = resp.get("url")
        if session_url:
            return session_url
        elif session_name:
            tunnel_url = _get_tunnel_url()
            if tunnel_url:
                return f"{tunnel_url}/tui?session={session_name}"
        return None
    except Exception as e:
        print(f"[wolf] session dispatch error: {e}", file=sys.stderr)
        send_wolf_notify(f"cron '{entry.get('name', '?')}' failed to dispatch: {e}")
        return None


def remove_cron(wolt_name: str, cron_name: str):
    """Delete a one-off cron from the wolt's wolf.json after firing."""
    path = WOLTS_DIR / wolt_name / "wolt" / "wolf.json"
    try:
        data = json.loads(path.read_text())
        data["crons"] = [c for c in data.get("crons", []) if c.get("name") != cron_name]
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"[wolf] removed one-off cron '{cron_name}' from {wolt_name}/wolt/wolf.json")
    except Exception as e:
        print(f"[wolf] failed to remove cron '{cron_name}' from {wolt_name}: {e}", file=sys.stderr)


CREATURE_EMOJI = {
    "raccoon": "🦝", "beaver": "🦫", "otter": "🦦", "rodent": "🦫",
}


def _get_wolt_emoji(wolt_name: str) -> str:
    """Get the creature emoji for a wolt by reading its wolt.json."""
    try:
        wolt_json = WOLTS_DIR / wolt_name / "wolt" / "wolt.json"
        data = json.loads(wolt_json.read_text())
        return CREATURE_EMOJI.get(data.get("type", ""), "🐾")
    except Exception:
        return "🐾"


def fire_cron(entry: dict):
    """Execute a cron entry — dispatch session, then always notify with link."""
    name = entry.get("name", "unnamed")
    owner = entry.get("_owner", "?")

    _log_job(name, "session", event="started", owner=owner)

    # Dispatch session for the owning wolt
    link = dispatch_session(entry)

    _log_job(name, "session", event="dispatched", owner=owner, link=link)

    # Build notification message
    emoji = _get_wolt_emoji(owner)
    custom_msg = entry.get("notify")
    if custom_msg:
        notify_body = f'{emoji} {owner} has been notified: "{custom_msg}"'
    else:
        notify_body = f"{emoji} {owner} has been woken up"
    if link:
        notify_body = f"{notify_body}\n{link}"
    send_wolf_notify(notify_body)


# ── Main loop ───────────────────────────────────────────────────────

def check_and_fire(crons: list[dict], now: datetime):
    """Check all crons and fire any that are due."""
    for entry in crons:
        name = entry.get("name")
        if not name:
            continue

        # --- One-off: "at" field ---
        at = entry.get("at")
        if at:
            try:
                fire_time = datetime.fromisoformat(at)
                # Make naive timestamps aware using local timezone
                if fire_time.tzinfo is None:
                    fire_time = fire_time.astimezone()
            except ValueError:
                print(f"[wolf] {name}: invalid 'at' timestamp: {at}", file=sys.stderr)
                continue

            if now >= fire_time:
                owner = entry.get("_owner", "?")
                print(f"[wolf] firing one-off: {name} (owner: {owner}, at: {at})")
                # Use at-based stamp so it won't re-fire
                set_last_run(name, now)
                fire_cron(entry)
                remove_cron(entry.get("_owner", ""), name)
            continue

        # --- Recurring: "schedule" field ---
        schedule = entry.get("schedule")
        if not schedule:
            continue

        local_now = now

        if not cron_matches(schedule, local_now):
            continue

        # Idempotency: check if already fired this minute
        stamp = local_now.strftime("%Y-%m-%d-%H:%M")
        last = get_last_run(name)
        if last == stamp:
            continue

        owner = entry.get("_owner", "?")
        print(f"[wolf] firing: {name} (owner: {owner}, schedule: {schedule})")
        set_last_run(name, local_now)
        fire_cron(entry)


def catch_up(crons: list[dict], now: datetime):
    """Fire any crons that were missed while the wolf was down.

    For each recurring cron, walks backwards from now minute-by-minute (up to 24h)
    looking for the most recent time the schedule would have matched.
    If that time is after the last recorded run, fires the cron.

    One-off crons with "at" in the past are also fired.
    """
    for entry in crons:
        name = entry.get("name")
        if not name:
            continue

        # Opt out of catch-up per cron
        if entry.get("catch_up") is False:
            continue

        # One-off catch-up: if "at" is in the past, fire it
        at = entry.get("at")
        if at:
            try:
                fire_time = datetime.fromisoformat(at)
                if fire_time.tzinfo is None:
                    fire_time = fire_time.astimezone()
            except ValueError:
                continue

            if now >= fire_time:
                last = get_last_run(name)
                if last is None:  # never fired
                    owner = entry.get("_owner", "?")
                    print(f"[wolf] catch-up one-off: {name} (owner: {owner}, at: {at})")
                    set_last_run(name, now)
                    fire_cron(entry)
                    remove_cron(entry.get("_owner", ""), name)
            continue

        # Recurring catch-up
        schedule = entry.get("schedule")
        if not schedule:
            continue

        local_now = now
        last = get_last_run(name)

        # Walk backwards minute by minute to find the most recent match
        # (cap at 24h to avoid runaway loops)
        check = local_now.replace(second=0, microsecond=0)
        most_recent_match = None
        for _ in range(24 * 60):
            if cron_matches(schedule, check):
                most_recent_match = check
                break
            check -= timedelta(minutes=1)

        if most_recent_match is None:
            continue

        match_stamp = most_recent_match.strftime("%Y-%m-%d-%H:%M")
        if last == match_stamp:
            continue  # already fired for this window

        owner = entry.get("_owner", "?")
        print(f"[wolf] catch-up: {name} (owner: {owner}, missed {match_stamp}, last run: {last or 'never'})")
        set_last_run(name, most_recent_match)
        _log_job(name, "session", event="catch-up", owner=owner, missed=match_stamp)
        fire_cron(entry)


async def run_loop():
    """Main wolf loop — checks every 30 seconds."""
    print("[wolf] 🐺 wolf scheduler starting (distributed mode)")
    print(f"[wolf] scanning {WOLTS_DIR}/*/wolt/wolf.json")

    # Catch up on anything missed while the wolf was down
    try:
        crons = load_schedule()
        if crons:
            owners = set(c.get("_owner", "?") for c in crons)
            print(f"[wolf] found {len(crons)} crons from {len(owners)} wolts: {', '.join(sorted(owners))}")
            now = datetime.now().astimezone()
            catch_up(crons, now)
        else:
            print("[wolf] no crons registered — wolf is idle")
    except Exception as e:
        print(f"[wolf] catch-up error: {e}", file=sys.stderr)

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
        print(f"No crons registered. Wolts can add crons to their wolt/wolf.json.")
        return

    # Group by owner
    by_owner = {}
    for c in crons:
        owner = c.get("_owner", "?")
        by_owner.setdefault(owner, []).append(c)

    for owner in sorted(by_owner):
        print(f"\n  {owner}:")
        for entry in by_owner[owner]:
            name = entry.get("name", "?")
            schedule = entry.get("schedule", "")
            at = entry.get("at", "")
            when = schedule or f"at {at}"
            last = get_last_run(name) or "never"
            notify = "🔔" if entry.get("notify") else "  "
            print(f"    {notify} {name:<25} {when:<25} last: {last}")


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
        if not name:
            continue

        # One-off
        at = entry.get("at")
        if at:
            try:
                fire_time = datetime.fromisoformat(at)
                if fire_time.tzinfo is None:
                    fire_time = fire_time.astimezone()
            except ValueError:
                continue
            if now >= fire_time:
                print(f"  [fire] {name} (one-off, owner: {entry.get('_owner', '?')})")
                set_last_run(name, now)
                fire_cron(entry)
                remove_cron(entry.get("_owner", ""), name)
                fired += 1
            continue

        # Recurring
        schedule = entry.get("schedule")
        if not schedule:
            continue

        if cron_matches(schedule, now):
            stamp = now.strftime("%Y-%m-%d-%H:%M")
            last = get_last_run(name)
            if last == stamp:
                print(f"  [skip] {name} — already fired at {stamp}")
                continue
            print(f"  [fire] {name} (owner: {entry.get('_owner', '?')})")
            set_last_run(name, now)
            fire_cron(entry)
            fired += 1

    if fired == 0:
        print("[wolf] nothing due right now")


# ── Fire by name ───────────────────────────────────────────────────

def fire_by_name(name: str) -> bool:
    """Fire a specific cron by name, ignoring its schedule. Returns True if found and fired."""
    crons = load_schedule()
    for entry in crons:
        if entry.get("name") == name:
            print(f"[wolf] manually firing: {name} (owner: {entry.get('_owner', '?')})")
            fire_cron(entry)
            return True
    print(f"[wolf] cron '{name}' not found", file=sys.stderr)
    return False


# ── Entry point ─────────────────────────────────────────────────────

def show_jobs(count: int = 20):
    """Show recent job log entries."""
    log_file = get_state_dir() / "jobs.jsonl"
    if not log_file.exists():
        print("No job log yet. Jobs are logged when crons fire.")
        return
    lines = log_file.read_text().strip().split("\n")
    recent = lines[-count:]
    print(f"🐺 last {min(count, len(recent))} job events:\n")
    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("ts", "")[:19]
            cron = entry.get("cron", "?")
            event = entry.get("event", "?")
            owner = entry.get("owner", "")
            extra = ""
            if owner:
                extra = f" [{owner}]"
            if entry.get("session"):
                extra += f" session={entry['session']}"
            if entry.get("error"):
                extra += f" error={entry['error']}"
            if entry.get("link"):
                extra += f" {entry['link']}"
            print(f"  {ts}  {cron:<20} {event:<12}{extra}")
        except json.JSONDecodeError:
            continue


def run():
    args = sys.argv[1:]
    if "--list" in args:
        list_crons()
    elif "--jobs" in args:
        n = 20
        if "--jobs" in args:
            idx = args.index("--jobs")
            if idx + 1 < len(args) and args[idx + 1].isdigit():
                n = int(args[idx + 1])
        show_jobs(n)
    elif "--fire" in args:
        idx = args.index("--fire")
        if idx + 1 >= len(args):
            print("usage: --fire <cron-name>", file=sys.stderr)
            sys.exit(1)
        name = args[idx + 1]
        if not fire_by_name(name):
            sys.exit(1)
    elif "--once" in args:
        run_once()
    else:
        asyncio.run(run_loop())


if __name__ == "__main__":
    run()
