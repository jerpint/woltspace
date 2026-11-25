"""Count the lodge's LLM token usage from engine transcripts, per wolt and day.

Two entry points, both stdlib only:

  count()  read-only: saved history plus a fresh transcript scan, built in
           memory. The usage page, `GET /api/usage` and `woltspace usage` use
           it; it never writes history or creates its lock.
  save()   the wolf's nightly job: the same count, merged into history under
           an exclusive lock and written atomically (mode 0600).

Transcripts come from two layouts. Each wolt's own engine folders
(`<wolt>/.claude/projects`, `<wolt>/.codex/sessions`), and, on a native lodge,
the operator's shared engine home (`~/.claude/projects`, `$CODEX_HOME/sessions`).
A shared-home transcript counts for a wolt only when the working folder it
recorded at its start is inside that wolt's folder, or, when it is not, when
the lodge's own session records name the session. Anything else in the shared
home is the operator's own use: it is never read past its first lines, never
counted and never listed, not even as a coverage gap.

Days are calendar days in the lodge's reporting zone, recorded in history by
its IANA name. A history saved in another zone is refused, never re-bucketed.
"""

from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import usage_claude
import usage_codex

READERS = {"claude": usage_claude, "codex": usage_codex}
DIMENSIONS = ("day", "provider", "wolt", "session_id", "model", "metric")
METRICS = ("input_fresh", "cache_read", "cache_write", "output")
HISTORY_SCHEMA = 1


class HistoryZoneMismatch(ValueError):
    """History holds days but no recorded zone; reading them in any zone would be a guess."""


def usage_dir(wolts_dir):
    return Path(wolts_dir) / ".space" / "usage"


def history_path(wolts_dir):
    return usage_dir(wolts_dir) / "history.json"


def claude_limits_path(wolts_dir):
    """Normalized Claude status-line reading, separate from token history."""
    return usage_dir(wolts_dir) / "claude-limits.json"


def machine_zone():
    """The lodge machine's local zone (the wolf's clock) as a ZoneInfo, or None without an IANA name."""
    try:
        import wolfcore
        return ZoneInfo(wolfcore.local_tz_name())
    except (ImportError, ValueError, ZoneInfoNotFoundError):
        return None


def host_bases(environ=None):
    """The operator's shared engine homes, per provider (a native lodge's engines write here)."""
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or Path.home())
    return {
        "claude": Path(environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / "projects",
        "codex": Path(environ.get("CODEX_HOME") or home / ".codex") / "sessions",
    }


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return parsed if parsed.tzinfo else None


def opaque(value):
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", str(value)):
        return value.lower()
    return "session-" + hashlib.sha256(str(value).encode()).hexdigest()[:24]


def jsonl(path, gap):
    try:
        with path.open("rb") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError
                except ValueError:
                    gap("invalid_jsonl" if line.endswith(b"\n") else "partial_final_line", None)
                    continue
                yield event
    except OSError:
        gap("transcript_unreadable", None)


def first_events(path, limit=50):
    """Up to `limit` JSON objects from the top of a transcript (other lines skipped); [] on error."""
    events = []
    try:
        with path.open("rb") as stream:
            for line in stream:
                if len(events) >= limit:
                    break
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events


class Usage:
    def __init__(self, source, counts, coverage, gap, zone, also=()):
        self.source, self.counts, self.coverage, self.gap, self.zone = source, counts, coverage, gap, zone
        self.also = also  # ids of other copies of the same transcript: covered alike

    def day(self, event):
        parsed = timestamp(event.get("timestamp"))
        if parsed is None:
            self.gap("invalid_timestamp", None)
            return None
        return parsed.astimezone(self.zone).date().isoformat()

    def cover(self, day):
        provider, wolt, file_id = self.source
        self.coverage[day, provider, wolt].update((file_id, *self.also))

    def valid(self, metrics, day):
        if not all(type(value) is int and value >= 0 for value in metrics.values()):
            self.gap("invalid_token_counts", day)
            return False
        return True

    def add(self, day, session, model, metrics):
        self.cover(day)
        if not self.valid(metrics, day):
            return False
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}", model):
            self.gap("unknown_model", day)
            model = "unknown"
        provider, wolt, _ = self.source
        for metric, amount in metrics.items():
            self.counts[day, provider, wolt, opaque(session), model, metric] += amount
        return True


