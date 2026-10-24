"""The wolf's stamps and journal are rows of the lodge DB's `wolf_runs`, and the
old `.space/wolf/` files come in once through `import_wolf`."""

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))

import lodge_db  # noqa: E402
import wolfcore  # noqa: E402

DIGEST = {"name": "digest", "schedule": "0 6 * * *", "prompt": "p", "_owner": "pal"}


def _wolf_json(wolts, wolt, *names):
    path = wolts / wolt / "wolt" / "wolf.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"crons": [
        {"name": n, "schedule": "0 6 * * *", "prompt": "p"} for n in names]}))


def test_last_run_is_the_newest_fired_row(tmp_path):
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") is None
    wolfcore.write_last_run(tmp_path, "pal", "digest", datetime(2026, 3, 14, 6, 0))
    wolfcore.write_last_run(tmp_path, "pal", "digest", datetime(2026, 3, 15, 6, 0))
    wolfcore.append_journal(tmp_path, "digest", "manual", owner="pal", session="s-1")
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") == "2026-03-15-06:00"
    assert wolfcore.read_last_run(tmp_path, "nunu", "digest") is None


def test_reading_a_lodge_without_a_db_creates_nothing(tmp_path):
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") is None
    assert wolfcore.read_journal(tmp_path) == []
    assert not lodge_db.path(tmp_path).exists()


def test_the_journal_reads_back_as_the_old_lines_newest_first(tmp_path):
    wolfcore.write_last_run(tmp_path, "pal", "digest", datetime(2026, 3, 15, 6, 0))
    wolfcore.append_journal(tmp_path, "digest", "started", owner="pal")
    wolfcore.append_journal(tmp_path, "digest", "dispatched", owner="pal", link=None)
    wolfcore.append_journal(tmp_path, "tidy", "manual", owner="nunu", session="s-1",
                            link="http://x/tui?session=s-1")

    fires = wolfcore.read_journal(tmp_path)

    assert [f["event"] for f in fires] == ["manual", "dispatched", "started"]  # no stamp row
    assert list(fires[0]) == ["ts", "cron", "action", "event", "owner", "session", "link"]
    assert fires[0]["action"] == "session" and fires[0]["session"] == "s-1"
    assert "session" not in fires[1] and fires[1]["link"] is None
    datetime.fromisoformat(fires[0]["ts"])
    assert [f["event"] for f in wolfcore.read_journal(tmp_path, cron="digest", limit=1)] == ["dispatched"]
    assert [f["cron"] for f in wolfcore.read_journal(tmp_path, wolt="nunu")] == ["tidy"]


def test_a_moved_cron_takes_its_last_run_and_a_forgotten_one_drops_it(tmp_path):
    wolfcore.write_last_run(tmp_path, "pal", "digest", datetime(2026, 3, 15, 6, 0))
    wolfcore.move_last_run(tmp_path, "pal", "digest", "nunu", "digest")
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") is None
    assert wolfcore.read_last_run(tmp_path, "nunu", "digest") == "2026-03-15-06:00"
    wolfcore.forget_last_run(tmp_path, "nunu", "digest")
    assert wolfcore.read_last_run(tmp_path, "nunu", "digest") is None


def test_forgetting_a_wolt_drops_every_stamp_and_keeps_the_journal(tmp_path):
    for name in ("digest", "tidy"):
        wolfcore.write_last_run(tmp_path, "pal", name, datetime(2026, 3, 15, 6, 0))
    wolfcore.append_journal(tmp_path, "digest", "started", owner="pal")
    wolfcore.forget_last_run(tmp_path, "pal")
    assert wolfcore.read_last_run(tmp_path, "pal", "tidy") is None
    assert len(wolfcore.read_journal(tmp_path)) == 1


def test_a_catch_up_after_a_restart_fires_exactly_once(tmp_path):
    """The wolf was down at 06:00. Restart at 09:00 catches up once; a second
    restart and the loop's own tick at 09:00 find the minute already fired."""
    from creatures import wolf

    now = datetime(2026, 3, 15, 9, 0).astimezone()
    with patch("creatures.wolf.WOLTS_DIR", tmp_path), \
         patch("creatures.wolf.fire_cron") as fire:
        wolf.catch_up([DIGEST], now)
        wolf.catch_up([DIGEST], now)
        wolf.check_and_fire([DIGEST], now)
    assert fire.call_count == 1
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") == "2026-03-15-06:00"
    [line] = wolfcore.read_journal(tmp_path)
    assert line["event"] == "catch-up" and line["missed"] == "2026-03-15-06:00"


def test_an_imported_stamp_stops_a_second_catch_up(tmp_path):
    """Upgrade day: the old lodge stamped 06:00 in a file, the new one must not refire."""
    stamp = tmp_path / ".space" / "wolf" / "pal" / "digest.last"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("2026-03-15-06:00")
    with lodge_db.write(tmp_path) as conn:
        wolfcore.import_wolf(conn, tmp_path)

    from creatures import wolf
    with patch("creatures.wolf.WOLTS_DIR", tmp_path), \
         patch("creatures.wolf.fire_cron") as fire:
        wolf.catch_up([DIGEST], datetime(2026, 3, 15, 9, 0).astimezone())
    fire.assert_not_called()


# --- import ---------------------------------------------------------------

