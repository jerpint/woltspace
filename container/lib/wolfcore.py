"""
🐺 Wolf core — the cron logic the scheduler and the lodge API share.

Pure, stdlib-only, and the one place any of this is decided:

  - parsing a cron expression (strictly) and matching it against a time
  - when a cron runs next, and whether an entry is due right now
  - reading and writing a wolt's `wolt/wolf.json` under a lock
  - the lodge-global last-run stamps and job journal in `.space/wolf/`

`container/creatures/wolf.py` is the loop that fires; `server/app.py` is the
API that edits. Both import this, so a schedule the API accepts is exactly a
schedule the wolf fires, and neither can drift from the other.

Time is the lodge machine's LOCAL time, everywhere: `datetime.now().astimezone()`.
A cron written `0 9 * * *` fires at 09:00 on the clock of the machine the lodge
runs on. There is no per-cron timezone.

Cron semantics, deliberately:
  - five fields: minute hour day-of-month month day-of-week (0 or 7 = Sunday)
  - `*`, lists `1,3,5`, ranges `1-5`, steps `*/15`, `5/10`, `1-10/2`
  - day-of-month AND day-of-week must BOTH match. Classic cron ORs them when
    both are restricted; the wolf always ANDed them, and schedules written
    against that keep meaning what they meant. `0 9 13 * 5` is "Friday the
    13th", not "every 13th plus every Friday".
"""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterator, Optional

from paths import space_wolf_dir

try:  # POSIX (macOS, Linux, the container). Elsewhere the lock is a no-op.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

# A cron name and a wolt name both become path components under the state dir,
# and both come from files a wolt can write. Anything else is never joined.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
# What the scheduler writes after a fire: the matched minute, local wall time.
STAMP_FORMAT = "%Y-%m-%d-%H:%M"
# How far back a restarted wolf looks for the most recent missed match.
CATCH_UP_WINDOW = timedelta(hours=24)
# A schedule that cannot fire within this horizon is refused on write.
NEXT_RUN_HORIZON_DAYS = 366

WOLF_JSON = "wolf.json"
JOURNAL = "jobs.jsonl"


class CronError(ValueError):
    """A cron entry that cannot be accepted. `field` names the culprit."""

    def __init__(self, message: str, field: str = ""):
        super().__init__(message)
        self.field = field


# ── Cron expressions ───────────────────────────────────────────────

# (label, lowest, highest) per field, in order.
_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


def _int(text: str, label: str, lo: int, hi: int) -> int:
    if not text.isdigit():
        raise CronError(f"{label}: '{text}' is not a number")
    value = int(text)
    if not lo <= value <= hi:
        raise CronError(f"{label}: {value} is out of range {lo}-{hi}")
    return value


def _parse_field(text: str, label: str, lo: int, hi: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{label}: empty list item in '{text}'")
        base, slash, step_text = part.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{label}: step '{step_text}' must be a positive number")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _int(first, label, lo, hi), _int(last, label, lo, hi)
            if start > end:
                raise CronError(f"{label}: range {start}-{end} runs backwards")
        else:
            start = _int(base, label, lo, hi)
            # `5/10` means from 5 to the top, every 10; a bare `5` is just 5.
            end = hi if slash else start
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    """A parsed five-field expression. Days of week use cron numbering, 0=Sunday."""

    expr: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]

    def matches(self, dt: datetime) -> bool:
        """Wall-clock match. Day-of-month and day-of-week are ANDed (see top)."""
        return (
            dt.minute in self.minutes
            and dt.hour in self.hours
            and self._day_matches(dt.date())
        )

    def _day_matches(self, day: date) -> bool:
        return (
            day.day in self.days
            and day.month in self.months
            and (day.weekday() + 1) % 7 in self.weekdays  # python 0=Mon, cron 0=Sun
        )


def parse_cron(expr: str) -> Cron:
    """Parse strictly: exactly five fields, every value in range, or CronError."""
    if not isinstance(expr, str):
        raise CronError("schedule must be a string", "schedule")
    parts = expr.split()
    if len(parts) != 5:
        raise CronError(
            f"schedule needs 5 fields (minute hour day month weekday), got {len(parts)}",
            "schedule",
        )
    try:
        fields = [_parse_field(p, *spec) for p, spec in zip(parts, _FIELDS)]
    except CronError as exc:
        raise CronError(str(exc), "schedule") from None
    minutes, hours, days, months, weekdays = fields
    weekdays = frozenset(d % 7 for d in weekdays)  # 7 is Sunday too
    return Cron(" ".join(parts), minutes, hours, days, months, weekdays)


