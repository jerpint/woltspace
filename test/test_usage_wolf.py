"""The wolf's built-in nightly usage save: 23:55 local, catch-up once, journaled, no session."""

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

from usage_support import claude, emit, no_host, request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
import usage_collector  # noqa: E402
import wolfcore  # noqa: E402
from creatures import wolf  # noqa: E402


def local(*args):
    return datetime(*args).astimezone()


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    root = tmp_path / "wolts"
    emit(claude(tmp_path), request("one"))
    monkeypatch.setattr(wolf, "WOLTS_DIR", root)
    monkeypatch.setattr(usage_collector, "host_bases", lambda: no_host(tmp_path))
    monkeypatch.setattr(wolf, "dispatch_session", lambda entry: pytest.fail("the save started a session"))
    return root


def journal(root):
    return [(f["event"], f["owner"]) for f in wolfcore.read_journal(root, cron="usage-save")]


def test_saves_at_2355_local_once(lodge):
    history = usage_collector.history_path(lodge)
    assert wolf.save_usage(local(2026, 11, 24, 23, 54)) is False and not history.exists()
    assert wolf.save_usage(local(2026, 11, 24, 23, 55)) is True
    assert json.loads(history.read_text())["rows"]
    assert wolf.save_usage(local(2026, 11, 24, 23, 55)) is False  # the same slot, again
    assert journal(lodge) == [("saved", "_lodge")]


def test_a_missed_save_is_caught_up_once_at_start(lodge):
    wolfcore.write_last_run(lodge, "_lodge", "usage-save", local(2026, 11, 23, 23, 55))
    morning = local(2026, 11, 25, 8, 0)
    assert wolf.save_usage(morning) is False  # the running loop only looks at this minute
    assert wolf.save_usage(morning, catching_up=True) is True
    assert wolfcore.read_last_run(lodge, "_lodge", "usage-save") == "2026-11-24-23:55"
    assert wolf.save_usage(morning, catching_up=True) is False
    assert journal(lodge) == [("catch-up", "_lodge")]


def test_a_failed_save_is_journaled_by_type_and_not_retried_every_tick(lodge, capsys):
    history = usage_collector.history_path(lodge)
    emit(history, {"schema_version": 1, "rows": [  # days, but no recorded zone: refused, not guessed
        {"day": "2026-11-24", "provider": "claude", "wolt": "alpha", "session_id": "s",
         "model": "m", "metric": "output", "amount": 1}]})
    before = history.read_bytes()
    assert wolf.save_usage(local(2026, 11, 24, 23, 55)) is False
    assert wolf.save_usage(local(2026, 11, 24, 23, 55)) is False
    assert history.read_bytes() == before
    [entry] = wolfcore.read_journal(lodge, cron="usage-save")
    assert (entry["event"], entry["error"]) == ("failed", "HistoryZoneMismatch")
    assert "no recorded zone" not in capsys.readouterr().err
