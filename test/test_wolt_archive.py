"""Archive a wolt: it leaves the lodge, its sessions stop, and nothing is lost.

Archiving moves wolts/<name> to wolts/.archive/<name>, which every wolt scan
already skips. These tests run on tmp_path with a fake runtime: no real tmux.
"""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import session_runtime  # noqa: E402
import wolfcore  # noqa: E402
from sessions import SessionRegistry  # noqa: E402
from wolt_archive import ArchiveError, archive_wolt, list_archived_wolts  # noqa: E402

from conftest import FakeSessionRuntime  # noqa: E402

RUNNING = "pal-maple-a1b2c3"
NO_ID = "pal-cedar-778899"
RESTING = "pal-birch-d4e5f6"
HOLD = {"until": None, "reason": "npm run dev", "since": 1}


class LiveRuntime(FakeSessionRuntime):
    """A tmux session is alive while its exact name is in `live`."""

    def __init__(self, live=()):
        super().__init__()
        self.live = set(live)

    def is_alive(self, handle) -> bool:
        return handle.tmux_session_name in self.live

    def list_session_names(self, include_main: bool = False) -> set[str]:
        return set(self.live)

    def stop(self, handle) -> bool:
        self.live.discard(handle.tmux_session_name)
        return super().stop(handle)


@pytest.fixture
def runtime():
    fake = LiveRuntime()
    session_runtime.set_runtime(fake)
    yield fake
    session_runtime.set_runtime(None)


def _wolt(wolts_dir, name, **config):
    wolt_json = wolts_dir / name / "wolt" / "wolt.json"
    wolt_json.parent.mkdir(parents=True)
    wolt_json.write_text(json.dumps({"name": name, "type": "beaver", **config}))
    return wolts_dir / name


def _session(wolts_dir, wolt, name, *, status, runtime=None, **fields):
    registry = SessionRegistry(wolts_dir)
    registry.create(name, wolt=wolt)
    data = registry._read(wolt, name)
    data.update({"status": status, **fields})
    registry._write(wolt, name, data)
    if runtime is not None and status == "running":
        runtime.live.add(name)


def _record(path):
    return json.loads(path.read_text())


def test_archive_moves_folder_and_stops_running_sessions(tmp_path, runtime):
    home = _wolt(tmp_path, "pal")
    (home / "wolt" / "wolf.json").write_text(json.dumps(
        {"crons": [{"name": "digest", "schedule": "0 9 * * *", "prompt": "hi"}]}))
    _session(tmp_path, "pal", RUNNING, status="running", runtime=runtime, harness_session_id="abc")
    _session(tmp_path, "pal", NO_ID, status="running", runtime=runtime)
    _session(tmp_path, "pal", RESTING, status="resting", harness_session_id="def")
    assert any(c["_owner"] == "pal" for c in wolfcore.load_all(tmp_path)[0])

    result = archive_wolt("pal", wolts_dir=tmp_path, now=5000)

    assert result == {"archived": "pal", "stopped": sorted([RUNNING, NO_ID])}
    assert not (tmp_path / "pal").exists()
    archived = tmp_path / ".archive" / "pal"
    assert (archived / "wolt" / "wolt.json").is_file()
    assert sorted(runtime.stops) == sorted([RUNNING, NO_ID])
    sessions = archived / ".state" / "sessions"
    rested = _record(sessions / f"{RUNNING}.json")
    assert (rested["status"], rested["rested_at"], rested["rest_reason"]) == ("resting", 5000, "archived")
    assert rested["harness_session_id"] == "abc"
    stopped = _record(sessions / f"{NO_ID}.json")
    assert (stopped["status"], stopped["finished_at"]) == ("stopped", 5000)
    assert _record(sessions / f"{RESTING}.json")["status"] == "resting"
    assert "rest_reason" not in _record(sessions / f"{RESTING}.json")
    assert all(c["_owner"] != "pal" for c in wolfcore.load_all(tmp_path)[0])


def test_archive_recovers_a_missing_conversation_id_and_rests(tmp_path, runtime, monkeypatch):
    import sessions

    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", NO_ID, status="running", runtime=runtime)
    monkeypatch.setattr(sessions, "recover_resume_id",
                        lambda data, taken: "found-1" if data["name"] == NO_ID else "")

    archive_wolt("pal", wolts_dir=tmp_path)

    record = _record(tmp_path / ".archive" / "pal" / ".state" / "sessions" / f"{NO_ID}.json")
    assert (record["status"], record["harness_session_id"]) == ("resting", "found-1")


def test_archive_refuses_while_a_session_holds(tmp_path, runtime):
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status="running", runtime=runtime, hold=dict(HOLD))

    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert e.value.extra["held"] == [{"session": RUNNING, "reason": "npm run dev"}]
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()
    assert not (tmp_path / ".archive").exists()
    assert runtime.stops == []


def test_a_session_that_will_not_stop_blocks_the_move(tmp_path, runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status="running", runtime=runtime, harness_session_id="abc")
    monkeypatch.setattr(runtime, "stop", lambda handle: False)

    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 500 and RUNNING in e.value.message
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()
    assert _record(tmp_path / "pal" / ".state" / "sessions" / f"{RUNNING}.json")["status"] == "running"


