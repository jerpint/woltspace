"""The usage collector: counting, history and native attribution, on neutral fixtures."""

from datetime import datetime, timezone
import json
import os
import stat
from zoneinfo import ZoneInfo

import pytest

from usage_support import (
    SID, ZONE, claude, codex, count, emit, host, request, save, token, wolt,
)
import usage_collector


# ── Counting (carried over from the reviewed collector) ──

def test_claude_dedupes_under_parent_and_counts_once_per_wolt(tmp_path):
    main = emit(claude(tmp_path), request("one"))
    emit(main.with_suffix("") / "subagents/agent.jsonl", request("one"), request("two"))
    emit(claude(tmp_path, name="beta"), request("one"))
    report = save(tmp_path)
    all_time = report["periods"]["all_time"]
    assert all_time["totals"] == {"input_fresh": 15, "cache_read": 9, "cache_write": 6, "output": 21}
    assert all_time["top_sessions"][0]["session_id"] == SID
    assert report["activity"][0] == {
        "day": "2026-11-24", "provider": "claude", "wolt": "alpha",
        "model": "claude-example", "session_id": SID,
        "metrics": {"input_fresh": 10, "cache_read": 6, "cache_write": 4, "output": 14},
    }
    assert all_time["by_wolt"]["alpha"]["input_fresh"] == 10
    assert "never export" not in (tmp_path / "history.json").read_text()
    assert "never export" not in json.dumps(report)


def test_days_and_periods_follow_request_time_in_the_lodge_zone(tmp_path):
    emit(claude(tmp_path), request("one", "2026-11-24T04:59:59Z"), request("two"))
    now = datetime(2026, 11, 24, 5, 15, tzinfo=timezone.utc)
    report = save(tmp_path, now)
    assert report["timezone"] == ZONE.key
    assert [row["day"] for row in report["by_day"]] == ["2026-11-23", "2026-11-24"]
    assert report["periods"]["today"]["totals"]["input_fresh"] == 5
    assert report["periods"]["last_7_days"]["totals"]["input_fresh"] == 10


def test_daylight_saving_change_keeps_calendar_days(tmp_path):
    # 2026-11-01 is 24 h + 1 h long in New York: 04:30Z is 00:30 EDT, 04:30Z next day 23:30 EST.
    emit(claude(tmp_path), request("one", "2026-11-01T04:30:00Z"), request("two", "2026-11-02T04:30:00Z"),
         request("three", "2026-11-02T05:30:00Z"))
    days = [row["day"] for row in save(tmp_path)["activity"]]
    assert days == ["2026-11-01", "2026-11-02"]
    assert [row["metrics"]["input_fresh"] for row in save(tmp_path)["by_day"]] == [10, 5]


def test_synthetic_usage_is_absent(tmp_path):
    emit(claude(tmp_path), request("one", model="<synthetic>"))
    assert save(tmp_path)["periods"]["all_time"]["totals"] is None


def test_codex_delta_split_and_request_model(tmp_path):
    first = {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 8,
             "reasoning_output_tokens": 3, "total_tokens": 18}
    second = {"input_tokens": 9, "cached_input_tokens": 2, "output_tokens": 7,
              "reasoning_output_tokens": 1, "total_tokens": 16}
    total = {key: value + second[key] for key, value in first.items()}
    emit(codex(tmp_path), {"type": "session_meta", "payload": {"id": SID}},
         {"type": "turn_context", "payload": {"model": "codex-a"}}, token(first, first),
         {"type": "turn_context", "payload": {"model": "codex-b"}}, token(second, total))
    result = save(tmp_path)
    assert result["periods"]["all_time"]["totals"] == {
        "input_fresh": 13, "cache_read": 6, "cache_write": 0, "output": 15,
    }
    assert [row["model"] for row in result["periods"]["all_time"]["by_model"]] == ["codex-a", "codex-b"]
    assert all(gap["reason"] != "codex_total_mismatch" for gap in result["coverage_gaps"])