def test_import_takes_stamps_legacy_stamps_and_the_journal(tmp_path, capsys):
    state = tmp_path / ".space" / "wolf"
    (state / "pal").mkdir(parents=True)
    (state / "pal" / "digest.last").write_text("2026-03-15-06:00\n")
    (state / "pal" / "wolf.json.lock").write_text("")
    _wolf_json(tmp_path, "pal", "tidy")
    _wolf_json(tmp_path, "nunu", "tidy")
    (state / "tidy.last").write_text("2026-03-14-06:00")          # before per-wolt stamps
    (state / "orphan.last").write_text("2026-03-14-06:00")        # no cron owns it
    lines = [
        {"ts": "2026-03-15T06:00:01.000001", "cron": "digest", "action": "session",
         "event": "started", "owner": "pal"},
        {"ts": "2026-03-15T06:00:02.000002", "cron": "digest", "action": "session",
         "event": "dispatched", "owner": "pal", "link": "http://x/tui?session=s-1"},
    ]
    (state / "jobs.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines) + '{"ts": "torn')

    with lodge_db.write(tmp_path) as conn:
        counts, retire = wolfcore.import_wolf(conn, tmp_path)

    assert counts == {"stamps": 3, "journal": 2, "skipped": 0}
    assert sorted(retire) == sorted([state / "pal" / "digest.last", state / "tidy.last",
                                     state / "jobs.jsonl"])
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") == "2026-03-15-06:00"
    assert wolfcore.read_last_run(tmp_path, "pal", "tidy") == "2026-03-14-06:00"
    assert wolfcore.read_last_run(tmp_path, "nunu", "tidy") == "2026-03-14-06:00"
    assert wolfcore.read_journal(tmp_path) == lines[::-1]
    assert "torn" in capsys.readouterr().err


def test_an_imported_stamp_keeps_its_file_time(tmp_path):
    stamp = tmp_path / ".space" / "wolf" / "pal" / "digest.last"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("2026-03-15-06:00")
    os.utime(stamp, (1_000_000, 1_000_000))
    with lodge_db.write(tmp_path) as conn:
        wolfcore.import_wolf(conn, tmp_path)
    wolfcore.write_last_run(tmp_path, "pal", "digest", datetime(2026, 3, 16, 6, 0))
    assert wolfcore.read_last_run(tmp_path, "pal", "digest") == "2026-03-16-06:00"


def test_import_of_a_lodge_whose_wolf_never_ran_is_empty(tmp_path):
    with lodge_db.write(tmp_path) as conn:
        assert wolfcore.import_wolf(conn, tmp_path) == ({"stamps": 0, "journal": 0, "skipped": 0}, [])


def test_a_journal_read_twice_is_imported_once(tmp_path):
    """A crash between commit and retire leaves the file: the next boot skips its lines."""
    state = tmp_path / ".space" / "wolf"
    state.mkdir(parents=True)
    (state / "jobs.jsonl").write_text(json.dumps(
        {"ts": "2026-03-15T06:00:01", "cron": "digest", "action": "session",
         "event": "started", "owner": "pal"}) + "\n")
    counts = []
    for _ in range(2):
        with lodge_db.write(tmp_path) as conn:
            counts.append(wolfcore.import_wolf(conn, tmp_path)[0])
    assert [(c["journal"], c["skipped"]) for c in counts] == [(1, 0), (0, 1)]
    assert len(wolfcore.read_journal(tmp_path)) == 1


def test_run_imports_retires_stamps_and_journal_and_keeps_the_wolf_json_lock(tmp_path):
    state = tmp_path / ".space" / "wolf"
    (state / "pal").mkdir(parents=True)
    (state / "pal" / "digest.last").write_text("2026-03-15-06:00")
    (state / "pal" / "wolf.json.lock").write_text("")
    (state / "jobs.jsonl").write_text("")
    lodge_db.run_imports(tmp_path, {"wolf": wolfcore.import_wolf})
    assert sorted(p.name for p in (state / "pal").iterdir()) == ["digest.last.imported",
                                                                 "wolf.json.lock"]
    assert (state / "jobs.jsonl.imported").exists()


def test_after_the_boot_import_an_upgraded_lodge_does_not_refire(tmp_path):
    """Upgrade through the real boot entry point: the stamp an old wolf wrote at
    06:00 is in the DB before the wolf starts, so its catch-up at 09:00 is quiet."""
    from creatures import wolf
    from woltspace.adoption import adopt_runtime_sessions
    from woltspace.layout import RuntimeLayout

    wolts = tmp_path / "wolts"
    _wolf_json(wolts, "pal", "digest")
    stamp = wolts / ".space" / "wolf" / "pal" / "digest.last"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("2026-03-15-06:00")

    adopt_runtime_sessions(RuntimeLayout(wolts_dir=wolts, install_root=ROOT))

    with patch("creatures.wolf.WOLTS_DIR", wolts), \
         patch("creatures.wolf.fire_cron") as fire:
        wolf.catch_up(wolf.load_schedule(), datetime(2026, 3, 15, 9, 0).astimezone())
    fire.assert_not_called()
    assert (stamp.parent / "digest.last.imported").exists()


def test_a_failed_import_skips_the_start_up_catch_up(tmp_path, capsys):
    """Old stamps still un-retired mean the import failed: the DB has no stamps,
    so a catch-up would re-fire every cron of the last 24 h. Skip it once."""
    from creatures import wolf

    state = tmp_path / ".space" / "wolf"
    (state / "pal").mkdir(parents=True)
    (state / "jobs.jsonl").write_text("")
    now = datetime(2026, 3, 15, 9, 0).astimezone()
    with patch("creatures.wolf.WOLTS_DIR", tmp_path), \
         patch("creatures.wolf.fire_cron") as fire:
        wolf.catch_up([DIGEST], now)
        fire.assert_not_called()
        (state / "jobs.jsonl").rename(state / "pal" / "digest.last")
        wolf.catch_up([DIGEST], now)
        fire.assert_not_called()
        (state / "pal" / "digest.last").rename(state / "pal" / "digest.last.imported")
        wolf.catch_up([DIGEST], now)
    assert fire.call_count == 1
    assert "not imported" in capsys.readouterr().err
