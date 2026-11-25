"""Usage queries over a count: validation, selection and JSON/CSV/table output.

`GET /api/usage` validates its query string here before any count starts, then
selects from the fresh dashboard data. `woltspace usage` is a thin client of it.
"""

from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import io
import json
import re
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

from usage_collector import MAX_RESET, METRICS, MIN_RESET, READERS

DIMENSIONS = ("day", "wolt", "session", "provider", "model")
COMMANDS = ("summary", "table", "limits")
FORMATS = ("table", "json", "csv")
PARAMETERS = ("by", "since", "until", "wolt", "provider", "model", "metric", "top", "format")
IDENTIFIERS = {
    "wolt": r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}",
    "model": r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,100}",
}


class BadQuery(ValueError):
    """An invalid user parameter. Messages are safe to return to clients."""


@dataclass(frozen=True)
class Request:
    command: str
    by: tuple[str, ...]
    since: date | int | None
    until: date | None
    top: int | None
    output_format: str
    filters: dict[str, str]


def parse_date(value, name):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise BadQuery(f"invalid {name}")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise BadQuery(f"invalid {name}") from None


def validate(command, options, default_format):
    if command not in COMMANDS:
        raise BadQuery("invalid command")
    params = dict(options)
    for key, choices in (("provider", READERS), ("metric", (*METRICS, "sessions")), ("format", FORMATS)):
        if key in params and params[key] not in choices:
            raise BadQuery(f"invalid {key}")
    for key, pattern in IDENTIFIERS.items():
        if key in params and not re.fullmatch(pattern, params[key]):
            raise BadQuery(f"invalid {key}")
    by = tuple(params.pop("by").split(",")) if "by" in params else (("wolt",) if command == "table" else ())
    if (command != "table" and by) or len(set(by)) != len(by) or any(key not in DIMENSIONS for key in by):
        raise BadQuery("invalid by")
    since = params.pop("since", None)
    if since is not None:
        if re.fullmatch(r"[1-9][0-9]{0,4}d", since):
            since = int(since[:-1])
        else:
            since = parse_date(since, "since")
    until = parse_date(params.pop("until"), "until") if "until" in params else None
    if isinstance(since, date) and until is not None and since > until:
        raise BadQuery("since is after until")
    top = params.pop("top", None)
    if top is not None:
        if command != "table" or not re.fullmatch(r"[1-9][0-9]{0,3}|10000", top):
            raise BadQuery("invalid top")
        top = int(top)
    output_format = params.pop("format", default_format)
    return Request(command, by, since, until, top, output_format, params)


def http_options(raw):
    if len(raw) > 2048 or re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise BadQuery("invalid query string")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=len(PARAMETERS) + 1)
    except ValueError:
        raise BadQuery("invalid query string") from None
    options = dict(pairs)
    if len(options) != len(pairs):
        raise BadQuery("repeated parameter")
    if options.keys() - {*PARAMETERS, "command"}:
        raise BadQuery("unknown parameter")
    command = options.pop("command", "table" if "by" in options else "summary")
    return validate(command, options, "json")


def resolve_wolt(data, value):
    names = {row["wolt"] for row in data["activity"]}
    names.update(gap["wolt"] for gap in data["coverage_gaps"] if gap.get("wolt"))
    if value in names:
        return value
    matches = [name for name in names if name.startswith(value + "-")]
    if len(matches) == 1:
        return matches[0]
    raise BadQuery("unknown or ambiguous wolt")