def test_codex_unchanged_total_counts_once_but_updates_limits(tmp_path):
    delta = {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 8, "total_tokens": 18}
    limits = {"primary": {"used_percent": 25, "window_minutes": 10080, "resets_at": 123}}
    emit(codex(tmp_path), token(delta, delta),
         token(delta, delta, "2026-11-25T05:10:00Z", limits=limits),
         token({"total_tokens": 18}, delta))
    result = save(tmp_path)
    assert len(result["by_day"]) == 1
    assert result["periods"]["all_time"]["totals"] == {
        "input_fresh": 6, "cache_read": 4, "cache_write": 0, "output": 8,
    }
    assert result["limits"]["codex"]["windows"]["primary"]["used_percent"] == 25
    assert all(gap["reason"] != "codex_total_mismatch" for gap in result["coverage_gaps"])


def test_codex_flags_mismatch_even_if_total_tokens_agree(tmp_path):
    emit(codex(tmp_path), token({"input_tokens": 10, "total_tokens": 10},
                                {"input_tokens": 10, "cached_input_tokens": 4, "total_tokens": 10}))
    assert any(gap["reason"] == "codex_total_mismatch" for gap in save(tmp_path)["coverage_gaps"])


def test_history_replaces_then_retains_partial_days(tmp_path):
    first = emit(claude(tmp_path), request("one"))
    second = emit(claude(tmp_path, "-extra"), request("two"))
    assert save(tmp_path)["periods"]["all_time"]["totals"]["input_fresh"] == 10
    emit(first, request("one", fresh=6))
    assert save(tmp_path)["periods"]["all_time"]["totals"]["input_fresh"] == 11
    assert save(tmp_path)["periods"]["all_time"]["totals"]["input_fresh"] == 11
    second.unlink()
    emit(first, request("one", fresh=7), request("three", "2026-11-25T05:10:00Z"))
    updated = save(tmp_path)
    assert [row["metrics"]["input_fresh"] for row in updated["by_day"]] == [12, 5]
    assert any(gap["reason"] == "retained_partial_day" for gap in updated["coverage_gaps"])
    first.unlink()
    assert save(tmp_path)["by_day"] == updated["by_day"]


def test_a_retained_day_keeps_the_larger_of_saved_and_fresh_rows(tmp_path):
    other = "00000000-0000-0000-0000-000000000002"
    growing = emit(claude(tmp_path), request("one"))
    expiring = emit(claude(tmp_path, sid=other), request("two"))
    save(tmp_path)  # a mid-day save, as after the wolf's catch-up
    expiring.unlink()  # the engine deletes an old transcript ...
    emit(growing, request("one"), request("three", fresh=6))  # ... while another grows the same day
    report = save(tmp_path)
    sessions = {row["session_id"]: row["metrics"]["input_fresh"] for row in report["activity"]}
    assert sessions == {SID: 11, other: 5}  # fresh for the growing one, saved for the expired one


def test_plan_limits_follow_event_timestamp_and_allow_limit_only_event(tmp_path):
    def limit(percent):
        return {"primary": {"used_percent": percent, "window_minutes": 10080, "resets_at": 123}}
    emit(codex(tmp_path), token(limits=limit(25)), token(stamp="2026-11-23T05:10:00Z", limits=limit(15)))
    (tmp_path / "claude-limits.json").write_text(json.dumps({
        "read_at": "2026-11-24T05:10:00Z", "rate_limits": {"five_hour": {"used_percentage": 0, "resets_at": 123}},
    }))
    result = save(tmp_path)
    assert result["limits"]["codex"]["windows"]["primary"]["used_percent"] == 25
    assert result["limits"]["claude"]["windows"]["five_hour"]["used_percent"] == 0


def test_claude_limits_are_unavailable_without_the_optional_feed(tmp_path):
    emit(claude(tmp_path), request("one"))
    result = save(tmp_path)
    assert result["limits"]["claude"] is None
    assert {"reason": "plan_limits_unavailable", "provider": "claude", "wolt": None, "day": None,
            "count": 1} in result["coverage_gaps"]