def test_a_hold_taken_during_the_archive_refuses_it(tmp_path, runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status="running", runtime=runtime, harness_session_id="abc")
    _session(tmp_path, "pal", NO_ID, status="running", runtime=runtime, harness_session_id="def")
    real_stop, held = runtime.stop, []

    def hold_the_other(handle):
        # The hold route writes between the hold check and this session's turn.
        if not held:
            other = NO_ID if handle.tmux_session_name == RUNNING else RUNNING
            registry = SessionRegistry(tmp_path)
            data = registry._read("pal", other)
            registry._write("pal", other, {**data, "hold": dict(HOLD)})
            held.append(other)
        return real_stop(handle)

    monkeypatch.setattr(runtime, "stop", hold_the_other)
    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert e.value.extra["held"] == [{"session": held[0], "reason": "npm run dev"}]
    assert held[0] in runtime.live
    record = _record(tmp_path / "pal" / ".state" / "sessions" / f"{held[0]}.json")
    assert (record["status"], record["hold"]["reason"]) == ("running", "npm run dev")
    assert not (tmp_path / ".archive" / "pal").exists()


def _revive_in_the_gap(tmp_path, runtime, monkeypatch, wolt, session):
    """Bring a session back between the stop pass and the move."""
    import wolt_archive

    real_rename = wolt_archive.os.rename

    def revive_then_rename(src, dst):
        # os.rename is global: the registry's own writes come through here too.
        if Path(src) == tmp_path / wolt:
            SessionRegistry(tmp_path).update(session, wolt=wolt, status="running")
            runtime.live.add(session)
        real_rename(src, dst)

    monkeypatch.setattr(wolt_archive.os, "rename", revive_then_rename)


def test_a_sweep_that_cannot_stop_a_session_says_the_wolt_was_archived(tmp_path, runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status="resting", harness_session_id="abc")
    _revive_in_the_gap(tmp_path, runtime, monkeypatch, "pal", RUNNING)
    monkeypatch.setattr(runtime, "stop", lambda handle: False)

    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 500
    assert e.value.message == f"archived pal, but session {RUNNING} could not be stopped"
    assert (tmp_path / ".archive" / "pal" / "wolt" / "wolt.json").is_file()


def test_the_sweep_stops_a_session_without_an_id_and_recovers_none(tmp_path, runtime, monkeypatch):
    import sessions

    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", NO_ID, status="stopped")
    _revive_in_the_gap(tmp_path, runtime, monkeypatch, "pal", NO_ID)
    monkeypatch.setattr(sessions, "recover_resume_id", lambda data, taken: "found-1")

    assert archive_wolt("pal", wolts_dir=tmp_path)["stopped"] == [NO_ID]

    record = _record(tmp_path / ".archive" / "pal" / ".state" / "sessions" / f"{NO_ID}.json")
    assert record["status"] == "stopped" and "harness_session_id" not in record
    assert not (tmp_path / ".archive" / ".state").exists()


def test_archive_refuses_when_already_in_archive(tmp_path, runtime):
    _wolt(tmp_path, "pal")
    (tmp_path / ".archive" / "pal").mkdir(parents=True)
    (tmp_path / ".archive" / "pal" / "keep.txt").write_text("older pal")

    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()
    assert (tmp_path / ".archive" / "pal" / "keep.txt").read_text() == "older pal"


def test_sweep_after_move_kills_only_exact_sessions(tmp_path, runtime, monkeypatch):
    ledger, two = "ledger-oak-111111", "ledger-two-elm-222222"
    _wolt(tmp_path, "ledger")
    _wolt(tmp_path, "ledger-two")
    _session(tmp_path, "ledger", ledger, status="running", runtime=runtime, harness_session_id="l-1")
    _session(tmp_path, "ledger-two", two, status="running", runtime=runtime, harness_session_id="t-1")
    _revive_in_the_gap(tmp_path, runtime, monkeypatch, "ledger", ledger)

    archive_wolt("ledger", wolts_dir=tmp_path)

    assert runtime.stops == [ledger, ledger]
    assert runtime.live == {two}
    sessions = tmp_path / ".archive" / "ledger" / ".state" / "sessions"
    record = _record(sessions / f"{ledger}.json")
    assert (record["status"], record["rest_reason"]) == ("resting", "archived")
    assert _record(tmp_path / "ledger-two" / ".state" / "sessions" / f"{two}.json")["status"] == "running"


def test_unknown_or_bad_names_create_nothing(tmp_path, runtime):
    for bad in ["ledgr", "../x", ".archive", "a/b", "", "-x", "a" * 65]:
        with pytest.raises(ArchiveError) as e:
            archive_wolt(bad, wolts_dir=tmp_path)
        assert e.value.status in (400, 404), bad
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_old_names_with_capitals_and_underscores_are_valid(tmp_path, runtime):
    _wolt(tmp_path, "Old_Pal")
    assert archive_wolt("Old_Pal", wolts_dir=tmp_path)["archived"] == "Old_Pal"


def test_list_archived_shows_shown_name(tmp_path, runtime):
    _wolt(tmp_path, "pal", display_name="Pal The Builder")
    _wolt(tmp_path, "bob", type="raccoon")
    archive_wolt("pal", wolts_dir=tmp_path)

    assert list_archived_wolts(wolts_dir=tmp_path) == [
        {"name": "pal", "display_name": "Pal The Builder", "type": "beaver"}]


def test_list_archived_is_empty_without_an_archive(tmp_path):
    assert list_archived_wolts(wolts_dir=tmp_path) == []
    assert not (tmp_path / ".archive").exists()
