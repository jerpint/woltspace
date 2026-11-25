"""Usage queries: selection, grouping, validation and metadata across output formats."""

import csv
import io
import json

import pytest

import usage_support  # noqa: F401  (puts container/lib on the path)
from usage_collector import METRICS
import usage_query as query


@pytest.fixture
def data():
    def row(day, wolt, provider, model, session, output):
        return {
            "day": day, "wolt": wolt, "provider": provider, "model": model,
            "session_id": session, "metrics": dict.fromkeys(METRICS, 1) | {"output": output},
        }

    return {
        "timezone": "America/New_York",
        "last_collected": "2026-11-24T04:59:59+00:00",
        "history_starts": "2026-11-16",
        "metrics": list(METRICS),
        "activity": [
            row("2026-11-16", "alpha-wolt", "claude", "a", "s1", 2),
            row("2026-11-17", "alpha-wolt", "claude", "a", "s1", 3),
            row("2026-11-23", "beta-wolt", "codex", "b", "s2", 5),
        ],
        "coverage_gaps": [
            {"reason": "invalid_jsonl", "provider": "claude", "wolt": "alpha-wolt", "day": None, "count": 1},
            {"reason": "codex_total_mismatch", "provider": "codex", "wolt": "beta-wolt",
             "day": "2026-11-23", "count": 1},
        ],
        "limits": {"claude": None, "codex": {"observed_at": "2026-11-23T15:00:00Z",
                    "windows": {"primary": {"used_percent": 10, "resets_at": 123, "window_minutes": 10080}}}},
    }


@pytest.mark.parametrize("by, expected", [
    ("day", {"2026-11-16": 2, "2026-11-17": 3, "2026-11-23": 5}),
    ("wolt", {"alpha-wolt": 5, "beta-wolt": 5}),
    ("session", {"s1": 5, "s2": 5}),
    ("provider", {"claude": 5, "codex": 5}),
    ("model", {"a": 5, "b": 5}),
])
def test_each_grouping(data, by, expected):
    out = query.execute(data, query.http_options(f"by={by}"))
    assert {row[by]: row["output"] for row in out["rows"]} == expected
    assert all(row["sessions"] == 1 for row in out["rows"])


@pytest.mark.parametrize("options, output, gap_count", [
    ("since=7d", 8, 2),
    ("since=2026-11-17&until=2026-11-17", 3, 1),
    ("until=2026-11-16", 2, 1),
    ("wolt=alpha", 5, 1),
    ("provider=codex", 5, 1),
    ("model=a", 5, 2),
])
def test_each_filter(data, options, output, gap_count):
    out = query.execute(data, query.http_options(options))
    assert out["totals"]["output"] == output
    assert len(out["coverage_gaps"]) == gap_count


def test_multi_group_selected_metric_and_top(data):
    request = query.http_options("by=provider,model&metric=cache_read&top=1")
    assert query.execute(data, request)["rows"] == [{"provider": "claude", "model": "a", "cache_read": 2}]


@pytest.mark.parametrize("raw", [
    "by=bogus", "by=wolt,wolt", "by=", "by=wolt&by=model", "since=0d", "since=2026-02-30",
    "until=7d", "since=2026-02-05&until=2026-02-04", "provider=other", "model=%FF",
    "wolt=..%2Falpha", "metric=cost", "format=yaml", "by=wolt&top=-1", "by=wolt&top=10001",
    "command=extra", "unknown=hello", "command=summary&by=model", "by=%zz",
])
def test_invalid_query_values(raw):
    with pytest.raises(query.BadQuery):
        query.http_options(raw)


@pytest.mark.parametrize("param", ["wolt=missing", "model=missing", "since=7d&until=2026-11-16"])
def test_data_dependent_validation(data, param):
    with pytest.raises(query.BadQuery):
        query.execute(data, query.http_options(param))


def test_outputs_start_with_freshness_and_keep_limits_and_coverage(data):
    result = query.execute(data, query.http_options("command=summary"))
    encoded, content_type = query.format_result(result, "json")
    assert list(json.loads(encoded))[:4] == ["timezone", "last_collected", "history_starts", "coverage_gaps"]
    assert content_type == "application/json"
    rendered, _ = query.format_result(result, "table")
    assert [line.split()[0] for line in rendered.splitlines()[:3]] == ["last_collected", "history_starts", "timezone"]
    assert "invalid_jsonl" in rendered and "unavailable" in rendered
    encoded, _ = query.format_result(result, "csv")
    records = csv.DictReader(io.StringIO(encoded))
    assert records.fieldnames[:4] == ["last_collected", "history_starts", "timezone", "record"]
    assert [row["record"] for row in records] == [
        "metadata", "totals", "limit", "limit", "coverage_gap", "coverage_gap",
    ]


def test_limits_and_empty_results(data):
    result = query.execute(data, query.http_options("command=limits&provider=codex"))
    assert list(result["limits"]) == ["codex"] and "totals" not in result
    assert result["coverage_gaps"][0]["provider"] == "codex"
    assert query.execute(data, query.http_options("since=2026-11-24"))["totals"] is None


def test_text_and_csv_show_reset_times_in_the_lodge_zone(data):
    result = query.execute(data, query.http_options("command=limits"))
    rendered, _ = query.format_result(result, "table")
    assert "1969-12-31T19:02-05:00" in rendered  # resets_at 123 (Unix seconds), New York time
    assert json.loads(query.format_result(result, "json")[0])["limits"]["codex"]["windows"]["primary"][
        "resets_at"] == 123


def test_csv_cells_that_look_like_formulas_are_quoted(data):
    data["activity"][0]["wolt"] = "=cmd-wolt"
    encoded, _ = query.format_result(query.execute(data, query.http_options("by=wolt&format=csv")), "csv")
    wolts = [row["wolt"] for row in csv.DictReader(io.StringIO(encoded)) if row["record"] == "usage"]
    assert "'=cmd-wolt" in wolts and "=cmd-wolt" not in wolts


@pytest.mark.parametrize("fmt", ["table", "csv"])
def test_limits_format_ignores_an_unrenderable_saved_reset(data, fmt):
    window = data["limits"]["codex"]["windows"]["primary"]
    window["resets_at"] = 253402300800
    result = query.execute(data, query.http_options(f"command=limits&format={fmt}"))
    body, _ = query.format_result(result, fmt)
    assert "253402300800" not in body
    assert "primary" in body