def test_invalid_lines_and_missing_provider_are_coverage_gaps(tmp_path):
    main = emit(claude(tmp_path), request("one"))
    save(tmp_path)
    emit(main, request("one"), request("two"))
    with main.open("a") as stream:
        stream.write("{malformed\n{incomplete")
    wolt(tmp_path, "beta")
    result = save(tmp_path)
    reasons = {gap["reason"] for gap in result["coverage_gaps"]}
    assert {"invalid_jsonl", "partial_final_line", "no_supported_transcripts", "plan_limits_unavailable"} <= reasons
    # A broken line retains the day; its readable requests still raise it (saved 5, fresh 10).
    assert result["periods"]["all_time"]["totals"]["input_fresh"] == 10
    assert any(gap["reason"] == "retained_partial_day" for gap in result["coverage_gaps"])

    emit(main, request("one"), request("two"))
    with main.open("a") as stream:
        stream.write("{incomplete")
    result = save(tmp_path)
    assert any(gap["reason"] == "partial_final_line" for gap in result["coverage_gaps"])
    assert result["periods"]["all_time"]["totals"]["input_fresh"] == 10


# ── History file, lock and zone ──

def test_save_writes_owner_only_history_with_its_zone(tmp_path):
    emit(claude(tmp_path), request("one"))
    history = usage_collector.history_path(tmp_path / "wolts")
    usage_collector.save(tmp_path / "wolts", zone=ZONE, bases={})
    saved = json.loads(history.read_text())
    assert saved["timezone"] == ZONE.key and saved["rows"]
    for path in (history, history.with_name("history.json.lock")):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(history.parent.stat().st_mode) == 0o700
    assert history.parent == tmp_path / "wolts/.space/usage"


def test_count_never_writes_or_creates_anything(tmp_path):
    transcript = emit(claude(tmp_path), request("one"))
    count(tmp_path)
    assert not (tmp_path / "history.json").exists() and not (tmp_path / "history.json.lock").exists()
    save(tmp_path)
    paths = (tmp_path / "history.json", tmp_path / "history.json.lock")
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
    emit(transcript, request("one"), request("two", fresh=7))
    assert count(tmp_path)["periods"]["all_time"]["totals"]["input_fresh"] == 12
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths] == before


def test_in_memory_count_equals_a_save_at_the_same_time(tmp_path):
    transcript = emit(claude(tmp_path), request("one"))
    save(tmp_path)
    emit(transcript, request("one"), request("two", fresh=7))
    now = datetime(2026, 11, 24, 12, tzinfo=timezone.utc)
    assert count(tmp_path, now) == save(tmp_path, now)


def test_history_keeps_its_zone_when_the_machine_zone_changes(tmp_path):
    emit(claude(tmp_path), request("late", "2026-11-24T04:30:00Z"))  # 23:30 on the 23rd in New York
    save(tmp_path)
    paris = ZoneInfo("Europe/Paris")
    for call in (usage_collector.count, usage_collector.save):
        report = call(tmp_path / "wolts", tmp_path / "history.json", tmp_path / "none.json", zone=paris, bases={})
        assert report["timezone"] == ZONE.key
        assert [row["day"] for row in report["by_day"]] == ["2026-11-23"]
        assert {"reason": "reporting_zone_differs", "provider": None, "wolt": None, "day": None,
                "count": 1} in report["coverage_gaps"]
    assert json.loads((tmp_path / "history.json").read_text())["timezone"] == ZONE.key
    assert all(gap["reason"] != "reporting_zone_differs" for gap in save(tmp_path)["coverage_gaps"])


def test_a_machine_zone_without_an_iana_name(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_collector, "machine_zone", lambda: None)
    emit(claude(tmp_path), request("one"))
    paths = (tmp_path / "wolts", tmp_path / "history.json", tmp_path / "none.json")
    fresh = usage_collector.save(*paths, bases={})  # a fresh history: UTC, and said so
    assert fresh["timezone"] == "UTC"
    assert any(gap["reason"] == "reporting_zone_differs" for gap in fresh["coverage_gaps"])
    (tmp_path / "history.json").unlink()
    save(tmp_path)  # a history started in New York keeps New York
    kept = usage_collector.count(*paths, bases={})
    assert kept["timezone"] == ZONE.key
    assert any(gap["reason"] == "reporting_zone_differs" for gap in kept["coverage_gaps"])


def test_history_without_a_recorded_zone_is_refused(tmp_path):
    emit(tmp_path / "history.json", {"schema_version": 1, "rows": [
        {"day": "2026-11-24", "provider": "claude", "wolt": "alpha", "session_id": SID,
         "model": "m", "metric": "output", "amount": 1}]})
    with pytest.raises(usage_collector.HistoryZoneMismatch):
        count(tmp_path)