def limit_window(record, percent_key, minutes):
    if not isinstance(record, dict):
        return None
    percent, reset = record.get(percent_key), record.get("resets_at")
    if type(percent) not in (int, float) or not 0 <= percent < float("inf"):
        return None
    if type(reset) is not int and timestamp(reset) is None:
        return None
    if type(minutes) is not int or minutes <= 0:
        return None
    return {"used_percent": percent, "window_minutes": minutes, "resets_at": reset}


def claude_limits(path):
    try:
        data = json.loads(path.read_bytes())
        observed = timestamp(data.get("read_at"))
        limits = data.get("rate_limits") or {}
        windows = {
            key: limit_window(limits.get(key), "used_percentage", minutes)
            for key, minutes in (("five_hour", 300), ("seven_day", 10080))
        }
    except (OSError, ValueError, AttributeError):
        return None
    windows = {key: value for key, value in windows.items() if value is not None}
    return {"observed_at": observed.isoformat(), "windows": windows} if observed and windows else None


# ── Native attribution: which wolt a shared-home transcript belongs to ──

def wolt_roots(root):
    """Canonical folder of every wolt (a directory holding wolt/wolt.json), by name."""
    roots = {}
    for wolt_dir in sorted(root.iterdir()):
        if not wolt_dir.is_symlink() and wolt_dir.is_dir() and (wolt_dir / "wolt/wolt.json").is_file():
            roots[wolt_dir.name] = Path(os.path.realpath(wolt_dir))
    return roots


