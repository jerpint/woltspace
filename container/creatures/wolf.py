"""
🐺 Wolf — Distributed Cron Scheduler

Each wolt registers its own schedule in wolt/wolf.json. The wolf discovers
all schedules, fires crons on time, and spawns sessions for the owning wolt.

Schedule config: {each_wolt}/wolt/wolf.json   (per-wolt)
Last-run state:  {wolts_dir}/.space/wolf/     (lodge-global — one scheduler
                 serves every wolt, so the stamps and the job journal that keep
                 a cron from firing twice cannot live inside one of them;
                 stamps are `<wolt>/<name>.last`, the journal is `jobs.jsonl`)

This module is the loop and the dispatch. What a cron means, when it is due,
and how wolf.json is read and written live in `lib/wolfcore.py`, shared with
the lodge API. Times are the lodge machine's local time.

Usage:
  python -m creatures.wolf              # Run as the background service

Managing crons is `woltspace wolf ...` (list/add/set/rm/run/runs), a client of
the lodge's /wolf/crons API.
"""

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from env_compat import get_env
import wolfcore
from wolfcore import CATCH_UP_WINDOW, STAMP_FORMAT, CronError, cron_matches  # noqa: F401

WOLTS_DIR = Path(get_env("WOLTSPACE_WOLTS_DIR", "/workspace/wolts"))


def _dialable_host(host: str) -> str:
    """A bind address turned into one a caller can actually connect to.

    Mirrors `RuntimeLayout.dialable_host`, duplicated rather than imported
    because the wolf runs from `container/` with no `woltspace` package on its
    path. Wildcards become loopback; an IPv6 literal gets its brackets, without
    which the URL below is unparseable.
    """
    raw = (host or "").strip().strip("[]")
    if raw in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if raw == "::":
        return "[::1]"
    return f"[{raw}]" if ":" in raw else raw


def server_url(path: str = "") -> str:
    """The control plane this wolf reports to.

    WOLTSPACE_API is the whole address, stamped by the connector that spawned
    us, and it is the answer whenever it is set. Behind it: host and port
    assembled by hand, for a wolf launched by something older than the stamp.

    Both halves matter. The port has to follow the instance — a native
    `woltspace start --port 8080` is an ordinary thing to do, and a wolf
    hardcoded to 7777 would fire every cron into a closed socket. So does the
    *host*: a plane bound to a LAN address or to `::` was never on 127.0.0.1
    either, and loopback-by-assumption silently dropped every callback.
    """
    api = (os.environ.get("WOLTSPACE_API") or "").strip().rstrip("/")
    if api:
        return f"{api}{path}"
    host = _dialable_host(os.environ.get("WOLTSPACE_HOST", ""))
    port = (os.environ.get("WOLTSPACE_PORT") or os.environ.get("PORT") or "7777").strip()
    return f"http://{host}:{port}{path}"


def _get_tunnel_url() -> Optional[str]:
    """Read tunnel URL from .space/platform/tunnel.json."""
    try:
        from paths import tunnel_state_file
        state = json.loads(tunnel_state_file().read_text())
        url = state.get("url", "").strip()
        return url if url else None
    except Exception:
        return None


# ── Config & State ──────────────────────────────────────────────────

