"""Wolf core — the cron logic the scheduler and the lodge API share.

Pure Python, no server: strict parsing, the AND of day-of-month and
day-of-week, next_run across DST, the one due check, per-wolt stamps and their
migration, and the locked atomic wolf.json write.

Usage: uv run pytest test/test_wolfcore.py -v
"""

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import wolfcore  # noqa: E402
from wolfcore import CronError, due_slot, next_run, parse_cron  # noqa: E402


@pytest.fixture
def tz(monkeypatch):
    """Pin the process's local zone (the wolf's clock) for one test."""
    def set_zone(name):
        monkeypatch.setenv("TZ", name)
        time.tzset()
    yield set_zone
    monkeypatch.undo()
    time.tzset()


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

class TestStrictParser:

    @pytest.mark.parametrize("expr, needle", [
        ("61 * * * *", "minute: 61 is out of range 0-59"),
        ("0 24 * * *", "hour: 24 is out of range 0-23"),
        ("0 0 0 * *", "day-of-month: 0 is out of range 1-31"),
        ("0 0 32 * *", "day-of-month: 32"),
        ("0 0 * 13 *", "month: 13"),
        ("0 0 * * 8", "day-of-week: 8"),
        ("*/0 * * * *", "step '0'"),
        ("a * * * *", "'a' is not a number"),
        ("0 0 * JAN *", "'JAN' is not a number"),
        ("5-1 * * * *", "runs backwards"),
        ("1,,2 * * * *", "empty list item"),
        ("0 6 * *", "needs 5 fields"),
        ("0 6 * * * *", "needs 5 fields"),
        ("", "needs 5 fields"),
    ])
    def test_rejects_with_a_clear_message(self, expr, needle):
        with pytest.raises(CronError) as exc:
            parse_cron(expr)
        assert needle in str(exc.value)
        assert exc.value.field == "schedule"

    def test_accepts_the_supported_forms(self):
        cron = parse_cron("*/15 9-17 1,15 * 1-5")
        assert cron.minutes == {0, 15, 30, 45}
        assert cron.hours == set(range(9, 18))
        assert cron.days == {1, 15}
        assert cron.weekdays == {1, 2, 3, 4, 5}

    def test_n_step_and_range_step(self):
        assert parse_cron("5/20 * * * *").minutes == {5, 25, 45}
        assert parse_cron("1-10/3 * * * *").minutes == {1, 4, 7, 10}

    def test_seven_is_sunday(self):
        assert parse_cron("0 0 * * 7").weekdays == {0}
        assert wolfcore.cron_matches("0 0 * * 7", datetime(2026, 3, 15, 0, 0))  # Sunday

    def test_cron_matches_is_tolerant(self):
        assert not wolfcore.cron_matches("61 * * * *", datetime(2026, 1, 1, 0, 1))
        assert not wolfcore.cron_matches("garbage", datetime(2026, 1, 1))


class TestDayOfMonthAndDayOfWeekAreAnded:
    """Classic cron ORs them; the wolf always ANDed them, and still does."""

    def test_friday_the_13th_only(self):
        fri_13 = datetime(2026, 2, 13, 9, 0)      # a Friday
        thu_13 = datetime(2026, 8, 13, 9, 0)      # a Thursday
        fri_14 = datetime(2026, 2, 20, 9, 0)      # a Friday, not the 13th
        assert wolfcore.cron_matches("0 9 13 * 5", fri_13)
        assert not wolfcore.cron_matches("0 9 13 * 5", thu_13)
        assert not wolfcore.cron_matches("0 9 13 * 5", fri_14)

    def test_next_run_honours_the_and(self, tz):
        tz("UTC")
        now = datetime(2026, 1, 1, tzinfo=None).astimezone()
        assert next_run("0 9 13 * 5", now).date().isoformat() == "2026-02-13"


# ---------------------------------------------------------------------------
# next_run
# ---------------------------------------------------------------------------