def execute(data, request):
    command, by, since, until = request.command, request.by, request.since, request.until
    filters = dict(request.filters)
    metric = filters.pop("metric", None)
    if "wolt" in filters:
        filters["wolt"] = resolve_wolt(data, filters["wolt"])
    if "model" in filters and not any(row["model"] == filters["model"] for row in data["activity"]):
        raise BadQuery("unknown model")

    if isinstance(since, int):
        today = datetime.fromisoformat(data["last_collected"]).astimezone(ZoneInfo(data["timezone"])).date()
        since = today - timedelta(days=min(since - 1, today.toordinal() - 1))
    if since is not None and until is not None and since > until:
        raise BadQuery("since is after until")
    lower, upper = since.isoformat() if since else "", until.isoformat() if until else "9999-12-31"

    coverage = [
        gap for gap in data["coverage_gaps"]
        if (not gap.get("day") or lower <= gap["day"] <= upper)
        and all(gap.get(key) in (value, None) for key, value in filters.items())
    ]
    result = {
        "timezone": data["timezone"],
        "last_collected": data["last_collected"],
        "history_starts": data["history_starts"],
        "coverage_gaps": coverage,
    }
    if command in ("summary", "limits"):
        result["limits"] = {
            key: reading for key, reading in data["limits"].items()
            if key == filters.get("provider", key)
        }
    if command == "limits":
        return result

    columns = (metric,) if metric else (*data["metrics"], "sessions")
    totals = defaultdict(Counter)
    sessions = defaultdict(set)
    for row in data["activity"]:
        if not lower <= row["day"] <= upper or any(row[key] != value for key, value in filters.items()):
            continue
        key = tuple(row["session_id"] if dim == "session" else row[dim] for dim in by)
        totals[key].update(row["metrics"])
        sessions[key].add((row["provider"], row["wolt"], row["session_id"]))

    def amounts(key):
        return {
            field: len(sessions[key]) if field == "sessions" else totals[key][field]
            for field in columns
        }

    if command == "summary":
        result["totals"] = amounts(()) if totals else None
    else:
        rank = metric or "output"
        keys = sorted(
            totals,
            key=lambda key: (-(len(sessions[key]) if rank == "sessions" else totals[key][rank]), key),
        )
        result["by"] = list(by)
        result["rows"] = [dict(zip(by, key), **amounts(key)) for key in keys[:request.top]]
    return result


def readable(values, zone):
    """A limit window for text and CSV: Unix-second reset times as ISO times in the lodge zone."""
    reset = values.get("resets_at")
    if type(reset) is int:
        rendered = (datetime.fromtimestamp(reset, zone).isoformat(timespec="minutes")
                    if MIN_RESET <= reset < MAX_RESET else None)
        values = dict(values, resets_at=rendered)
    return values


def blocks(result):
    """Records used by CSV and plain text, with metadata ahead of usage."""
    zone = ZoneInfo(result["timezone"])
    yield "metadata", [{key: result[key] for key in ("last_collected", "history_starts", "timezone")}]
    if "totals" in result:
        yield "totals", [result["totals"]] if result["totals"] is not None else []
    if "rows" in result:
        yield "usage", result["rows"]
    if "limits" in result:
        yield "limit", [
            {"provider": provider, "window": window, "observed_at": reading["observed_at"] if reading else None,
             **readable(values, zone)}
            for provider, reading in result["limits"].items()
            for window, values in (reading["windows"] if reading else {"unavailable": {}}).items()
        ]
    yield "coverage_gap", result["coverage_gaps"]


def spreadsheet_safe(value):
    """A text cell a spreadsheet would read as a formula gets a leading apostrophe."""
    return "'" + value if isinstance(value, str) and value[:1] in ("=", "+", "-", "@") else value


def format_result(result, output_format):
    if output_format == "json":
        return json.dumps(result, allow_nan=False) + "\n", "application/json"
    sections = list(blocks(result))
    if output_format == "csv":
        keys = list(dict.fromkeys(key for _, rows in sections for row in rows for key in row))
        stream = io.StringIO()
        writer = csv.DictWriter(stream, [*keys[:3], "record", *keys[3:]])
        writer.writeheader()
        for label, rows in sections:
            writer.writerows({key: spreadsheet_safe(value) for key, value in dict(row, record=label).items()}
                             for row in rows)
        return stream.getvalue(), "text/csv; charset=utf-8"

    lines = [f"{key}  {value if value is not None else 'unavailable'}"
             for key, value in sections[0][1][0].items()]
    for label, rows in sections[1:]:
        lines.extend(("", label))
        if not rows:
            lines.append("unavailable" if label != "coverage_gap" else "none reported")
            continue
        keys = list(dict.fromkeys(key for row in rows for key in row))
        cells = [[str(row[key]) if row.get(key) is not None else "—" for key in keys] for row in rows]
        widths = [max(len(key), *(len(row[i]) for row in cells)) for i, key in enumerate(keys)]
        for row in [keys, *cells]:
            lines.append("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
    return "\n".join(lines) + "\n", "text/plain; charset=utf-8"