def session_records(root):
    """{engine session id: wolt} from the lodge's own session records, read-only; {} without them."""
    import lodge_db
    if not lodge_db.path(root).is_file():
        return {}
    try:
        with closing(lodge_db.connect(root, readonly=True)) as connection:
            rows = connection.execute(
                "SELECT harness_session_id, wolt FROM sessions WHERE harness_session_id IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {str(row[0]).lower(): row[1] for row in rows}


def inside(path, folder):
    """Whether `path` is `folder` or lies under it, by whole path components."""
    return path.parts[:len(folder.parts)] == folder.parts


def owner_of(cwd, roots):
    """The wolt whose folder contains `cwd`, compared by whole path components; None otherwise."""
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        return None
    real = Path(os.path.realpath(cwd))
    matches = [name for name, folder in roots.items() if inside(real, folder)]
    return max(matches, key=lambda name: len(roots[name].parts)) if matches else None


def identify(reader, relative, path):
    """(session id, recorded working folder) from the top of a transcript; (None, None) if malformed."""
    try:
        return reader.identity(relative, first_events(path))
    except (AttributeError, TypeError, ValueError):
        return None, None


def transcripts(base):
    return [path for path in sorted(base.rglob("*.jsonl")) if path.is_file() and not path.is_symlink()]


def sources(root, bases):
    """{(provider, wolt): [(base, path, shared root or None)]}: every transcript that counts for each wolt.

    A wolt's engine folder is its own only when it physically lies inside the wolt's
    folder. One that resolves anywhere else (a link to a shared home, say) is read
    like the shared homes: a transcript there counts only for the wolt it is
    attributed to. Unattributed or malformed transcripts are left out entirely.
    """
    roots = wolt_roots(root)
    found, own, shared = defaultdict(list), [], {}
    for wolt, folder in roots.items():
        for provider, reader in READERS.items():
            base = root / wolt / reader.ROOT
            if not base.is_dir():
                continue
            real = Path(os.path.realpath(base))
            if inside(real, folder):
                own.append(real)
                found[provider, wolt].extend((base, path, None) for path in transcripts(base))
            else:
                shared.setdefault((provider, real), base)
    for provider, base in bases.items():
        if base.is_dir():
            shared.setdefault((provider, Path(os.path.realpath(base))), base)
    records = None
    for (provider, real), base in sorted(shared.items()):
        if any(inside(real, folder) for folder in own):
            continue  # already read as that wolt's own folder (the container layout)
        reader = READERS[provider]
        for path in transcripts(base):
            session, cwd = identify(reader, path.relative_to(base), path)
            if session is None and cwd is None:
                continue
            wolt = owner_of(cwd, roots)
            if wolt is None and session is not None:
                if records is None:
                    records = session_records(root)
                wolt = records.get(str(session).lower())
            if wolt in roots:
                found[provider, wolt].append((base, path, str(real)))
    return roots, found


def distinct(provider, entries):
    """One copy of each transcript seen in several places, with the ids of all its copies.

    Claude: visit the longest relative-path copy first, then scan the remaining
    copies for counter maxima. Codex: by engine session id, keeping the longest copy. Coverage records every copy's path id, so
    switching between copies is not a lost source, while a copy that disappears
    still is (its day is retained). Path ids are what history has always recorded.
    Returns [(base, path, [chosen copy's id, other copies' ids...])].
    """
    reader, groups = READERS[provider], {}
    for base, path, shared_root in entries:
        relative = path.relative_to(base)
        key = relative
        if provider == "codex":
            session, _ = identify(reader, relative, path)
            key = ("session", str(session).lower()) if session else relative
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        groups.setdefault(key, []).append((size, base, path, path_id(base, path, shared_root)))
    chosen = []
    for copies in groups.values():
        primary = max(copies, key=lambda copy: copy[0])  # first of the longest on a tie
        _, base, path, first = primary
        chosen.append((base, path, [first, *sorted({copy[3] for copy in copies} - {first})]))
        if provider == "claude":
            for copy in copies:
                if copy is not primary:
                    _, other_base, other_path, source = copy
                    chosen.append((other_base, other_path, [source]))
    return chosen


def path_id(base, path, shared_root):
    """A transcript's coverage id. In a wolt's own folder: its relative path, as history has
    always recorded it. In a shared home: also that home, so a copy there never shares an
    id with a same-named copy in the wolt's own folder."""
    relative = path.relative_to(base).as_posix()
    text = relative if shared_root is None else f"shared:{shared_root}\n{relative}"
    return hashlib.sha256(text.encode()).hexdigest()


def scan(root, old_codex_limits, zone, bases=None):
    counts, coverage, gaps, incomplete = Counter(), defaultdict(set), Counter(), set()
    latest = old_codex_limits

    def codex_limits(at, record):
        nonlocal latest
        observed = timestamp(at)
        if not observed or not isinstance(record, dict):
            return
        if latest and observed <= timestamp(latest["observed_at"]):
            return
        windows = {}
        for key in ("primary", "secondary"):
            window = record.get(key)
            if isinstance(window, dict):
                valid = limit_window(window, "used_percent", window.get("window_minutes"))
                if valid:
                    windows[key] = valid
        if windows:
            latest = {"observed_at": observed.isoformat(), "windows": windows}

    roots, found = sources(root, host_bases() if bases is None else bases)
    for wolt in roots:
        engines = False
        for provider, reader in READERS.items():
            entries = distinct(provider, found.get((provider, wolt), []))
            if (root / wolt / reader.ROOT).is_dir() or entries:
                engines = True
                if not entries:
                    gaps[("no_transcripts", provider, wolt, None)] += 1
            seen = {} if provider == "claude" else set()
            for base, path, ids in entries:
                relative = path.relative_to(base)
                source = (provider, wolt, ids[0])

                def gap(reason, day, provider=provider, wolt=wolt, source=source):
                    gaps[(reason, provider, wolt, day)] += 1
                    if reason not in {"unknown_model", "codex_total_mismatch", "partial_final_line"}:
                        incomplete.add(source)

                usage = Usage(source, counts, coverage, gap, zone, also=ids[1:])
                try:
                    reader.read(relative, jsonl(path, gap), seen, usage, codex_limits, gap)
                except (AttributeError, TypeError, ValueError):
                    gap("invalid_event_shape", None)
            if provider == "claude":
                usage_claude.finish(seen)
        if not engines:
            gaps[("no_supported_transcripts", None, wolt, None)] += 1
    return counts, coverage, gaps, incomplete, latest


def merge(old, counts, coverage, gaps, incomplete):
    """Keep a high-water amount for each saved row and flag affected days.

    On such a retained day every row keeps the larger of its saved and fresh amount,
    so a mid-day save followed by a transcript expiry loses nothing either way.
    """
    previous = {
        (entry["day"], entry["provider"], entry["wolt"]): set(entry["sources"])
        for entry in old.get("coverage", [])
    }
    retained = {
        bucket for bucket, sources in previous.items()
        if sources - coverage.get(bucket, set()) or any(
            (*bucket[1:], src) in incomplete for src in coverage.get(bucket, ())
        )
    }
    amounts = dict(counts)
    for row in old.get("rows", []):
        key = tuple(row[name] for name in DIMENSIONS)
        if key not in counts or counts[key] < row["amount"]:
            retained.add(key[:3])
            amounts[key] = row["amount"]
    for day, provider, wolt in retained:
        bucket = day, provider, wolt
        coverage[bucket] = previous.get(bucket, set()) | coverage.get(bucket, set())
        gaps["retained_partial_day", provider, wolt, day] += 1
    return [dict(zip(DIMENSIONS, key), amount=amount) for key, amount in sorted(amounts.items())]


def dashboard(rows, gaps, limits, now, zone):
    today = now.astimezone(zone).date()
    daily = defaultdict(Counter)
    activity = defaultdict(Counter)
    for row in rows:
        day, provider, wolt, session, model, metric = (row[key] for key in DIMENSIONS)
        daily[day, provider][metric] += row["amount"]
        activity[day, provider, wolt, session, model][metric] += row["amount"]
    periods = {}
    for period, span in (("today", 1), ("last_7_days", 7), ("last_30_days", 30), ("all_time", None)):
        start = (today - timedelta(days=span - 1)).isoformat() if span else ""
        breakdowns = {key: defaultdict(Counter) for key in ("wolt", "model", "session")}
        totals = Counter()
        for row in rows:
            if row["day"] < start:
                continue
            metric, amount = row["metric"], row["amount"]
            totals[metric] += amount
            breakdowns["wolt"][row["wolt"]][metric] += amount
            breakdowns["model"][row["provider"], row["model"]][metric] += amount
            breakdowns["session"][row["provider"], row["wolt"], row["session_id"]][metric] += amount

        sessions = sorted(
            breakdowns["session"].items(),
            key=lambda entry: (-sum(entry[1].values()), entry[0]),
        )[:10]
        periods[period] = {
            "totals": dict(totals) if totals else None,
            "by_wolt": dict(sorted(breakdowns["wolt"].items())),
            "by_model": [
                {"provider": provider, "model": model, "metrics": metrics}
                for (provider, model), metrics in sorted(breakdowns["model"].items())
            ],
            "top_sessions": [
                {"provider": provider, "wolt": wolt, "session_id": session, "metrics": metrics}
                for (provider, wolt, session), metrics in sessions
            ],
        }
    return {
        "schema_version": 1,
        "timezone": zone.key,
        "metrics": list(METRICS),
        "last_collected": now.isoformat(),
        "history_starts": rows[0]["day"] if rows else None,
        "periods": periods,
        "by_day": [
            {"day": day, "provider": provider, "metrics": metrics}
            for (day, provider), metrics in sorted(daily.items())
        ],
        "activity": [
            {"day": day, "provider": provider, "wolt": wolt, "session_id": session,
             "model": model, "metrics": metrics}
            for (day, provider, wolt, session, model), metrics in sorted(activity.items())
        ],
        "limits": limits,
        "coverage_gaps": [
            {"reason": reason, "provider": provider, "wolt": wolt, "day": day, "count": count}
            for (reason, provider, wolt, day), count in sorted(
                gaps.items(), key=lambda entry: tuple(part or "" for part in entry[0])
            )
        ],
    }


def write_json(path, data, mode, *, sync=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((json.dumps(data, allow_nan=False, separators=(",", ":")) + "\n").encode())
            if sync:
                stream.flush()
                os.fsync(stream.fileno())
            stream.close()
            temporary.chmod(mode)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def reporting_zone(old, machine):
    """(zone, differs): the zone the days are counted in, and whether the machine's zone differs.

    History keeps the zone it was started in, so saved days never shift: a machine
    whose zone changed, or has no IANA name, is reported as a coverage gap
    (`reporting_zone_differs`), not obeyed. A fresh history takes the machine's zone,
    or UTC when it has none. History with days but no recorded zone is refused.
    """
    if old.get("schema_version") != HISTORY_SCHEMA:
        raise ValueError("unrecognized history schema")
    saved = old.get("timezone")
    if saved:
        zone = ZoneInfo(saved)
    elif old.get("rows"):
        raise HistoryZoneMismatch("history has days but no recorded zone")
    else:
        zone = machine or ZoneInfo("UTC")
    return zone, machine is None or machine.key != zone.key


def read_history(history):
    """Saved history under a shared hold of the save lock; never writes or creates anything."""
    try:
        lock = history.with_name(history.name + ".lock").open("rb")
    except FileNotFoundError:
        lock = None  # no save yet; the lock is created by save()
    try:
        if lock:
            fcntl.flock(lock, fcntl.LOCK_SH)  # waits while a save holds it exclusively
        try:
            return json.loads(history.read_bytes())
        except FileNotFoundError:
            return {"schema_version": HISTORY_SCHEMA}
    finally:
        if lock:
            lock.close()


def _report(root, old, claude_path, machine, now, bases):
    zone, differs = reporting_zone(old, machine)
    counts, coverage, gaps, incomplete, codex_reading = scan(root, old.get("codex_limits"), zone, bases)
    if differs:
        gaps[("reporting_zone_differs", None, None, None)] += 1
    rows = merge(old, counts, coverage, gaps, incomplete)
    claude_reading = claude_limits(claude_path)
    for provider, reading in (("claude", claude_reading), ("codex", codex_reading)):
        if reading is None:
            gaps[("plan_limits_unavailable", provider, None, None)] += 1
    report = dashboard(rows, gaps, {"claude": claude_reading, "codex": codex_reading}, now, zone)
    return report, rows, coverage, codex_reading, zone


def count(root, history=None, claude_path=None, zone=None, now=None, bases=None):
    """The dashboard from saved history plus a fresh scan, in memory. Writes nothing.
    `zone` stands in for the machine's zone (tests); history's own zone still wins."""
    root = Path(root)
    old = read_history(history or history_path(root))
    report, *_ = _report(root, old, claude_path or claude_limits_path(root), zone or machine_zone(),
                         now or datetime.now(timezone.utc), bases)
    return report


def save(root, history=None, claude_path=None, zone=None, now=None, bases=None):
    """Merge a fresh scan into history under the exclusive lock and replace it atomically."""
    root = Path(root)
    history = history or history_path(root)
    history.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = history.with_name(history.name + ".lock")
    with os.fdopen(os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600), "rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        os.fchmod(lock.fileno(), 0o600)  # also an older or restored lock
        old = json.loads(history.read_bytes()) if history.exists() else {"schema_version": HISTORY_SCHEMA}
        report, rows, coverage, codex_reading, zone = _report(
            root, old, claude_path or claude_limits_path(root), zone or machine_zone(),
            now or datetime.now(timezone.utc), bases)
        write_json(history, {
            "schema_version": HISTORY_SCHEMA,
            "timezone": zone.key,
            "rows": rows,
            "coverage": [
                {"day": day, "provider": provider, "wolt": wolt, "sources": sorted(sources)}
                for (day, provider, wolt), sources in sorted(coverage.items())
            ],
            "codex_limits": codex_reading,
        }, 0o600)
        return report