class TestNextRun:

    def test_later_today_then_tomorrow(self, tz):
        tz("UTC")
        now = datetime(2026, 3, 15, 5, 59, 30).astimezone()
        assert next_run("0 6 * * *", now).isoformat() == "2026-03-15T06:00:00+00:00"
        now = datetime(2026, 3, 15, 6, 0, 10).astimezone()
        assert next_run("0 6 * * *", now).isoformat() == "2026-03-16T06:00:00+00:00"

    def test_never_within_a_year_is_none(self, tz):
        tz("UTC")
        assert next_run("0 0 31 2 *", datetime(2026, 1, 1).astimezone()) is None

    def test_across_spring_forward(self, tz):
        """02:30 does not exist on the spring-forward day; the offset changes."""
        tz("America/New_York")
        # 2026-03-08 02:30 never happens: the next 02:30 after the 7th is the 9th, in EDT
        after_7th = datetime(2026, 3, 7, 3, 0).astimezone()          # EST, -05:00
        assert next_run("30 2 * * *", after_7th).isoformat() == "2026-03-09T02:30:00-04:00"
        assert next_run("0 9 * * *", after_7th).isoformat() == "2026-03-07T09:00:00-05:00"
        sunday = datetime(2026, 3, 8, 0, 0).astimezone()
        assert next_run("0 9 * * *", sunday).isoformat() == "2026-03-08T09:00:00-04:00"

    def test_across_fall_back(self, tz):
        """01:30 happens twice on fall-back; the first one is the run."""
        tz("America/New_York")
        now = datetime(2026, 10, 31, 12, 0).astimezone()
        assert next_run("30 1 * * *", now).isoformat() == "2026-11-01T01:30:00-04:00"


# ---------------------------------------------------------------------------
# The one due check
# ---------------------------------------------------------------------------

class TestDueSlot:

    def test_loop_fires_only_in_the_matching_minute(self, tz):
        tz("UTC")
        entry = {"name": "d", "schedule": "0 6 * * *"}
        at_six = datetime(2026, 3, 15, 6, 0, 40).astimezone()
        assert due_slot(entry, at_six, None) == at_six.replace(second=0)
        assert due_slot(entry, at_six + timedelta(minutes=1), None) is None

    def test_same_minute_stamp_is_not_due(self, tz):
        tz("UTC")
        entry = {"name": "d", "schedule": "0 6 * * *"}
        now = datetime(2026, 3, 15, 6, 0).astimezone()
        assert due_slot(entry, now, "2026-03-15-06:00") is None

    def test_catch_up_finds_the_most_recent_miss_within_a_day(self, tz):
        tz("UTC")
        entry = {"name": "d", "schedule": "0 6 * * *"}
        now = datetime(2026, 3, 15, 9, 17).astimezone()
        slot = due_slot(entry, now, "2026-03-14-06:00", wolfcore.CATCH_UP_WINDOW)
        assert slot.strftime(wolfcore.STAMP_FORMAT) == "2026-03-15-06:00"
        assert due_slot(entry, now, "2026-03-15-06:00", wolfcore.CATCH_UP_WINDOW) is None

    def test_catch_up_does_not_reach_past_the_window(self, tz):
        tz("UTC")
        entry = {"name": "w", "schedule": "0 6 * * 1"}   # Mondays
        wednesday = datetime(2026, 3, 18, 9, 0).astimezone()
        assert due_slot(entry, wednesday, None, wolfcore.CATCH_UP_WINDOW) is None

    def test_one_off_is_due_once_its_time_passes(self, tz):
        tz("UTC")
        entry = {"name": "o", "at": "2026-03-21T20:00"}
        assert due_slot(entry, datetime(2026, 3, 21, 19, 59).astimezone(), None) is None
        now = datetime(2026, 3, 21, 20, 0).astimezone()
        assert due_slot(entry, now, None) == now

    def test_unreadable_entries_raise(self):
        now = datetime.now().astimezone()
        with pytest.raises(CronError):
            due_slot({"name": "x", "schedule": "61 * * * *"}, now, None)
        with pytest.raises(CronError):
            due_slot({"name": "x", "at": "tomorrow"}, now, None)
        with pytest.raises(CronError):
            due_slot({"name": "x"}, now, None)


