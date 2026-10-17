"""Archive a wolt: it leaves the lodge, its sessions stop, and nothing is lost.

Archiving moves wolts/<name> to wolts/.archive/<name>, which every wolt scan
already skips. These tests run on tmp_path with a fake runtime: no real tmux.
"""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import session_runtime  # noqa: E402
import wolfcore  # noqa: E402
from execution_policy import AutoGrantStore  # noqa: E402
from session_targets import SessionTarget  # noqa: E402
from sessions import SessionRegistry  # noqa: E402
from wolt_archive import (  # noqa: E402
    ArchiveError, archive_wolt, delete_archived_wolt, list_archived_wolts, unarchive_wolt)

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


# --- unarchive ---------------------------------------------------------------

def _archived(tmp_path, runtime, name="pal"):
    home = _wolt(tmp_path, name)
    (home / "wolt" / "wolf.json").write_text(json.dumps(
        {"crons": [{"name": "digest", "schedule": "0 9 * * *", "prompt": "hi"}]}))
    _session(tmp_path, name, RESTING, status="resting", harness_session_id="abc")
    archive_wolt(name, wolts_dir=tmp_path)
    return tmp_path / ".archive" / name


def test_unarchive_brings_folder_records_and_crons_back(tmp_path, runtime):
    _archived(tmp_path, runtime)
    assert all(c["_owner"] != "pal" for c in wolfcore.load_all(tmp_path)[0])

    assert unarchive_wolt("pal", wolts_dir=tmp_path) == {"unarchived": "pal"}

    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()
    assert not (tmp_path / ".archive" / "pal").exists()
    record = SessionRegistry(tmp_path).get(RESTING, check_alive=False)
    assert (record["status"], record["harness_session_id"]) == ("resting", "abc")
    assert any(c["_owner"] == "pal" for c in wolfcore.load_all(tmp_path)[0])


def test_unarchive_refuses_when_the_name_is_taken(tmp_path, runtime):
    archived = _archived(tmp_path, runtime)
    _wolt(tmp_path, "pal", display_name="A new pal")

    with pytest.raises(ArchiveError) as e:
        unarchive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert json.loads((tmp_path / "pal" / "wolt" / "wolt.json").read_text())["display_name"] == "A new pal"
    assert (archived / "wolt" / "wolt.json").is_file()


def test_unarchive_clears_an_empty_ghost_folder(tmp_path, runtime):
    _archived(tmp_path, runtime)
    # A message racing the move: the session lock leaves an empty <s>.lock.
    (tmp_path / "pal" / ".state" / "sessions").mkdir(parents=True)
    (tmp_path / "pal" / ".state" / "sessions" / f"{RESTING}.lock").touch()
    (tmp_path / "pal" / "wolt").mkdir()

    assert unarchive_wolt("pal", wolts_dir=tmp_path) == {"unarchived": "pal"}
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()
    assert SessionRegistry(tmp_path).get(RESTING, check_alive=False)["status"] == "resting"


@pytest.mark.parametrize("leftover", [
    "notes.txt", ".state/sessions/x.json", ".state/sessions/x.lock", "link"])
def test_unarchive_refuses_a_ghost_with_a_file(tmp_path, runtime, leftover):
    archived = _archived(tmp_path, runtime)
    (tmp_path / "pal" / "a" / "empty").mkdir(parents=True)
    path = tmp_path / "pal" / leftover
    path.parent.mkdir(parents=True, exist_ok=True)
    if leftover == "link":
        (tmp_path / "elsewhere").mkdir()
        path.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    else:
        path.write_text("keep me")

    with pytest.raises(ArchiveError) as e:
        unarchive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert path.is_symlink() or path.read_text() == "keep me"
    assert (tmp_path / "pal" / "a" / "empty").is_dir()  # a refusal removes nothing
    assert (archived / "wolt" / "wolt.json").is_file()


def test_unarchive_unknown_name_404(tmp_path, runtime):
    _wolt(tmp_path, "pal")
    for name in ["pal", "ghost"]:
        with pytest.raises(ArchiveError) as e:
            unarchive_wolt(name, wolts_dir=tmp_path)
        assert e.value.status == 404
    with pytest.raises(ArchiveError) as e:
        unarchive_wolt("../pal", wolts_dir=tmp_path)
    assert e.value.status == 400
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pal"]


# --- delete ------------------------------------------------------------------

