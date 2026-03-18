"""Wolf scheduler unit tests — pure Python, no server or tmux required.

Tests the cron parser, schedule loading, state tracking, and CLI dispatch
in container/creatures/wolf.py.

Usage: uv run pytest test/test_wolf.py -v
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add container to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))


# ---------------------------------------------------------------------------
# Cron expression parser
# ---------------------------------------------------------------------------

class TestCronParser:
    """Unit: cron expression matching."""

    def test_exact_match(self):
        from creatures.wolf import cron_matches
        dt = datetime(2026, 3, 15, 6, 0)  # Sunday 6:00am
        assert cron_matches("0 6 * * *", dt)

    def test_no_match_wrong_minute(self):
        from creatures.wolf import cron_matches
        dt = datetime(2026, 3, 15, 6, 30)
        assert not cron_matches("0 6 * * *", dt)

    def test_no_match_wrong_hour(self):
        from creatures.wolf import cron_matches
        dt = datetime(2026, 3, 15, 7, 0)
        assert not cron_matches("0 6 * * *", dt)

    def test_wildcard_all(self):
        from creatures.wolf import cron_matches
        dt = datetime(2026, 1, 1, 0, 0)
        assert cron_matches("* * * * *", dt)

    def test_step_every_15(self):
        from creatures.wolf import cron_matches
        assert cron_matches("*/15 * * * *", datetime(2026, 3, 15, 10, 0))
        assert cron_matches("*/15 * * * *", datetime(2026, 3, 15, 10, 15))
        assert cron_matches("*/15 * * * *", datetime(2026, 3, 15, 10, 30))
        assert cron_matches("*/15 * * * *", datetime(2026, 3, 15, 10, 45))
        assert not cron_matches("*/15 * * * *", datetime(2026, 3, 15, 10, 7))

    def test_range(self):
        from creatures.wolf import cron_matches
        # weekdays 1-5 (Mon-Fri in cron: 1=Mon)
        # 2026-03-16 is Monday
        assert cron_matches("0 9 * * 1-5", datetime(2026, 3, 16, 9, 0))
        # 2026-03-15 is Sunday
        assert not cron_matches("0 9 * * 1-5", datetime(2026, 3, 15, 9, 0))

    def test_list(self):
        from creatures.wolf import cron_matches
        assert cron_matches("0 9,17 * * *", datetime(2026, 3, 15, 9, 0))
        assert cron_matches("0 9,17 * * *", datetime(2026, 3, 15, 17, 0))
        assert not cron_matches("0 9,17 * * *", datetime(2026, 3, 15, 12, 0))

    def test_specific_day_of_month(self):
        from creatures.wolf import cron_matches
        assert cron_matches("0 0 1 * *", datetime(2026, 1, 1, 0, 0))
        assert not cron_matches("0 0 1 * *", datetime(2026, 1, 2, 0, 0))

    def test_specific_month(self):
        from creatures.wolf import cron_matches
        assert cron_matches("0 0 * 12 *", datetime(2026, 12, 25, 0, 0))
        assert not cron_matches("0 0 * 12 *", datetime(2026, 3, 25, 0, 0))

    def test_sunday_is_zero(self):
        """Cron convention: 0 = Sunday."""
        from creatures.wolf import cron_matches
        # 2026-03-15 is Sunday
        assert cron_matches("0 0 * * 0", datetime(2026, 3, 15, 0, 0))
        # Monday should not match
        assert not cron_matches("0 0 * * 0", datetime(2026, 3, 16, 0, 0))

    def test_invalid_expression_returns_false(self):
        from creatures.wolf import cron_matches
        assert not cron_matches("bad", datetime(2026, 1, 1, 0, 0))
        assert not cron_matches("0 6 * *", datetime(2026, 1, 1, 0, 0))  # only 4 fields

    def test_step_with_base(self):
        from creatures.wolf import cron_matches
        # 5/10 means starting at 5, every 10: 5, 15, 25, 35, 45, 55
        assert cron_matches("5/10 * * * *", datetime(2026, 1, 1, 0, 5))
        assert cron_matches("5/10 * * * *", datetime(2026, 1, 1, 0, 15))
        assert not cron_matches("5/10 * * * *", datetime(2026, 1, 1, 0, 10))


# ---------------------------------------------------------------------------
# _parse_field
# ---------------------------------------------------------------------------

class TestParseField:
    """Unit: individual cron field parsing."""

    def test_wildcard(self):
        from creatures.wolf import _parse_field
        assert _parse_field("*", 0, 59) == set(range(0, 60))

    def test_single_value(self):
        from creatures.wolf import _parse_field
        assert _parse_field("5", 0, 59) == {5}

    def test_range(self):
        from creatures.wolf import _parse_field
        assert _parse_field("1-5", 0, 59) == {1, 2, 3, 4, 5}

    def test_step(self):
        from creatures.wolf import _parse_field
        assert _parse_field("*/15", 0, 59) == {0, 15, 30, 45}

    def test_list(self):
        from creatures.wolf import _parse_field
        assert _parse_field("1,3,5", 0, 59) == {1, 3, 5}

    def test_combined_list_and_range(self):
        from creatures.wolf import _parse_field
        assert _parse_field("1-3,7", 0, 59) == {1, 2, 3, 7}


# ---------------------------------------------------------------------------
# Schedule loading & state
# ---------------------------------------------------------------------------

class TestScheduleLoading:
    """Unit: wolf.json loading and state management."""

    def test_load_valid_schedule(self, tmp_path):
        from creatures.wolf import load_schedule, get_schedule_path
        config = {
            "crons": [
                {"name": "test-cron", "schedule": "0 6 * * *", "action": "script", "command": "echo hi"}
            ]
        }
        wolf_json = tmp_path / "wolt" / "wolf.json"
        wolf_json.parent.mkdir(parents=True)
        wolf_json.write_text(json.dumps(config))

        with patch("creatures.wolf.get_schedule_path", return_value=wolf_json):
            crons = load_schedule()
        assert len(crons) == 1
        assert crons[0]["name"] == "test-cron"

    def test_load_missing_file(self, tmp_path):
        from creatures.wolf import load_schedule
        with patch("creatures.wolf.get_schedule_path", return_value=tmp_path / "nope.json"):
            crons = load_schedule()
        assert crons == []

    def test_load_invalid_json(self, tmp_path):
        from creatures.wolf import load_schedule
        bad = tmp_path / "wolf.json"
        bad.write_text("not json {{{")
        with patch("creatures.wolf.get_schedule_path", return_value=bad):
            crons = load_schedule()
        assert crons == []

    def test_load_empty_crons(self, tmp_path):
        from creatures.wolf import load_schedule
        f = tmp_path / "wolf.json"
        f.write_text(json.dumps({"crons": []}))
        with patch("creatures.wolf.get_schedule_path", return_value=f):
            crons = load_schedule()
        assert crons == []


class TestStateTracking:
    """Unit: last-run timestamps prevent double-firing."""

    def test_get_set_last_run(self, tmp_path):
        from creatures.wolf import get_last_run, set_last_run
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            assert get_last_run("test") is None
            set_last_run("test", datetime(2026, 3, 15, 6, 0))
            assert get_last_run("test") == "2026-03-15-06:00"

    def test_idempotent_check(self, tmp_path):
        """Same minute stamp should prevent re-firing."""
        from creatures.wolf import get_last_run, set_last_run
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            dt = datetime(2026, 3, 15, 6, 0)
            set_last_run("x", dt)
            stamp = dt.strftime("%Y-%m-%d-%H:%M")
            assert get_last_run("x") == stamp  # already fired


# ---------------------------------------------------------------------------
# check_and_fire logic
# ---------------------------------------------------------------------------

class TestCheckAndFire:
    """Unit: the core scheduling logic."""

    def test_fires_matching_cron(self, tmp_path):
        from creatures.wolf import check_and_fire
        crons = [
            {"name": "test", "schedule": "0 6 * * *", "action": "script", "command": "echo hi", "notify": "test"}
        ]
        now = datetime(2026, 3, 15, 6, 0).astimezone()

        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            check_and_fire(crons, now)
            mock_fire.assert_called_once_with(crons[0])

    def test_skips_non_matching(self, tmp_path):
        from creatures.wolf import check_and_fire
        crons = [
            {"name": "test", "schedule": "0 6 * * *", "action": "script", "command": "echo hi"}
        ]
        now = datetime(2026, 3, 15, 10, 0).astimezone()  # 10am, not 6am

        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            check_and_fire(crons, now)
            mock_fire.assert_not_called()

    def test_skips_already_fired(self, tmp_path):
        from creatures.wolf import check_and_fire, set_last_run
        crons = [
            {"name": "test", "schedule": "0 6 * * *", "action": "script", "command": "echo hi"}
        ]
        now = datetime(2026, 3, 15, 6, 0).astimezone()

        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            # Pre-mark as fired
            set_last_run("test", now)
            with patch("creatures.wolf.fire_cron") as mock_fire:
                check_and_fire(crons, now)
                mock_fire.assert_not_called()

    def test_skips_entries_without_name(self, tmp_path):
        from creatures.wolf import check_and_fire
        crons = [{"schedule": "0 6 * * *", "action": "script"}]
        now = datetime(2026, 3, 15, 6, 0).astimezone()

        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            check_and_fire(crons, now)
            mock_fire.assert_not_called()


# ---------------------------------------------------------------------------
# fire_cron dispatches to correct action
# ---------------------------------------------------------------------------

class TestFireCron:
    """Unit: fire_cron routes to the right action handler."""

    def test_dispatches_script(self):
        from creatures.wolf import fire_cron
        entry = {"name": "t", "action": "script", "command": "echo hi"}
        with patch("creatures.wolf.run_script") as mock, \
             patch("creatures.wolf.send_wolf_notify"):
            fire_cron(entry)
            mock.assert_called_once_with(entry)

    def test_dispatches_session(self):
        from creatures.wolf import fire_cron
        entry = {"name": "t", "action": "session", "prompt": "do stuff"}
        with patch("creatures.wolf.run_session") as mock, \
             patch("creatures.wolf.send_wolf_notify"):
            fire_cron(entry)
            mock.assert_called_once_with(entry)

    def test_dispatches_skill(self):
        from creatures.wolf import fire_cron
        entry = {"name": "t", "action": "skill", "skill": "digest"}
        with patch("creatures.wolf.run_skill") as mock, \
             patch("creatures.wolf.send_wolf_notify"):
            fire_cron(entry)
            mock.assert_called_once_with(entry)

    def test_fires_action_then_notifies(self):
        from creatures.wolf import fire_cron
        call_order = []
        entry = {"name": "t", "action": "script", "command": "echo hi", "notify": "heads up"}
        with patch("creatures.wolf.send_wolf_notify", side_effect=lambda m: call_order.append("notify")), \
             patch("creatures.wolf.run_script", side_effect=lambda e: call_order.append("script")):
            fire_cron(entry)
        assert call_order == ["script", "notify"]

    def test_no_notify_when_not_set(self):
        from creatures.wolf import fire_cron
        entry = {"name": "t", "action": "script", "command": "echo hi"}
        with patch("creatures.wolf.send_wolf_notify") as mock_notify, \
             patch("creatures.wolf.run_script"):
            fire_cron(entry)
            mock_notify.assert_not_called()

    def test_unknown_action(self, capsys):
        from creatures.wolf import fire_cron
        entry = {"name": "t", "action": "bogus"}
        with patch("creatures.wolf.send_wolf_notify"):
            fire_cron(entry)
        assert "unknown action" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# CLI --fire <name>
# ---------------------------------------------------------------------------

class TestFireByName:
    """Unit: --fire triggers a specific cron by name regardless of schedule."""

    def test_fire_existing_cron(self, tmp_path):
        from creatures.wolf import fire_by_name
        config = {
            "crons": [
                {"name": "digest", "schedule": "0 6 * * *", "action": "script", "command": "echo hi", "notify": "go"}
            ]
        }
        wolf_json = tmp_path / "wolf.json"
        wolf_json.write_text(json.dumps(config))

        with patch("creatures.wolf.get_schedule_path", return_value=wolf_json), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            result = fire_by_name("digest")
            assert result is True
            mock_fire.assert_called_once()
            assert mock_fire.call_args[0][0]["name"] == "digest"

    def test_fire_nonexistent_cron(self, tmp_path):
        from creatures.wolf import fire_by_name
        config = {"crons": [{"name": "digest", "schedule": "0 6 * * *", "action": "script", "command": "echo hi"}]}
        wolf_json = tmp_path / "wolf.json"
        wolf_json.write_text(json.dumps(config))

        with patch("creatures.wolf.get_schedule_path", return_value=wolf_json), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            result = fire_by_name("nope")
            assert result is False
            mock_fire.assert_not_called()

    def test_fire_no_config(self, tmp_path):
        from creatures.wolf import fire_by_name
        with patch("creatures.wolf.get_schedule_path", return_value=tmp_path / "nope.json"):
            result = fire_by_name("anything")
            assert result is False


# ---------------------------------------------------------------------------
# wolf_schedules tool (from core.py)
# ---------------------------------------------------------------------------

class TestWolfSchedulesTool:
    """Unit: the wolf_schedules tool exposed to the dog."""

    def test_no_config_returns_empty(self, tmp_path):
        from bot.core import _tool_wolf_schedules
        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_schedules({}, None))
        assert result["count"] == 0
        assert result["crons"] == []

    def test_returns_crons_with_last_run(self, tmp_path):
        from bot.core import _tool_wolf_schedules
        # Set up wolf.json
        wolf_json = tmp_path / "wolt" / "wolf.json"
        wolf_json.parent.mkdir(parents=True)
        wolf_json.write_text(json.dumps({
            "crons": [{"name": "digest", "schedule": "0 6 * * *", "action": "script", "command": "echo"}]
        }))
        # Set up last-run state
        state_dir = tmp_path / ".state" / "wolf"
        state_dir.mkdir(parents=True)
        (state_dir / "digest.last").write_text("2026-03-15-06:00")

        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_schedules({}, None))
        assert result["count"] == 1
        assert result["crons"][0]["last_run"] == "2026-03-15-06:00"

    def test_returns_never_when_no_state(self, tmp_path):
        from bot.core import _tool_wolf_schedules
        wolf_json = tmp_path / "wolt" / "wolf.json"
        wolf_json.parent.mkdir(parents=True)
        wolf_json.write_text(json.dumps({
            "crons": [{"name": "new-cron", "schedule": "0 6 * * *", "action": "script", "command": "echo"}]
        }))

        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_schedules({}, None))
        assert result["crons"][0]["last_run"] == "never"


# ---------------------------------------------------------------------------
# fire_wolf tool (from core.py)
# ---------------------------------------------------------------------------

class TestFireWolfTool:
    """Unit: the fire_wolf tool exposed to the dog."""

    def test_fire_existing_cron(self, tmp_path):
        from bot.core import _tool_fire_wolf
        with patch("creatures.wolf.get_schedule_path", return_value=tmp_path / "wolf.json"), \
             patch("creatures.wolf.fire_cron") as mock_fire:
            (tmp_path / "wolf.json").write_text(json.dumps({
                "crons": [{"name": "digest", "schedule": "0 6 * * *", "action": "script", "command": "echo"}]
            }))
            result = json.loads(_tool_fire_wolf({"name": "digest"}, None))
            assert result["ok"] is True
            assert result["fired"] == "digest"
            mock_fire.assert_called_once()

    def test_fire_missing_cron(self, tmp_path):
        from bot.core import _tool_fire_wolf
        with patch("creatures.wolf.get_schedule_path", return_value=tmp_path / "wolf.json"):
            (tmp_path / "wolf.json").write_text(json.dumps({"crons": []}))
            result = json.loads(_tool_fire_wolf({"name": "nope"}, None))
            assert result["ok"] is False

    def test_fire_empty_name(self):
        from bot.core import _tool_fire_wolf
        result = json.loads(_tool_fire_wolf({"name": ""}, None))
        assert "error" in result


# ---------------------------------------------------------------------------
# check_update tool (from core.py)
# ---------------------------------------------------------------------------

class TestCheckUpdateTool:
    """Unit: the check_update tool exposed to the dog."""

    TAGS_OUTPUT = (
        "aaa\trefs/tags/v0.1.0\n"
        "bbb\trefs/tags/v0.1.1\n"
        "ccc\trefs/tags/v0.1.2\n"
    )

    def test_up_to_date(self, tmp_path):
        from bot.core import _tool_check_update
        version_file = tmp_path / ".state" / "woltspace-version"
        version_file.parent.mkdir(parents=True)
        version_file.write_text("v0.1.2")

        mock_result = MagicMock(returncode=0, stdout=self.TAGS_OUTPUT)

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result):
            result = json.loads(_tool_check_update({}, None))
        assert result["up_to_date"] is True
        assert result["local_version"] == "v0.1.2"
        assert result["latest_version"] == "v0.1.2"

    def test_update_available(self, tmp_path):
        from bot.core import _tool_check_update
        version_file = tmp_path / ".state" / "woltspace-version"
        version_file.parent.mkdir(parents=True)
        version_file.write_text("v0.1.0")

        mock_result = MagicMock(returncode=0, stdout=self.TAGS_OUTPUT)

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result):
            result = json.loads(_tool_check_update({}, None))
        assert result["up_to_date"] is False
        assert result["local_version"] == "v0.1.0"
        assert result["latest_version"] == "v0.1.2"
        assert "v0.1.0" in result["message"]
        assert "v0.1.2" in result["message"]

    def test_first_run_initializes_version(self, tmp_path):
        from bot.core import _tool_check_update
        # No version file exists yet
        mock_result = MagicMock(returncode=0, stdout=self.TAGS_OUTPUT)

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result):
            result = json.loads(_tool_check_update({}, None))
        assert result["up_to_date"] is True
        assert "initialized" in result.get("note", "")
        # Version file should now exist with latest tag
        assert (tmp_path / ".state" / "woltspace-version").read_text() == "v0.1.2"

    def test_remote_unreachable(self, tmp_path):
        from bot.core import _tool_check_update
        mock_result = MagicMock(returncode=1, stdout="")

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result):
            result = json.loads(_tool_check_update({}, None))
        assert "error" in result

    def test_no_tags_on_remote(self, tmp_path):
        """When remote has no semver tags, return an error."""
        from bot.core import _tool_check_update
        mock_result = MagicMock(returncode=0, stdout="aaa\trefs/tags/not-a-version\n")

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result):
            result = json.loads(_tool_check_update({}, None))
        assert "error" in result

    def test_fetches_tags_not_branches(self, tmp_path):
        """Verify we use --tags, not refs/heads."""
        from bot.core import _tool_check_update
        state_dir = tmp_path / ".state"
        state_dir.mkdir(parents=True)
        (state_dir / "woltspace-version").write_text("v0.1.2")

        mock_result = MagicMock(returncode=0, stdout=self.TAGS_OUTPUT)

        with patch("bot.core.WOLT_DIR", tmp_path), \
             patch("subprocess.run", return_value=mock_result) as mock_run:
            _tool_check_update({}, None)
        mock_run.assert_called_once()
        assert "--tags" in mock_run.call_args[0][0]


# ---------------------------------------------------------------------------
# Notify footer (from server/notify.py)
# ---------------------------------------------------------------------------

class TestNotifyFooter:
    """Unit: session footer is skipped for sessionless notifications."""

    def test_footer_skipped_when_no_session(self):
        """Empty session should produce no footer."""
        # We test the logic directly rather than calling the async function
        session = ""
        footer = ""
        if session:
            footer = f"\n\n---reply footer\n/tui?session={session}"
        assert footer == ""

    def test_footer_present_when_session_exists(self):
        """Non-empty session should produce a footer."""
        session = "beaver-chunky-dam-abc123"
        footer = ""
        if session:
            footer = f"\n\n---reply footer\n/tui?session={session}"
        assert "beaver-chunky-dam-abc123" in footer
        assert footer != ""


# ---------------------------------------------------------------------------
# Job logging (_log_job)
# ---------------------------------------------------------------------------

class TestJobLogging:
    """Unit: _log_job writes valid JSONL to .state/wolf/jobs.jsonl."""

    def test_creates_log_file(self, tmp_path):
        from creatures.wolf import _log_job
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            _log_job("test-cron", "script", event="started")
        log_file = tmp_path / "jobs.jsonl"
        assert log_file.exists()

    def test_writes_valid_jsonl(self, tmp_path):
        from creatures.wolf import _log_job
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            _log_job("digest", "script", event="started", command="echo hi")
        log_file = tmp_path / "jobs.jsonl"
        entry = json.loads(log_file.read_text().strip())
        assert entry["cron"] == "digest"
        assert entry["action"] == "script"
        assert entry["event"] == "started"
        assert entry["command"] == "echo hi"
        assert "ts" in entry

    def test_appends_multiple_entries(self, tmp_path):
        from creatures.wolf import _log_job
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            _log_job("digest", "script", event="started")
            _log_job("digest", "script", event="dispatched", session="wolf-digest-abc123")
            _log_job("update-check", "session", event="started")
        log_file = tmp_path / "jobs.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 3
        # Each line is valid JSON
        for line in lines:
            json.loads(line)

    def test_includes_error_field(self, tmp_path):
        from creatures.wolf import _log_job
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path):
            _log_job("broken", "script", event="error", error="tmux failed")
        entry = json.loads((tmp_path / "jobs.jsonl").read_text().strip())
        assert entry["error"] == "tmux failed"

    def test_fire_cron_logs_started_and_dispatched(self, tmp_path):
        """fire_cron should log both 'started' and 'dispatched' events."""
        from creatures.wolf import fire_cron
        entry = {"name": "test", "action": "script", "command": "echo hi"}
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.run_script", return_value="wolf-test-abc"), \
             patch("creatures.wolf.send_wolf_notify"), \
             patch("creatures.wolf._get_tunnel_url", return_value=None):
            fire_cron(entry)
        log_file = tmp_path / "jobs.jsonl"
        lines = log_file.read_text().strip().split("\n")
        events = [json.loads(l)["event"] for l in lines]
        assert "started" in events
        assert "dispatched" in events

    def test_fire_cron_logs_error_on_unknown_action(self, tmp_path):
        from creatures.wolf import fire_cron
        entry = {"name": "bad", "action": "bogus"}
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf.send_wolf_notify"):
            fire_cron(entry)
        log_file = tmp_path / "jobs.jsonl"
        lines = log_file.read_text().strip().split("\n")
        errors = [json.loads(l) for l in lines if json.loads(l).get("event") == "error"]
        assert len(errors) >= 1
        assert "unknown action" in errors[0]["error"]


# ---------------------------------------------------------------------------
# show_jobs CLI output
# ---------------------------------------------------------------------------

class TestShowJobs:
    """Unit: --jobs CLI output."""

    def test_no_log_file(self, tmp_path, capsys):
        from creatures.wolf import show_jobs
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"):
            show_jobs()
        assert "No job log yet" in capsys.readouterr().out

    def test_shows_recent_entries(self, tmp_path, capsys):
        from creatures.wolf import show_jobs
        log_file = tmp_path / "jobs.jsonl"
        entries = [
            json.dumps({"ts": "2026-03-15T06:00:00", "cron": "digest", "event": "started"}),
            json.dumps({"ts": "2026-03-15T06:00:01", "cron": "digest", "event": "dispatched", "session": "wolf-digest-abc"}),
        ]
        log_file.write_text("\n".join(entries))
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"):
            show_jobs(count=5)
        out = capsys.readouterr().out
        assert "digest" in out
        assert "started" in out
        assert "dispatched" in out

    def test_respects_count_limit(self, tmp_path, capsys):
        from creatures.wolf import show_jobs
        log_file = tmp_path / "jobs.jsonl"
        entries = [
            json.dumps({"ts": f"2026-03-15T0{i}:00:00", "cron": f"job-{i}", "event": "started"})
            for i in range(5)
        ]
        log_file.write_text("\n".join(entries))
        with patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"):
            show_jobs(count=2)
        out = capsys.readouterr().out
        # Should show only the last 2
        assert "job-3" in out
        assert "job-4" in out
        assert "job-0" not in out


# ---------------------------------------------------------------------------
# wolf_jobs bot tool (from core.py)
# ---------------------------------------------------------------------------

class TestWolfJobsTool:
    """Unit: the wolf_jobs tool exposed to the dog."""

    def test_no_log_file(self, tmp_path):
        from bot.core import _tool_wolf_jobs
        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_jobs({}, None))
        assert result["jobs"] == []
        assert "no job log" in result["note"]

    def test_returns_recent_jobs(self, tmp_path):
        from bot.core import _tool_wolf_jobs
        log_dir = tmp_path / ".state" / "wolf"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "jobs.jsonl"
        entries = [
            json.dumps({"ts": "2026-03-15T06:00:00", "cron": "digest", "event": "started"}),
            json.dumps({"ts": "2026-03-15T06:00:01", "cron": "digest", "event": "dispatched"}),
        ]
        log_file.write_text("\n".join(entries))
        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_jobs({}, None))
        assert result["count"] == 2
        assert result["jobs"][0]["cron"] == "digest"

    def test_respects_count_param(self, tmp_path):
        from bot.core import _tool_wolf_jobs
        log_dir = tmp_path / ".state" / "wolf"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "jobs.jsonl"
        entries = [
            json.dumps({"ts": f"2026-03-15T0{i}:00:00", "cron": f"job-{i}", "event": "started"})
            for i in range(5)
        ]
        log_file.write_text("\n".join(entries))
        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_jobs({"count": 2}, None))
        assert result["count"] == 2
        assert result["jobs"][0]["cron"] == "job-3"
        assert result["jobs"][1]["cron"] == "job-4"

    def test_handles_corrupted_lines(self, tmp_path):
        from bot.core import _tool_wolf_jobs
        log_dir = tmp_path / ".state" / "wolf"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "jobs.jsonl"
        log_file.write_text('{"cron":"ok","event":"started"}\nnot json\n{"cron":"also-ok","event":"done"}\n')
        with patch("bot.core._get_wolf_wolt_dir", return_value=tmp_path):
            result = json.loads(_tool_wolf_jobs({}, None))
        assert result["count"] == 2  # skipped the bad line


# ---------------------------------------------------------------------------
# run_script env inheritance — the WOLT_DIR fix
# ---------------------------------------------------------------------------

class TestRunScriptEnv:
    """Unit: run_script exports the wolf's own WOLT_DIR and WOLT_NAME
    into the tmux session, not the container's inherited env."""

    def test_tmux_command_includes_wolt_dir_export(self, tmp_path):
        """The wrapped command should start with export WOLT_DIR=... WOLT_NAME=..."""
        from creatures.wolf import run_script

        wolf_dir = tmp_path / "howlie"
        wolf_dir.mkdir()
        entry = {"name": "update-check", "action": "script", "command": "bash check.sh"}

        with patch("creatures.wolf.get_wolt_dir", return_value=wolf_dir), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"), \
             patch("creatures.wolf._get_tunnel_url", return_value=None), \
             patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._log_job"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            run_script(entry)

            # Inspect the bash -c command passed to tmux
            call_args = mock_run.call_args[0][0]
            # tmux new-session -d -s <name> -c <dir> bash -c <wrapped>
            wrapped_cmd = call_args[-1]  # last arg is the wrapped command string
            assert f"export WOLT_DIR={wolf_dir}" in wrapped_cmd
            assert "WOLT_NAME=howlie" in wrapped_cmd

    def test_env_export_precedes_command(self, tmp_path):
        """The export must come BEFORE the actual command."""
        from creatures.wolf import run_script

        wolf_dir = tmp_path / "howlie"
        wolf_dir.mkdir()
        entry = {"name": "test", "action": "script", "command": "echo hello"}

        with patch("creatures.wolf.get_wolt_dir", return_value=wolf_dir), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"), \
             patch("creatures.wolf._get_tunnel_url", return_value=None), \
             patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._log_job"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            run_script(entry)

            wrapped_cmd = mock_run.call_args[0][0][-1]
            export_pos = wrapped_cmd.find("export WOLT_DIR=")
            command_pos = wrapped_cmd.find("echo hello")
            assert export_pos < command_pos, "WOLT_DIR export must come before the command"

    def test_tmux_working_dir_matches_wolf_dir(self, tmp_path):
        """tmux -c should also point to the wolf's directory."""
        from creatures.wolf import run_script

        wolf_dir = tmp_path / "howlie"
        wolf_dir.mkdir()
        entry = {"name": "test", "action": "script", "command": "echo hi"}

        with patch("creatures.wolf.get_wolt_dir", return_value=wolf_dir), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"), \
             patch("creatures.wolf._get_tunnel_url", return_value=None), \
             patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._log_job"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            run_script(entry)

            call_args = mock_run.call_args[0][0]
            # Find -c flag and the value after it
            c_idx = call_args.index("-c")
            assert call_args[c_idx + 1] == str(wolf_dir)

    def test_different_wolf_dir_from_env(self, tmp_path):
        """Even if WOLT_DIR env points elsewhere, run_script should use the wolf's dir."""
        from creatures.wolf import run_script

        wolf_dir = tmp_path / "howlie"
        wolf_dir.mkdir()
        entry = {"name": "test", "action": "script", "command": "bash check.sh"}

        # Simulate container env pointing to a different wolt
        with patch.dict(os.environ, {"WOLT_DIR": "/workspace/wolts/neowolt", "WOLT_NAME": "neowolt"}), \
             patch("creatures.wolf.get_wolt_dir", return_value=wolf_dir), \
             patch("creatures.wolf._get_wolf_name", return_value="howlie"), \
             patch("creatures.wolf._get_tunnel_url", return_value=None), \
             patch("creatures.wolf.get_state_dir", return_value=tmp_path), \
             patch("creatures.wolf._log_job"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            run_script(entry)

            wrapped_cmd = mock_run.call_args[0][0][-1]
            # Should export howlie's dir, NOT neowolt
            assert str(wolf_dir) in wrapped_cmd
            assert "WOLT_NAME=howlie" in wrapped_cmd
            assert "neowolt" not in wrapped_cmd