def get_state_dir() -> Path:
    """Global wolf state directory at .space/wolf/."""
    from paths import space_wolf_dir
    d = space_wolf_dir(WOLTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_job(name: str, action: str, **kwargs):
    """Append a job event to the lodge-global {wolts_dir}/.space/wolf/jobs.jsonl."""
    try:
        wolfcore.append_journal(get_state_dir(), name, action, **kwargs)
    except Exception as e:
        print(f"[wolf] log error: {e}", file=sys.stderr)


def load_schedule() -> list[dict]:
    """Every wolt's crons, merged. Each is tagged `_owner` (the wolt name) and
    `_owner_dir` (its path). An unreadable wolf.json is logged and skipped."""
    crons, errors = wolfcore.load_all(WOLTS_DIR)
    for wolt, error in errors:
        print(f"[wolf] error reading {wolt}/wolt/wolf.json: {error}", file=sys.stderr)
    return crons


def get_last_run(owner: str, name: str) -> Optional[str]:
    """The last-run stamp (YYYY-MM-DD-HH:MM, local) for one wolt's cron."""
    return wolfcore.read_last_run(get_state_dir(), owner, name)


def set_last_run(owner: str, name: str, dt: datetime):
    """Record that one wolt's cron fired for this minute."""
    wolfcore.write_last_run(get_state_dir(), owner, name, dt)


def migrate_stamps(crons: list[dict]):
    """Move pre-per-wolt `<name>.last` stamps under their owning wolt (once)."""
    owners: dict[str, list[str]] = {}
    for c in crons:
        if c.get("name"):
            owners.setdefault(c["name"], []).append(c["_owner"])
    try:
        for moved in wolfcore.migrate_legacy_stamps(get_state_dir(), owners):
            print(f"[wolf] migrated last-run stamp → {moved}")
    except OSError as e:
        print(f"[wolf] stamp migration error: {e}", file=sys.stderr)


# ── Actions ─────────────────────────────────────────────────────────

def send_wolf_notify(message: str):
    """Send a 🐺 wolf notification via the server."""
    full_message = f"🐺 *Howl*\n\n{message}"

    # Use the notify endpoint directly (no session context needed)
    payload = json.dumps({"message": full_message, "session": ""})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", server_url("/notify"),
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
    """Spawn a session for the owning wolt. Returns the session URL if available.

    Goes through the lodge's /sessions/new/lodge, i.e. start_session() — the
    single entry point for all session creation. The session runs in the
    wolt's directory, with the wolt's identity and skills.
    """
    prompt = entry.get("prompt", "")
    owner = entry.get("_owner", "")
    if not prompt:
        print(f"[wolf] {entry.get('name', '?')}: no prompt specified", file=sys.stderr)
        return None
    if not owner:
        print(f"[wolf] {entry.get('name', '?')}: no _owner set", file=sys.stderr)
        return None

    print(f"[wolf] dispatching session for {owner}: {prompt[:80]}")

    payload = json.dumps({"prompt": prompt, "wolt": owner})
    try:
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", server_url("/sessions/new/lodge"),
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
    try:
        if wolfcore.remove_entry(WOLTS_DIR, wolt_name, cron_name):
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

# Entries already reported as unreadable, so a broken cron is logged once per
# change rather than every 30 seconds.
_warned: set[tuple] = set()


def _skip(entry: dict, reason: str):
    key = (entry.get("_owner"), entry.get("name"), entry.get("schedule"), entry.get("at"), reason)
    if key not in _warned:
        _warned.add(key)
        print(f"[wolf] skipping {entry.get('_owner', '?')}/{entry.get('name', '?')}: {reason}",
              file=sys.stderr)


def check_and_fire(crons: list[dict], now: datetime, *, catching_up: bool = False):
    """Fire every cron that is due at `now`.

    The loop looks at the current minute; `catching_up` (once, at start-up)
    looks back over CATCH_UP_WINDOW for the most recent match a down wolf
    missed, honouring each cron's `catch_up: false`. Either way the decision is
    `wolfcore.due_slot` — one rule. A bad entry is skipped, never fatal.
    """
    lookback = CATCH_UP_WINDOW if catching_up else timedelta(0)
    for entry in crons:
        name = entry.get("name")
        owner = entry.get("_owner", "")
        if not name:
            continue
        if catching_up and entry.get("catch_up") is False:
            continue
        if not (wolfcore.NAME_RE.match(name) and wolfcore.NAME_RE.match(owner)):
            _skip(entry, "name must be letters, digits, '-' or '_' only")
            continue
        try:
            slot = wolfcore.due_slot(entry, now, get_last_run(owner, name), lookback)
        except CronError as e:
            _skip(entry, str(e))
            continue
        if slot is None:
            continue

        one_off = bool(entry.get("at"))
        when = f"at {entry['at']}" if one_off else f"schedule: {entry.get('schedule')}"
        print(f"[wolf] {'catch-up' if catching_up else 'firing'}: {name} (owner: {owner}, {when})")
        set_last_run(owner, name, slot)
        if catching_up and not one_off:
            _log_job(name, "session", event="catch-up", owner=owner,
                     missed=slot.strftime(STAMP_FORMAT))
        fire_cron(entry)
        if one_off:
            remove_cron(owner, name)


def catch_up(crons: list[dict], now: datetime):
    """Fire any crons that were missed while the wolf was down (most recent
    match within CATCH_UP_WINDOW, once), and any one-off whose time passed."""
    check_and_fire(crons, now, catching_up=True)


async def run_loop():
    """Main wolf loop — checks every 30 seconds."""
    print("[wolf] 🐺 wolf scheduler starting (distributed mode)")
    print(f"[wolf] scanning {WOLTS_DIR}/*/wolt/wolf.json")

    # Catch up on anything missed while the wolf was down
    try:
        crons = load_schedule()
        migrate_stamps(crons)
        if crons:
            owners = set(c.get("_owner", "?") for c in crons)
            print(f"[wolf] found {len(crons)} crons from {len(owners)} wolts: {', '.join(sorted(owners))}")
            catch_up(crons, wolfcore.now_local())
        else:
            print("[wolf] no crons registered — wolf is idle")
    except Exception as e:
        print(f"[wolf] catch-up error: {e}", file=sys.stderr)

    while True:
        try:
            crons = load_schedule()
            if crons:
                check_and_fire(crons, wolfcore.now_local())
        except Exception as e:
            print(f"[wolf] error in run loop: {e}", file=sys.stderr)

        await asyncio.sleep(30)


# ── Fire by name ───────────────────────────────────────────────────

def fire_by_name(name: str, wolt: str = "") -> bool:
    """Fire a cron now, ignoring its schedule (the dog's `fire_wolf` tool).

    `wolt` narrows it when several wolts share a name; without it the first
    match fires. Journaled as a manual fire. True if found and fired.
    """
    for entry in load_schedule():
        if entry.get("name") == name and (not wolt or entry.get("_owner") == wolt):
            print(f"[wolf] manually firing: {name} (owner: {entry.get('_owner', '?')})")
            _log_job(name, "session", event="manual", owner=entry.get("_owner", ""))
            fire_cron(entry)
            return True
    print(f"[wolf] cron '{name}' not found", file=sys.stderr)
    return False


def run():
    asyncio.run(run_loop())


if __name__ == "__main__":
    run()