def _grant(tmp_path, wolt, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    AutoGrantStore(tmp_path).grant(SessionTarget(wolt, workdir.resolve()))


def test_delete_only_from_the_archive_and_revokes_grants(tmp_path, runtime):
    _wolt(tmp_path, "pal")
    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)
    assert e.value.status == 404
    assert (tmp_path / "pal" / "wolt" / "wolt.json").is_file()

    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    _grant(tmp_path, "pal", tmp_path / "b")
    _grant(tmp_path, "other", tmp_path / "a")
    stamps = tmp_path / ".space" / "wolf" / "pal"
    stamps.mkdir(parents=True)
    (stamps / "digest.last").write_text("2026-10-17-09:00")
    (tmp_path / ".space" / "wolf" / "other").mkdir()

    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 2}

    assert not (tmp_path / ".archive" / "pal").exists()
    assert [g.wolt_id for g in AutoGrantStore(tmp_path).list()] == ["other"]
    assert not stamps.exists()
    assert (tmp_path / ".space" / "wolf" / "other").is_dir()


def test_delete_removes_read_only_folders(tmp_path, runtime):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    pkg = tmp_path / ".archive" / "pal" / "gomod" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "mod.go").write_text("package mod")
    (pkg / "mod.go").chmod(0o444)
    pkg.chmod(0o555)

    assert delete_archived_wolt("pal", wolts_dir=tmp_path)["deleted"] == "pal"
    assert not (tmp_path / ".archive" / "pal").exists()


@pytest.mark.parametrize("mode", [0o000, 0o300])
def test_delete_removes_folders_it_cannot_read(tmp_path, runtime, mode):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    locked = tmp_path / ".archive" / "pal" / "fixtures" / "locked"
    locked.mkdir(parents=True)
    (locked / "secret.txt").write_text("x")
    locked.chmod(mode)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    (tmp_path / ".archive" / "pal" / "link").symlink_to(outside, target_is_directory=True)

    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 1}
    assert not (tmp_path / ".archive" / "pal").exists()
    assert AutoGrantStore(tmp_path).list() == []
    assert outside.stat().st_mode & 0o777 == 0o500  # never chmodded through the link


def test_a_folder_that_stays_unreadable_ends_as_a_500(tmp_path, runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    locked = tmp_path / ".archive" / "pal" / "locked"
    locked.mkdir()
    (locked / "secret.txt").write_text("x")
    locked.chmod(0o000)
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)

    try:
        with pytest.raises(ArchiveError) as e:  # never a TypeError from rmtree's handler
            delete_archived_wolt("pal", wolts_dir=tmp_path)
    finally:
        monkeypatch.undo()
        locked.chmod(0o700)

    assert e.value.status == 500 and e.value.message.endswith("run delete again to retry")
    assert locked.is_dir()
    assert AutoGrantStore(tmp_path).list() == []


def test_a_delete_that_fails_leaves_the_wolt_in_the_archive_without_its_grants(
        tmp_path, runtime, monkeypatch):
    import shutil
    import wolt_archive

    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")

    real_rmtree = shutil.rmtree

    def refuse(path, **kwargs):
        if Path(path) != tmp_path / ".archive" / "pal":
            return real_rmtree(path, **kwargs)
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(wolt_archive.shutil, "rmtree", refuse)
    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)
    monkeypatch.setattr(wolt_archive.shutil, "rmtree", real_rmtree)

    assert e.value.status == 500
    assert (tmp_path / ".archive" / "pal" / "wolt" / "wolt.json").is_file()
    # Revoked before the folder goes: a half-done delete never leaves a grant.
    assert AutoGrantStore(tmp_path).list() == []


def test_a_delete_that_failed_halfway_can_be_run_again(tmp_path, runtime, monkeypatch):
    import shutil
    import wolt_archive

    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    target = tmp_path / ".archive" / "pal"
    (target / "data").mkdir()
    (target / "data" / "stuck.bin").write_text("x")

    real_rmtree = shutil.rmtree

    def halfway(path, **kwargs):
        if Path(path) != target:
            return real_rmtree(path, **kwargs)
        (target / "wolt" / "wolt.json").unlink()
        raise PermissionError(13, "Permission denied", str(target / "data" / "stuck.bin"))

    monkeypatch.setattr(wolt_archive.shutil, "rmtree", halfway)
    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)
    monkeypatch.setattr(wolt_archive.shutil, "rmtree", real_rmtree)
    assert e.value.status == 500 and e.value.message.endswith("run delete again to retry")
    assert list_archived_wolts(wolts_dir=tmp_path) == []
    assert AutoGrantStore(tmp_path).list() == []

    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 0}
    assert not target.exists()


def test_delete_refuses_a_symlink_in_the_archive(tmp_path, runtime):
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "wolt").mkdir(parents=True)
    (elsewhere / "wolt" / "wolt.json").write_text("{}")
    (tmp_path / ".archive").mkdir()
    (tmp_path / ".archive" / "pal").symlink_to(elsewhere, target_is_directory=True)

    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 404
    assert (elsewhere / "wolt" / "wolt.json").is_file()
