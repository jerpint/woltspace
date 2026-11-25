"""Projection for the usage dashboard's two independently filtered scopes."""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import re
from zoneinfo import ZoneInfo

from usage_collector import METRICS

DATED_GAPS = {"retained_partial_day", "invalid_token_counts", "missing_message_id"}
NON_COVERAGE = {"plan_limits_unavailable", "unknown_model", "codex_total_mismatch",
                "partial_final_line", "reporting_zone_differs"}


def filters(params):
    if "view" not in params:
        return None
    keys = {"view", "period", "provider", "calendar_wolt", "metric"}
    items = params.multi_items()
    if (len(items) != len(params) or set(params) - keys or params.get("view") != "compact"):
        raise ValueError("invalid_view")
    span = params.get("period", "30")
    provider = params.get("provider", "all")
    wolt = params.get("calendar_wolt", "")
    metric = params.get("metric", "output")
    if (span not in {"0", "7", "30", "90"} or provider not in {"all", "claude", "codex"}
            or metric not in METRICS or (wolt and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", wolt))):
        raise ValueError("invalid_view")
    return int(span), provider, wolt, metric


def project(data, options):
    span, provider, wolt, metric = options
    today = datetime.fromisoformat(data["last_collected"]).astimezone(ZoneInfo(data["timezone"])).date()
    try:
        year_ago = today.replace(year=today.year - 1)
    except ValueError:  # February 29, as in the browser's calendar
        year_ago = date(today.year - 1, 3, 1)
    calendar_start = (year_ago + timedelta(days=1)).isoformat()
    period_start = (today - timedelta(days=span - 1)).isoformat() if span else ""
    through = today.isoformat()
    calendar = defaultdict(Counter)
    calendar_sessions = defaultdict(set)
    daily = defaultdict(Counter)
    totals = Counter()
    sessions = set()
    groups = {key: defaultdict(Counter) for key in ("wolt", "model")}
    group_sessions = {key: defaultdict(set) for key in groups}
    ranked = []
    names = set()
    for row in data["activity"]:
        day, source, owner = row["day"], row["provider"], row["wolt"]
        names.add(owner)
        if provider != "all" and provider != source:
            continue
        identity = source, owner, row["session_id"]
        metrics = row["metrics"]
        if calendar_start <= day <= through and (not wolt or wolt == owner):
            calendar[day].update(metrics)
            calendar_sessions[day].add(identity)
        if not period_start <= day <= through:
            continue
        daily[day, source].update(metrics)
        totals.update(metrics)
        sessions.add(identity)
        for key in groups:
            groups[key][row[key]].update(metrics)
            group_sessions[key][row[key]].add(identity)
        ranked.append(row)

    gaps = [gap for gap in data["coverage_gaps"] if provider == "all" or
            gap["provider"] in (provider, None)]
    calendar_gaps = [gap for gap in gaps if not wolt or gap["wolt"] in (wolt, None)]
    return {
        "schema_version": 1, "view": "compact",
        **{key: data[key] for key in ("timezone", "last_collected", "history_starts", "limits")},
        "wolts": sorted(names),
        "calendar": {
            "through": through, "history_starts": data["history_starts"],
            "days": [{"day": day, "output": counts["output"], "sessions": len(calendar_sessions[day])}
                     for day, counts in sorted(calendar.items())],
            "coverage_gaps": [gap for gap in calendar_gaps if gap["reason"] in DATED_GAPS
                              and gap["day"] and calendar_start <= gap["day"] <= through],
            "undated_gaps": [gap for gap in calendar_gaps if not gap["day"]
                             and gap["reason"] not in NON_COVERAGE],
        },
        "coverage_gaps": [gap for gap in gaps if not gap["day"] or period_start <= gap["day"] <= through],
        "totals": dict(totals) if sessions else None, "sessions": len(sessions),
        "by_day": [{"day": day, "provider": source, "metrics": counts}
                   for (day, source), counts in sorted(daily.items())],
        **{f"by_{key}": [{"name": name, "metrics": counts, "sessions": len(group_sessions[key][name])}
                          for name, counts in sorted(values.items())] for key, values in groups.items()},
        "top": sorted(ranked, key=lambda row: -row["metrics"].get(metric, 0))[:5],
    }