class TestTheSchedulerUsesTheOneCheck:
    """The loop and catch-up both go through wolfcore.due_slot."""

    def test_loop_and_catch_up_share_due_slot(self, tmp_path):
        from creatures import wolf
        crons = [{"name": "d", "schedule": "0 6 * * *", "prompt": "p", "_owner": "nunu"}]
        now = datetime(2026, 3, 15, 6, 0).astimezone()
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron"), \
             patch("wolfcore.due_slot", wraps=wolfcore.due_slot) as spy:
            wolf.check_and_fire(crons, now)
            wolf.catch_up(crons, now)
        lookbacks = [call.args[3] for call in spy.call_args_list]
        assert lookbacks == [timedelta(0), wolfcore.CATCH_UP_WINDOW]

    def test_catch_up_journals_the_missed_minute(self, tmp_path):
        from creatures import wolf
        crons = [{"name": "d", "schedule": "0 6 * * *", "prompt": "p", "_owner": "nunu"}]
        now = datetime(2026, 3, 15, 9, 0).astimezone()
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as fire:
            wolf.catch_up(crons, now)
            wolf.catch_up(crons, now)                     # already caught up
        assert fire.call_count == 1
        line = json.loads((tmp_path / "jobs.jsonl").read_text().splitlines()[0])
        assert line["event"] == "catch-up" and line["owner"] == "nunu"
        assert line["missed"].endswith("-06:00")
        assert (tmp_path / "nunu" / "d.last").read_text().endswith("-06:00")

    def test_catch_up_false_opts_out(self, tmp_path):
        from creatures import wolf
        crons = [{"name": "d", "schedule": "0 6 * * *", "prompt": "p",
                  "_owner": "nunu", "catch_up": False}]
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as fire:
            wolf.catch_up(crons, datetime(2026, 3, 15, 9, 0).astimezone())
        fire.assert_not_called()

    def test_one_off_fires_then_is_removed(self, tmp_path):
        from creatures import wolf
        crons = [{"name": "o", "at": "2026-03-21T20:00", "prompt": "p", "_owner": "nunu"}]
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as fire, \
             patch("creatures.wolf.remove_cron") as remove:
            wolf.check_and_fire(crons, datetime(2026, 3, 21, 21, 0).astimezone())
        fire.assert_called_once()
        remove.assert_called_once_with("nunu", "o")

    def test_a_bad_entry_is_skipped_and_the_rest_still_fire(self, tmp_path, capsys):
        from creatures import wolf
        crons = [
            {"name": "bad", "schedule": "61 * * * *", "prompt": "p", "_owner": "nunu"},
            {"name": "../evil", "schedule": "* * * * *", "prompt": "p", "_owner": "nunu"},
            {"name": "good", "schedule": "* * * * *", "prompt": "p", "_owner": "nunu"},
        ]
        now = datetime(2026, 3, 15, 6, 0).astimezone()
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as fire:
            wolf.check_and_fire(crons, now)
            wolf.check_and_fire(crons, now + timedelta(minutes=1))
        assert [c.args[0]["name"] for c in fire.call_args_list] == ["good", "good"]
        err = capsys.readouterr().err
        assert err.count("minute: 61") == 1                 # logged once, not every tick
        assert not list(tmp_path.rglob("evil.last"))

    def test_same_name_in_two_wolts_does_not_collide(self, tmp_path):
        from creatures import wolf
        crons = [
            {"name": "digest", "schedule": "0 6 * * *", "prompt": "p", "_owner": "alpha"},
            {"name": "digest", "schedule": "0 6 * * *", "prompt": "p", "_owner": "beta"},
        ]
        now = datetime(2026, 3, 15, 6, 0).astimezone()
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as fire:
            wolf.check_and_fire(crons, now)
        assert fire.call_count == 2
        assert (tmp_path / "alpha" / "digest.last").exists()
        assert (tmp_path / "beta" / "digest.last").exists()


# ---------------------------------------------------------------------------
# Per-wolt stamps
# ---------------------------------------------------------------------------

