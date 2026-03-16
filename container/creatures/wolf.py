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
  python -m creatures.wolf --fire NAME  # Fire a specific cron by name (ignores schedule)
  python -m creatures.wolf --jobs [N]   # Show last N job log entries (default 20)
"""

import asyncio
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── Cached helpers ─────────────────────────────────────────────────

_wolf_name_cache: Optional[str] = None


def _get_wolf_name() -> str:
    """Read wolf name from wolt.json, cached after first call."""
    global _wolf_name_cache
    if _wolf_name_cache is not None:
        return _wolf_name_cache
    try:
        wolt_json = get_wolt_dir() / "wolt" / "wolt.json"
        data = json.loads(wolt_json.read_text())
        _wolf_name_cache = data.get("name", "wolf")
    except Exception:
        _wolf_name_cache = "wolf"
    return _wolf_name_cache


def _get_tunnel_url() -> Optional[str]:
    """Read tunnel URL from .state/tunnel-url."""
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    tunnel_file = wolts_dir / ".state" / "tunnel-url"
    if tunnel_file.exists():
        url = tunnel_file.read_text().strip()
        return url if url else None
    return None


def _make_session_name(cron_name: str) -> str:
    """Generate a tmux session name: {wolf}-{cron}-{hex6}."""
    wolf = _get_wolf_name()
    hex6 = secrets.token_hex(3)
    return f"{wolf}-{cron_name}-{hex6}"

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

def _find_wolf_wolt() -> Optional[Path]:
    """Find the active wolf-wolt directory, if one exists."""
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    config_file = wolts_dir / "woltspace.json"
    # Check woltspace.json for active_wolf
    if config_file.exists():
        try:
            config = json.loads(config_file.read_text())
            active_wolf = config.get("creatures", {}).get("active_wolf")
            if active_wolf:
                wolf_dir = wolts_dir / active_wolf
                if (wolf_dir / "wolt" / "wolf.json").exists():
                    return wolf_dir
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: scan for any wolt with type=wolf
    for wolt_json in wolts_dir.glob("*/wolt/wolt.json"):
        try:
            data = json.loads(wolt_json.read_text())
            if data.get("type") == "wolf":
                return wolt_json.parent.parent
        except (json.JSONDecodeError, OSError):
            continue
    return None


def get_wolt_dir() -> Path:
    """Get the wolf's working directory.

    Prefers a dedicated wolf-wolt (type: wolf) if one exists.
    Falls back to the active rodent-wolt for backwards compat.
    """
    wolf_wolt = _find_wolf_wolt()
    if wolf_wolt:
        return wolf_wolt
    return Path(os.environ.get("WOLT_DIR", "/workspace/wolt"))


def get_schedule_path() -> Path:
    return get_wolt_dir() / "wolt" / "wolf.json"


def get_state_dir() -> Path:
    d = get_wolt_dir() / ".state" / "wolf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_job(name: str, action: str, **kwargs):
    """Append a job event to .state/wolf/jobs.jsonl."""
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
    wolf_name = _get_wolf_name()
    full_message = f"🐺 {wolf_name}: {message}"

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


def run_script(entry: dict) -> Optional[str]:
    """Run a shell command inside a tmux session. Returns session name."""
    command = entry.get("command", "")
    cron_name = entry.get("name", "script")
    if not command:
        print(f"[wolf] {cron_name}: no command specified", file=sys.stderr)
        return None

    session_name = _make_session_name(cron_name)
    wolt_dir = str(get_wolt_dir())
    wolf_name = _get_wolf_name()

    # Build notification JSON payloads (avoid shell quoting hell by using heredocs)
    tunnel_url = _get_tunnel_url()
    tui_link = f"\\n{tunnel_url}/tui?session={session_name}" if tunnel_url else ""
    ok_msg = f"🐺 {wolf_name}: cron '{cron_name}' completed successfully{tui_link}"
    fail_prefix = f"🐺 {wolf_name}: cron '{cron_name}' failed (exit "
    notify_url = "http://localhost:7777/notify"

    # Export the wolf's own env so cron scripts use the right state directory
    # (without this, scripts inherit WOLT_DIR from the container entrypoint,
    # which points to the rodent-wolt, not the wolf-wolt)
    env_setup = f"export WOLT_DIR={wolt_dir} WOLT_NAME={wolf_name}; "

    # Wrap: run command, capture exit code, notify completion, keep terminal open
    wrapped = (
        f"{env_setup}{command}; _exit=$?; "
        f"_notify() {{ curl -s -X POST {notify_url} -H 'Content-Type: application/json' "
        f"""-d "$(printf '{{"message":"%s","session":""}}' "$1")"; }}; """
        f'if [ "$_exit" -eq 0 ]; then '
        f'  _notify "{ok_msg}"; '
        f"else "
        f'  _notify "{fail_prefix}$_exit)"; '
        f"fi; "
        f"exec bash"
    )

    print(f"[wolf] running script in tmux session: {session_name}")
    try:
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", session_name, "-c", wolt_dir,
             "bash", "-c", wrapped],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return session_name
    except Exception as e:
        print(f"[wolf] script error: {e}", file=sys.stderr)
        _log_job(cron_name, "script", event="error", error=str(e))
        send_wolf_notify(f"cron '{cron_name}' failed to start: {e}")
        return None


def run_session(entry: dict) -> Optional[str]:
    """Dispatch a Claude Code session via the bot's API. Returns session URL if available."""
    prompt = entry.get("prompt", "")
    creature = entry.get("creature", "beaver")
    if not prompt:
        print(f"[wolf] {entry['name']}: no prompt specified", file=sys.stderr)
        return None

    print(f"[wolf] dispatching {creature} session: {prompt[:80]}")

    # Use the server's session spawn endpoint
    payload = json.dumps({
        "prompt": prompt,
        "creature": creature,
        "wolt": os.environ.get("WOLT_NAME"),
    })
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "http://localhost:7777/tools/claude_code",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        print(f"[wolf] session response: {result.stdout[:200]}")
        # Parse response for session name/URL
        resp = json.loads(result.stdout) if result.stdout else {}
        session_name = resp.get("name")
        session_url = resp.get("url")
        if session_url:
            return session_url
        elif session_name:
            # Construct URL from tunnel
            tunnel_url = _get_tunnel_url()
            if tunnel_url:
                return f"{tunnel_url}/tui?session={session_name}"
        return None
    except Exception as e:
        print(f"[wolf] session dispatch error: {e}", file=sys.stderr)
        send_wolf_notify(f"cron '{entry['name']}' failed to dispatch: {e}")
        return None