def cron_matches(expr: str, dt: datetime) -> bool:
    """Does `expr` match `dt`? An unparseable expression matches nothing."""
    try:
        return parse_cron(expr).matches(dt)
    except CronError:
        return False


# ── Local time ─────────────────────────────────────────────────────

def now_local() -> datetime:
    """The lodge's current time, aware, in the machine's local zone."""
    return datetime.now().astimezone()


def _localize(naive: datetime) -> Optional[datetime]:
    """A local wall time as an aware datetime, or None if the clock skips it.

    On a spring-forward day 02:30 never happens, so a cron at 02:30 does not
    fire that day — the scheduler never sees that minute either. On fall-back
    the first of the two 01:30s is the one that counts; the second has the same
    stamp and is skipped as already fired.
    """
    stamp = naive.timestamp()
    back = datetime.fromtimestamp(stamp)
    if back.replace(fold=0) != naive.replace(fold=0):
        return None
    return back.astimezone()


def _minute(dt: datetime) -> datetime:
    return dt.replace(second=0, microsecond=0)


def local_tz_name() -> str:
    """The lodge's zone as an IANA name when it can be found, else its abbreviation."""
    tz = (os.environ.get("TZ") or "").lstrip(":")
    if "/" in tz and not tz.startswith("/"):
        return tz
    for link in ("/etc/localtime",):
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    try:
        name = Path("/etc/timezone").read_text().strip()
        if name:
            return name
    except OSError:
        pass
    return now_local().tzname() or "local"


def stamp_to_iso(stamp: Optional[str]) -> Optional[str]:
    """`YYYY-MM-DD-HH:MM` (local) as ISO 8601 with offset; None when unreadable."""
    if not stamp:
        return None
    try:
        naive = datetime.strptime(stamp.strip(), STAMP_FORMAT)
    except ValueError:
        return None
    aware = _localize(naive) or naive.astimezone()
    return aware.isoformat()


# ── When ───────────────────────────────────────────────────────────

def next_run(expr: "str | Cron", now: datetime,
             horizon_days: int = NEXT_RUN_HORIZON_DAYS) -> Optional[datetime]:
    """The first local minute strictly after `now` that the schedule matches.

    Walks days, then hours, then minutes — never a minute-by-minute scan of a
    year. None when nothing matches within the horizon (`0 0 31 2 *`).
    """
    cron = expr if isinstance(expr, Cron) else parse_cron(expr)
    after = _minute(now.astimezone()).replace(tzinfo=None)
    hours, minutes = sorted(cron.hours), sorted(cron.minutes)
    for offset in range(horizon_days + 1):
        day = after.date() + timedelta(days=offset)
        if not cron._day_matches(day):
            continue
        for hour in hours:
            for minute in minutes:
                naive = datetime(day.year, day.month, day.day, hour, minute)
                if naive <= after:
                    continue
                aware = _localize(naive)
                if aware is not None:
                    return aware
    return None


def parse_at(value: str) -> datetime:
    """A one-off's `at` as an aware time. Naive means lodge-local."""
    if not isinstance(value, str) or not value.strip():
        raise CronError("at must be a timestamp like 2026-03-22T14:30", "at")
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        raise CronError(f"at '{value}' is not a timestamp like 2026-03-22T14:30", "at") from None
    return when.astimezone() if when.tzinfo is None else when