class TestStamps:

    def test_names_that_are_not_identifiers_never_become_paths(self, tmp_path):
        assert wolfcore.stamp_path(tmp_path, "nunu", "../x") is None
        assert wolfcore.stamp_path(tmp_path, "..", "x") is None
        assert wolfcore.stamp_path(tmp_path, "a b", "x") is None
        assert wolfcore.read_last_run(tmp_path, "nunu", "../../etc/passwd") is None
        with pytest.raises(CronError):
            wolfcore.write_last_run(tmp_path, "nunu", "a/b", datetime.now())

    def test_legacy_stamp_is_read_until_migrated(self, tmp_path):
        (tmp_path / "digest.last").write_text("2026-03-15-06:00\n")
        assert wolfcore.read_last_run(tmp_path, "alpha", "digest") == "2026-03-15-06:00"

    def test_stamp_to_iso_carries_the_offset(self, tz):
        tz("America/New_York")
        assert wolfcore.stamp_to_iso("2026-07-01-06:00") == "2026-07-01T06:00:00-04:00"
        assert wolfcore.stamp_to_iso("nonsense") is None
        assert wolfcore.stamp_to_iso(None) is None


class TestMigration:

    def test_unique_owner_gets_it_moved(self, tmp_path):
        (tmp_path / "digest.last").write_text("2026-03-15-06:00")
        moved = wolfcore.migrate_legacy_stamps(tmp_path, {"digest": ["alpha"]})
        assert moved == ["alpha/digest"]
        assert not (tmp_path / "digest.last").exists()
        assert (tmp_path / "alpha" / "digest.last").read_text() == "2026-03-15-06:00"

    def test_ambiguous_name_is_copied_to_every_owner(self, tmp_path):
        (tmp_path / "digest.last").write_text("2026-03-15-06:00")
        wolfcore.migrate_legacy_stamps(tmp_path, {"digest": ["alpha", "beta"]})
        for wolt in ("alpha", "beta"):
            assert (tmp_path / wolt / "digest.last").read_text() == "2026-03-15-06:00"
        assert not (tmp_path / "digest.last").exists()

    def test_idempotent_and_never_overwrites(self, tmp_path):
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "digest.last").write_text("2026-03-16-06:00")
        (tmp_path / "digest.last").write_text("2026-03-15-06:00")
        owners = {"digest": ["alpha"]}
        wolfcore.migrate_legacy_stamps(tmp_path, owners)
        assert wolfcore.migrate_legacy_stamps(tmp_path, owners) == []
        assert (tmp_path / "alpha" / "digest.last").read_text() == "2026-03-16-06:00"

    def test_orphans_and_odd_names_are_left_alone(self, tmp_path):
        (tmp_path / "gone.last").write_text("x")
        (tmp_path / "has space.last").write_text("x")
        wolfcore.migrate_legacy_stamps(tmp_path, {"has space": ["alpha"]})
        assert (tmp_path / "gone.last").exists()
        assert (tmp_path / "has space.last").exists()

    def test_the_wolf_migrates_on_start(self, tmp_path):
        from creatures import wolf
        state = tmp_path / ".space" / "wolf"
        state.mkdir(parents=True)
        (state / "digest.last").write_text("2026-03-15-06:00")
        crons = [{"name": "digest", "_owner": "alpha"}, {"name": "digest", "_owner": "beta"}]
        with patch("creatures.wolf.WOLTS_DIR", tmp_path):
            wolf.migrate_stamps(crons)
        assert (state / "alpha" / "digest.last").exists()
        assert (state / "beta" / "digest.last").exists()


# ---------------------------------------------------------------------------
# wolf.json: lock, atomic write, round-trip
# ---------------------------------------------------------------------------