def run_skill(entry: dict):
    """Run a Claude Code skill via a session."""
    skill = entry.get("skill", "")
    prompt = entry.get("prompt", f"/{skill}")
    creature = entry.get("creature", "beaver")

    print(f"[wolf] running skill /{skill}")
    run_session({**entry, "prompt": prompt, "creature": creature})


def fire_cron(entry: dict):
    """Execute a cron entry — fire action first, then notify with link."""
    name = entry.get("name", "unnamed")
    action = entry.get("action", "script")
    notify_msg = entry.get("notify")

    _log_job(name, action, event="started", command=entry.get("command", ""))

    # Fire action first (to get session name/URL)
    link = None
    session_name = None
    if action == "script":
        session_name = run_script(entry)
        if session_name:
            tunnel_url = _get_tunnel_url()
            if tunnel_url:
                link = f"{tunnel_url}/tui?session={session_name}"
    elif action == "session":
        link = run_session(entry)
    elif action == "skill":
        run_skill(entry)
    else:
        print(f"[wolf] {name}: unknown action '{action}'", file=sys.stderr)
        _log_job(name, action, event="error", error=f"unknown action '{action}'")

    _log_job(name, action, event="dispatched", session=session_name, link=link)

    # Send notification with link appended
    if notify_msg:
        msg = notify_msg
        if link:
            msg = f"{msg}\n{link}"
        send_wolf_notify(msg)


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


# ── Fire by name ───────────────────────────────────────────────────

def fire_by_name(name: str) -> bool:
    """Fire a specific cron by name, ignoring its schedule. Returns True if found and fired."""
    crons = load_schedule()
    for entry in crons:
        if entry.get("name") == name:
            print(f"[wolf] manually firing: {name}")
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
    wolf = _get_wolf_name()
    print(f"🐺 {wolf} — last {min(count, len(recent))} job events:\n")
    for line in recent:
        try:
            entry = json.loads(line)
            ts = entry.get("ts", "")[:19]
            cron = entry.get("cron", "?")
            event = entry.get("event", "?")
            extra = ""
            if entry.get("session"):
                extra = f" session={entry['session']}"
            if entry.get("error"):
                extra = f" error={entry['error']}"
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