def due_slot(entry: dict, now: datetime, last_stamp: Optional[str],
             lookback: timedelta = timedelta(0)) -> Optional[datetime]:
    """THE due check. The minute to fire `entry` for, or None if not due.

    One rule for the running loop and the start-up catch-up alike; they differ
    only in how far back they look. The loop looks at the current minute only
    (`lookback` 0); catch-up looks back `CATCH_UP_WINDOW` for the most recent
    match. A recurring cron is due when that minute is not the one it last
    fired for. A one-off is due once its `at` has passed — it is removed from
    `wolf.json` after firing, which is what stops it firing again.

    Raises CronError for an entry that cannot be read; callers skip it.
    """
    if entry.get("at"):
        return now if now >= parse_at(entry["at"]) else None
    schedule = entry.get("schedule")
    if not schedule:
        raise CronError("entry has neither schedule nor at", "schedule")
    cron = parse_cron(schedule)
    # Step back in real (UTC) minutes and read each one on the local clock, so
    # a window spanning a DST change still sees every wall minute once.
    start = _minute(now).timestamp()
    for i in range(max(1, int(lookback.total_seconds() // 60))):
        slot = datetime.fromtimestamp(start - 60 * i).astimezone()
        if cron.matches(slot):
            return None if slot.strftime(STAMP_FORMAT) == last_stamp else slot
    return None


# ── Entries ────────────────────────────────────────────────────────

def validate_name(name: str, field: str = "name") -> str:
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise CronError(f"{field} must be letters, digits, '-' or '_' only", field)
    return name


def slugify(prompt: str, words: int = 4) -> str:
    """A cron name from its prompt: the first few words, lowercase, dashed."""
    parts = re.findall(r"[a-z0-9]+", (prompt or "").lower())[:words]
    return "-".join(parts)[:48].strip("-") or "cron"


def unique_name(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def validate_entry(entry: dict, now: datetime) -> dict:
    """Strict check of a cron about to be written. Returns it, or raises CronError.

    Only what the API and CLI write goes through this. The scheduler reads
    hand-edited files and stays tolerant: an entry this would refuse is logged
    and skipped there, never fatal.
    """
    validate_name(entry.get("name", ""))
    has_schedule, has_at = bool(entry.get("schedule")), bool(entry.get("at"))
    if has_schedule == has_at:
        raise CronError("give exactly one of schedule (recurring) or at (one-off)", "schedule")
    if has_schedule:
        cron = parse_cron(entry["schedule"])
        if next_run(cron, now) is None:
            raise CronError(f"schedule '{entry['schedule']}' never fires within a year", "schedule")
    else:
        if parse_at(entry["at"]) <= now:
            raise CronError(f"at '{entry['at']}' is not in the future (lodge time)", "at")
    prompt = entry.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise CronError("prompt must not be empty", "prompt")
    notify = entry.get("notify")
    if notify is not None and not isinstance(notify, str):
        raise CronError("notify must be text", "notify")
    if "catch_up" in entry and not isinstance(entry["catch_up"], bool):
        raise CronError("catch_up must be true or false", "catch_up")
    return entry


# ── wolf.json ──────────────────────────────────────────────────────

def wolf_json_path(wolts_dir: Path, wolt: str) -> Path:
    return Path(wolts_dir) / wolt / "wolt" / WOLF_JSON


def dump_wolf_json(data: dict) -> str:
    """The on-disk format: two-space indent, trailing newline."""
    return json.dumps(data, indent=2) + "\n"


def read_wolf_json(path: Path) -> dict:
    """The file as a dict with a `crons` list. Missing file = no crons."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {"crons": []}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    crons = data.setdefault("crons", [])
    if not isinstance(crons, list):
        raise ValueError(f"{path}: crons is not a list")
    return data


def atomic_write(path: Path, text: str) -> None:
    """Write beside the target, then rename over it: never a half-written file.

    The replacement keeps the permissions the file already had: a cron's words
    are the user's own, and a wolf.json someone locked down stays locked down.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    except FileNotFoundError:
        pass  # a new file keeps the umask default
    os.replace(tmp, path)


@contextmanager
def wolf_json_lock(wolts_dir: Path, wolt: str) -> Iterator[None]:
    """Exclusive lock for one wolt's wolf.json, shared by the wolf and the API.

    The lock is a sidecar in the lodge-global state dir, not the file itself:
    the file is replaced by rename on every write, so a lock on it would be a
    lock on an inode nobody reads any more.
    """
    validate_name(wolt, "wolt")
    lock_dir = wolt_state_dir(space_wolf_dir(Path(wolts_dir)), wolt)
    lock_dir.mkdir(parents=True, exist_ok=True)
    with open(lock_dir / "wolf.json.lock", "a") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remove_entry(wolts_dir: Path, wolt: str, name: str, at: str | None = None) -> bool:
    """Drop the named cron from a wolt's wolf.json. True if one was removed.

    `at` narrows it to the one-off that actually fired: if the entry was
    rescheduled while it was firing, the new time no longer matches and the
    entry stays, to fire when it is next due.
    """
    path = wolf_json_path(wolts_dir, wolt)

    def fired(c) -> bool:
        return (isinstance(c, dict) and c.get("name") == name
                and (at is None or c.get("at") == at))

    with wolf_json_lock(wolts_dir, wolt):
        data = read_wolf_json(path)
        kept = [c for c in data["crons"] if not fired(c)]
        if len(kept) == len(data["crons"]):
            return False
        data["crons"] = kept
        atomic_write(path, dump_wolf_json(data))
        return True


def load_all(wolts_dir: Path) -> tuple[list[dict], list[tuple[str, str]]]:
    """Every wolt's crons, each tagged `_owner`/`_owner_dir`, plus read errors."""
    crons, errors = [], []
    for path in sorted(Path(wolts_dir).glob(f"*/wolt/{WOLF_JSON}")):
        wolt_dir = path.parent.parent
        try:
            data = read_wolf_json(path)
        except (ValueError, OSError) as exc:
            errors.append((wolt_dir.name, str(exc)))
            continue
        for cron in data["crons"]:
            if isinstance(cron, dict):
                crons.append({**cron, "_owner": wolt_dir.name, "_owner_dir": str(wolt_dir)})
    return crons, errors


# ── State: stamps and journal ──────────────────────────────────────

def wolt_state_dir(state_dir: Path, wolt: str) -> Path:
    return Path(state_dir) / wolt


def _inside(state_dir: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(state_dir).resolve())
        return True
    except ValueError:
        return False


def stamp_path(state_dir: Path, wolt: str, name: str) -> Optional[Path]:
    """`<state>/<wolt>/<name>.last`, or None for a wolt or name that is not a
    plain identifier. Keyed by wolt AND name: two wolts may both have a
    `digest` without either one's stamp silencing the other."""
    if not (NAME_RE.match(wolt or "") and NAME_RE.match(name or "")):
        return None
    path = Path(state_dir) / wolt / f"{name}.last"
    return path if _inside(state_dir, path) else None


def read_last_run(state_dir: Path, wolt: str, name: str) -> Optional[str]:
    """The stamp of the last fire, or None.

    Falls back to a pre-migration lodge-wide `<name>.last`, so a lodge read
    before the wolf has started (and migrated) still shows the truth.
    """
    path = stamp_path(state_dir, wolt, name)
    if path is None:
        return None
    for candidate in (path, Path(state_dir) / f"{name}.last"):
        try:
            return candidate.read_text().strip() or None
        except OSError:
            continue
    return None


def write_last_run(state_dir: Path, wolt: str, name: str, dt: datetime) -> None:
    path = stamp_path(state_dir, wolt, name)
    if path is None:
        raise CronError(f"cannot stamp {wolt!r}/{name!r}: not a plain name", "name")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, dt.strftime(STAMP_FORMAT))


def move_last_run(state_dir: Path, wolt: str, name: str, new_wolt: str, new_name: str) -> None:
    """Carry a cron's stamp along when it is renamed or moved to another wolt."""
    src = stamp_path(state_dir, wolt, name)
    dst = stamp_path(state_dir, new_wolt, new_name)
    if src is None or dst is None or src == dst or not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(src, dst)


def migrate_legacy_stamps(state_dir: Path, owners: dict[str, list[str]]) -> list[str]:
    """One-time move of lodge-wide `<name>.last` stamps to `<wolt>/<name>.last`.

    `owners` maps a cron name to every wolt that has a cron by that name. One
    owner: the stamp moves to it. Several: each gets a copy, so none of them
    fires again for a minute it already fired for. No owner: left alone. An
    existing per-wolt stamp is never overwritten. Idempotent — a migrated
    stamp is gone from the top level, so a second run finds nothing to do.
    """
    moved = []
    state_dir = Path(state_dir)
    if not state_dir.is_dir():
        return moved
    for legacy in sorted(state_dir.glob("*.last")):
        name = legacy.stem
        wolts = [w for w in owners.get(name, []) if stamp_path(state_dir, w, name)]
        if not wolts or not legacy.is_file():
            continue
        stamp = legacy.read_text()
        for wolt in wolts:
            target = stamp_path(state_dir, wolt, name)
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, stamp)
            moved.append(f"{wolt}/{name}")
        legacy.unlink()
    return moved


def append_journal(state_dir: Path, cron: str, action: str, **fields) -> None:
    """One line in `jobs.jsonl`. Fields are only ever added, never renamed:
    `ts`, `cron`, `action`, then `event`, `owner`, and whatever else."""
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": datetime.now().isoformat(), "cron": cron, "action": action, **fields}
    with open(state_dir / JOURNAL, "a") as handle:
        handle.write(json.dumps(entry) + "\n")


# ── Presenting ─────────────────────────────────────────────────────

def describe(entry: dict, wolt: str, state_dir: Path, now: datetime) -> dict:
    """One cron as the API shows it: what it runs, when next, when last."""
    name = entry.get("name", "")
    out = {"wolt": wolt, "name": name}
    kind = "at" if entry.get("at") else "schedule"
    out[kind] = entry.get(kind, "")
    out.update({
        "prompt": entry.get("prompt", ""),
        "notify": entry.get("notify"),
        "catch_up": entry.get("catch_up") is not False,
        "next_run": None,
        "last_run": stamp_to_iso(read_last_run(state_dir, wolt, name)),
    })
    try:
        when = parse_at(entry["at"]) if kind == "at" else next_run(entry.get("schedule", ""), now)
        out["next_run"] = when.isoformat() if when else None
    except CronError as exc:
        out["error"] = str(exc)
    return out