class TestWolfJson:

    def _wolt(self, wolts, name, data):
        path = wolts / name / "wolt" / "wolf.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return path

    def test_remove_keeps_unknown_keys_and_the_format(self, tmp_path):
        path = self._wolt(tmp_path, "nunu", {"note": "mine", "crons": [
            {"name": "keep", "schedule": "0 6 * * *", "prompt": "p", "timezone": "x", "extra": [1]},
            {"name": "drop", "at": "2026-03-21T20:00", "prompt": "p"},
        ]})
        assert wolfcore.remove_entry(tmp_path, "nunu", "drop") is True
        text = path.read_text()
        assert text.endswith("}\n") and '\n  "crons": [' in text
        data = json.loads(text)
        assert data["note"] == "mine"
        assert data["crons"] == [{"name": "keep", "schedule": "0 6 * * *", "prompt": "p",
                                  "timezone": "x", "extra": [1]}]
        assert wolfcore.remove_entry(tmp_path, "nunu", "drop") is False

    def test_atomic_write_leaves_no_temp_file(self, tmp_path):
        target = tmp_path / "wolf.json"
        wolfcore.atomic_write(target, "{}\n")
        assert target.read_text() == "{}\n"
        assert [p.name for p in tmp_path.iterdir()] == ["wolf.json"]

    def test_the_lock_serialises_writers(self, tmp_path):
        self._wolt(tmp_path, "nunu", {"crons": []})
        order = []

        def hold():
            with wolfcore.wolf_json_lock(tmp_path, "nunu"):
                order.append("a-in")
                time.sleep(0.2)
                order.append("a-out")

        t = threading.Thread(target=hold)
        t.start()
        time.sleep(0.05)
        with wolfcore.wolf_json_lock(tmp_path, "nunu"):
            order.append("b-in")
        t.join()
        assert order == ["a-in", "a-out", "b-in"]

    def test_the_lock_refuses_a_path_like_wolt(self, tmp_path):
        with pytest.raises(CronError):
            with wolfcore.wolf_json_lock(tmp_path, "../x"):
                pass

    def test_load_all_reports_broken_files(self, tmp_path):
        self._wolt(tmp_path, "good", {"crons": [{"name": "a", "schedule": "* * * * *"}]})
        bad = tmp_path / "bad" / "wolt" / "wolf.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{ nope")
        crons, errors = wolfcore.load_all(tmp_path)
        assert [c["_owner"] for c in crons] == ["good"]
        assert [e[0] for e in errors] == ["bad"]


class TestValidateEntry:

    NOW = datetime(2026, 3, 15, 12, 0).astimezone()

    def _check(self, **entry):
        base = {"name": "n", "prompt": "p"}
        return wolfcore.validate_entry({**base, **entry}, self.NOW)

    @pytest.mark.parametrize("entry, field", [
        ({"schedule": "61 * * * *"}, "schedule"),
        ({"schedule": "0 0 31 2 *"}, "schedule"),
        ({}, "schedule"),
        ({"schedule": "0 6 * * *", "at": "2027-01-01T00:00"}, "schedule"),
        ({"at": "2020-01-01T00:00"}, "at"),
        ({"at": "soon"}, "at"),
        ({"schedule": "0 6 * * *", "prompt": "  "}, "prompt"),
        ({"schedule": "0 6 * * *", "name": "no spaces"}, "name"),
        ({"schedule": "0 6 * * *", "notify": 5}, "notify"),
        ({"schedule": "0 6 * * *", "catch_up": "no"}, "catch_up"),
    ])
    def test_errors_name_their_field(self, entry, field):
        with pytest.raises(CronError) as exc:
            self._check(**entry)
        assert exc.value.field == field

    def test_accepts_good_entries(self):
        assert self._check(schedule="0 6 * * *")
        assert self._check(at="2027-01-01T09:00", catch_up=False, notify="hi")

    def test_slug_and_unique_names(self):
        assert wolfcore.slugify("Check if PR #215 passed CI!") == "check-if-pr-215"
        assert wolfcore.slugify("!!!") == "cron"
        assert wolfcore.unique_name("a", {"a", "a-2"}) == "a-3"
        assert wolfcore.unique_name("b", {"a"}) == "b"


def test_local_tz_name_prefers_an_iana_name(monkeypatch):
    monkeypatch.setenv("TZ", "America/Montreal")
    assert wolfcore.local_tz_name() == "America/Montreal"