# ── Native attribution: the operator's shared engine home ──

def test_shared_home_transcripts_count_for_the_wolt_whose_folder_they_ran_in(tmp_path):
    alpha, beta = wolt(tmp_path, "alpha"), wolt(tmp_path, "beta")
    bases = host(tmp_path)
    emit(bases["claude"] / "-wolts-alpha/a1.jsonl", request("one", cwd=alpha / "work"))
    emit(bases["codex"] / "2026/11/24/rollout-b.jsonl",
         {"type": "session_meta", "payload": {"id": SID, "cwd": str(beta)}},
         {"type": "turn_context", "payload": {"model": "codex-a"}},
         token({"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
               {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}))
    report = save(tmp_path, bases=bases)
    by_wolt = report["periods"]["all_time"]["by_wolt"]
    assert by_wolt["alpha"]["input_fresh"] == 5 and by_wolt["beta"]["input_fresh"] == 4


def test_operator_sessions_in_the_shared_home_are_never_counted_or_listed(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    wolt(tmp_path, "alpha-two")
    bases = host(tmp_path)
    emit(bases["claude"] / "-home-op/op.jsonl", request("op1", cwd=tmp_path / "home/project", model="op-model"))
    emit(bases["claude"] / "-home-op/near.jsonl", request("op2", cwd=tmp_path / "wolts/apps/tool", model="op-model"))
    emit(bases["claude"] / "-home-op/none.jsonl", request("op3", model="op-model"))  # no cwd at all
    emit(bases["claude"] / "-home-op/broken.jsonl", {"cwd": str(tmp_path / "home")}, "not an object")
    emit(bases["codex"] / "rollout-op.jsonl",
         {"type": "session_meta", "payload": {"id": "op-session", "cwd": str(tmp_path / "home")}},
         token({"input_tokens": 9, "total_tokens": 9}, {"input_tokens": 9, "total_tokens": 9}))
    emit(bases["claude"] / "-wolts-alpha/a1.jsonl", request("one", cwd=alpha))
    report = save(tmp_path, bases=bases)
    text = json.dumps(report) + (tmp_path / "history.json").read_text()
    assert "op-model" not in text and "op-session" not in text and "-home-op" not in text
    assert report["periods"]["all_time"]["totals"]["input_fresh"] == 5
    assert {row["wolt"] for row in report["activity"]} == {"alpha"}
    assert all(gap["wolt"] in (None, "alpha", "alpha-two") for gap in report["coverage_gaps"])


def test_a_folder_name_prefix_is_not_a_match(tmp_path):
    wolt(tmp_path, "alpha")
    two = wolt(tmp_path, "alpha-two")
    bases = host(tmp_path)
    emit(bases["claude"] / "p/x.jsonl", request("one", cwd=two))
    assert set(save(tmp_path, bases=bases)["periods"]["all_time"]["by_wolt"]) == {"alpha-two"}


def test_a_transcript_seen_in_both_layouts_counts_once(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    events = (request("one", cwd=alpha), request("two", cwd=alpha))
    emit(alpha / f".claude/projects/project/{SID}.jsonl", *events)
    emit(bases["claude"] / f"project/{SID}.jsonl", *events)  # same file, other layout
    emit(bases["claude"] / "elsewhere/copy.jsonl", *events)  # same requests, other file name
    rollout = ({"type": "session_meta", "payload": {"id": SID, "cwd": str(alpha)}},
               token({"input_tokens": 4, "total_tokens": 4}, {"input_tokens": 4, "total_tokens": 4}))
    emit(alpha / ".codex/sessions/2026/11/24/rollout-x.jsonl", *rollout)
    emit(bases["codex"] / "2026/11/24/rollout-x.jsonl", *rollout)
    report = save(tmp_path, bases=bases)
    totals = {row["provider"]: row["metrics"]["input_fresh"] for row in report["by_day"]}
    assert totals == {"claude": 10, "codex": 4}


def test_the_wolts_own_folder_as_engine_home_is_not_read_twice(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    emit(alpha / f".claude/projects/project/{SID}.jsonl", request("one", cwd=alpha))
    bases = {"claude": alpha / ".claude/projects", "codex": alpha / ".codex/sessions"}
    assert save(tmp_path, bases=bases)["periods"]["all_time"]["totals"]["input_fresh"] == 5


def test_lodge_session_records_attribute_a_session_without_a_wolt_folder(tmp_path):
    import lodge_db
    wolt(tmp_path, "beta")
    recorded = "00000000-0000-0000-0000-0000000000aa"
    with lodge_db.write(tmp_path / "wolts") as conn:
        conn.execute("INSERT INTO sessions(name, wolt, status, created_at, harness, harness_session_id,"
                     " record, updated_at) VALUES ('beta-1', 'beta', 'stopped', 0, 'claude', ?, '{}', 0)",
                     (recorded.upper(),))
    bases = host(tmp_path)
    emit(bases["claude"] / f"-srv/{recorded}.jsonl", request("one", cwd=tmp_path / "srv"))
    emit(bases["claude"] / f"-srv/{SID}.jsonl", request("two", cwd=tmp_path / "srv"))  # not a lodge session
    report = save(tmp_path, bases=bases)
    assert {row["session_id"]: row["wolt"] for row in report["activity"]} == {recorded: "beta"}


def test_an_engine_folder_linked_outside_the_wolt_is_attributed_not_owned(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    elsewhere = tmp_path / "home/.claude/projects"  # the operator's home, reached through a link
    emit(elsewhere / "-home-op/op.jsonl", request("op1", cwd=tmp_path / "home", model="op-model"))
    emit(elsewhere / "-wolts-alpha/a1.jsonl", request("one", cwd=alpha))
    (alpha / ".claude").mkdir()
    (alpha / ".claude/projects").symlink_to(elsewhere)
    for bases in (host(tmp_path), {}):  # the same folder named as the shared home, or not at all
        report = save(tmp_path, bases=bases)
        assert report["periods"]["all_time"]["totals"]["input_fresh"] == 5
        assert "op-model" not in json.dumps(report)


def test_a_folder_without_a_wolt_manifest_is_not_counted(tmp_path):
    wolt(tmp_path, "alpha")
    emit(tmp_path / "wolts/tools/.claude/projects/p/x.jsonl", request("one", model="tool-model"))
    report = save(tmp_path)
    assert report["periods"]["all_time"]["totals"] is None and "tools" not in json.dumps(report)


def rollout(cwd, *deltas):
    events = [{"type": "session_meta", "payload": {"id": SID, "cwd": str(cwd)}}]
    total = {"input_tokens": 0, "total_tokens": 0}
    for amount in deltas:
        total = {key: value + amount for key, value in total.items()}
        events.append(token({"input_tokens": amount, "total_tokens": amount}, dict(total)))
    return events


def test_a_renamed_copy_of_a_codex_rollout_counts_once(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(alpha / ".codex/sessions/2026/11/24/rollout-a.jsonl", *rollout(alpha, 4))
    emit(bases["codex"] / "2026/11/24/rollout-renamed.jsonl", *rollout(alpha, 4))
    emit(bases["codex"] / "copies/rollout-again.jsonl", *rollout(alpha, 4))
    assert save(tmp_path, bases=bases)["periods"]["all_time"]["totals"]["input_fresh"] == 4


def test_the_newer_longer_copy_of_a_codex_rollout_keeps_its_extra_requests(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(alpha / ".codex/sessions/2026/11/24/rollout-a.jsonl", *rollout(alpha, 4))
    emit(bases["codex"] / "2026/11/24/rollout-b.jsonl", *rollout(alpha, 4, 3))
    assert save(tmp_path, bases=bases)["periods"]["all_time"]["totals"]["input_fresh"] == 7


def test_switching_to_a_longer_codex_copy_is_not_a_lost_source(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(alpha / ".codex/sessions/2026/11/24/rollout-a.jsonl", *rollout(alpha, 4))
    save(tmp_path, bases=bases)
    emit(bases["codex"] / "2026/11/24/rollout-b.jsonl", *rollout(alpha, 4, 3))  # the same session, grown
    for _ in range(2):
        report = save(tmp_path, bases=bases)
        assert all(gap["reason"] != "retained_partial_day" for gap in report["coverage_gaps"])
        assert report["periods"]["all_time"]["totals"]["input_fresh"] == 7
    (bases["codex"] / "2026/11/24/rollout-b.jsonl").unlink()  # the winner goes: a real loss, kept
    report = save(tmp_path, bases=bases)
    assert any(gap["reason"] == "retained_partial_day" for gap in report["coverage_gaps"])
    assert report["periods"]["all_time"]["totals"]["input_fresh"] == 7


def test_a_same_named_copy_in_the_shared_home_is_its_own_source(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(alpha / ".codex/sessions/2026/11/24/rollout-a.jsonl", *rollout(alpha, 4))
    longer = emit(bases["codex"] / "2026/11/24/rollout-a.jsonl", *rollout(alpha, 4, 3))  # same relative path
    assert save(tmp_path, bases=bases)["periods"]["all_time"]["totals"]["input_fresh"] == 7
    longer.unlink()
    report = save(tmp_path, bases=bases)
    assert report["periods"]["all_time"]["totals"]["input_fresh"] == 7
    assert any(gap["reason"] == "retained_partial_day" for gap in report["coverage_gaps"])


def test_history_that_recorded_codex_files_by_path_reads_as_the_same_sources(tmp_path):
    import hashlib
    alpha = wolt(tmp_path, "alpha")
    emit(alpha / ".codex/sessions/2026/11/24/rollout-a.jsonl", *rollout(alpha, 4))
    save(tmp_path)
    saved = json.loads((tmp_path / "history.json").read_text())
    for entry in saved["coverage"]:  # as an older save wrote it: by relative file path
        entry["sources"] = [hashlib.sha256(b"2026/11/24/rollout-a.jsonl").hexdigest()]
    emit(tmp_path / "history.json", saved)
    report = save(tmp_path)
    assert all(gap["reason"] != "retained_partial_day" for gap in report["coverage_gaps"])


def test_malformed_shared_records_are_skipped_and_counting_goes_on(tmp_path):
    alpha = wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    emit(bases["codex"] / "a/rollout-1.jsonl", {"type": "session_meta", "payload": "not an object"})
    emit(bases["codex"] / "a/rollout-2.jsonl", {"type": "turn_context", "payload": ["list"]}, {"payload": 3})
    emit(bases["claude"] / "p/odd.jsonl", {"cwd": 42}, {"cwd": ["x"]})
    emit(bases["codex"] / "b/rollout-3.jsonl", *rollout(alpha, 4))
    emit(bases["claude"] / "p/a1.jsonl", request("one", cwd=alpha))
    report = save(tmp_path, bases=bases)
    assert {row["provider"]: row["metrics"]["input_fresh"] for row in report["by_day"]} == {"claude": 5, "codex": 4}


def test_an_older_lock_is_made_owner_only(tmp_path):
    emit(claude(tmp_path), request("one"))
    lock = tmp_path / "history.json.lock"
    lock.touch(mode=0o644)
    lock.chmod(0o644)
    save(tmp_path)
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_unreadable_shared_home_is_skipped_quietly(tmp_path):
    wolt(tmp_path, "alpha")
    bases = host(tmp_path)
    secret = emit(bases["claude"] / "p/locked.jsonl", request("one", cwd=tmp_path / "home"))
    os.chmod(secret, 0)
    try:
        report = save(tmp_path, bases=bases)
    finally:
        os.chmod(secret, 0o600)
    assert all(gap["reason"] != "transcript_unreadable" for gap in report["coverage_gaps"])


# ── Backup and restore ──

def test_history_survives_backup_and_restore_and_saving_resumes(tmp_path):
    from woltspace.backup import create_backup, restore_backup
    root = tmp_path / "wolts"
    wolt(tmp_path, "alpha")
    emit(claude(tmp_path), request("one"))
    history = usage_collector.history_path(root)
    usage_collector.save(root, zone=ZONE, bases={})
    restored = restore_backup(create_backup(root, out_dir=tmp_path / "out").archive,
                              to=tmp_path / "restored").wolts_dir
    copy = usage_collector.history_path(restored)
    assert copy.read_bytes() == history.read_bytes()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    copy.with_name("history.json.lock").unlink(missing_ok=True)  # a restore may drop the lock
    report = usage_collector.save(restored, zone=ZONE, bases={})
    assert copy.with_name("history.json.lock").exists()
    assert report["periods"]["all_time"]["totals"]["input_fresh"] == 5
