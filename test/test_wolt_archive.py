"""Archive, unarchive and delete a wolt (container/lib/wolt_archive.py).

Archiving moves wolts/<name> to wolts/.archive/<name>, which every wolt scan
already skips. tmp_path and a fake runtime only: no real tmux.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import sessions  # noqa: E402
import wolfcore  # noqa: E402
import wolt_archive  # noqa: E402
from execution_policy import AutoGrantStore  # noqa: E402
from session_targets import SessionTarget  # noqa: E402
from sessions import SessionRegistry  # noqa: E402
from wolt_archive import (  # noqa: E402
    ArchiveError, archive_wolt, delete_archived_wolt, list_archived_wolts, unarchive_wolt)

RUNNING, NO_ID, RECOVERED, RESTING = (
    "pal-maple-a1b2c3", "pal-cedar-778899", "pal-ash-elm-334455", "pal-birch-d4e5f6")
HOLD = {"until": None, "reason": "npm run dev", "since": 1}
CRONS = {"crons": [{"name": "digest", "schedule": "0 9 * * *", "prompt": "hi"}]}


def _wolt(wolts_dir, name, **config):
    (wolts_dir / name / "wolt").mkdir(parents=True)
    (wolts_dir / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name, "type": "beaver", **config}))
    return wolts_dir / name


def _session(wolts_dir, wolt, name, *, status, runtime=None, **fields):
    registry = SessionRegistry(wolts_dir)
    registry.create(name, wolt=wolt)
    registry._write(wolt, name, {**registry._read(wolt, name), "status": status, **fields})
    if runtime is not None and status == "running":
        runtime.live.add(name)


def _record(wolt_home, name):
    return json.loads((wolt_home / ".state" / "sessions" / f"{name}.json").read_text())


def _owners(wolts_dir):
    return {cron["_owner"] for cron in wolfcore.load_all(wolts_dir)[0]}


def _grant(wolts_dir, wolt, workdir):
    workdir.mkdir(parents=True, exist_ok=True)
    AutoGrantStore(wolts_dir).grant(SessionTarget(wolt, workdir.resolve()))


def _revive_in_the_gap(wolts_dir, runtime, monkeypatch, wolt, *names):
    """Bring sessions back between the stop pass and the move."""
    real_rename = os.rename

    def revive_then_rename(src, dst):
        # os.rename is global: the registry's own writes come through here too.
        if Path(src) == wolts_dir / wolt:
            for name in names:
                SessionRegistry(wolts_dir).update(name, wolt=wolt, status="running")
                runtime.live.add(name)
        real_rename(src, dst)

    monkeypatch.setattr(wolt_archive.os, "rename", revive_then_rename)


# --- archive -----------------------------------------------------------------

def test_archive_moves_the_folder_and_ends_running_sessions(tmp_path, live_runtime, monkeypatch):
    home = _wolt(tmp_path, "pal")
    (home / "wolt" / "wolf.json").write_text(json.dumps(CRONS))
    _session(tmp_path, "pal", RUNNING, status="running", runtime=live_runtime, harness_session_id="abc")
    _session(tmp_path, "pal", NO_ID, status="running", runtime=live_runtime)
    _session(tmp_path, "pal", RECOVERED, status="running", runtime=live_runtime)
    _session(tmp_path, "pal", RESTING, status="resting", harness_session_id="def")
    monkeypatch.setattr(sessions, "recover_resume_id",
                        lambda data, taken: "found-1" if data["name"] == RECOVERED else "")
    assert "pal" in _owners(tmp_path)

    result = archive_wolt("pal", wolts_dir=tmp_path, now=5000)

    assert result == {"archived": "pal", "stopped": sorted([RUNNING, NO_ID, RECOVERED])}
    archived = tmp_path / ".archive" / "pal"
    assert not home.exists() and (archived / "wolt" / "wolt.json").is_file()
    assert "pal" not in _owners(tmp_path) and live_runtime.live == set()
    # With a conversation id, found or recovered, a session rests: after unarchive a message wakes it.
    rested = _record(archived, RUNNING)
    assert (rested["status"], rested["rested_at"], rested["rest_reason"]) == ("resting", 5000, "archived")
    assert [_record(archived, RECOVERED)[k] for k in ("status", "harness_session_id")] == ["resting", "found-1"]
    assert [_record(archived, NO_ID)[k] for k in ("status", "finished_at")] == ["stopped", 5000]
    assert "rest_reason" not in _record(archived, RESTING)


@pytest.mark.parametrize("taken", ["before", "during"])
def test_a_hold_refuses_the_archive_and_ends_nothing_it_holds(tmp_path, live_runtime, monkeypatch, taken):
    """A hold from before ends nothing; F1: one taken during it is checked again under the lock."""
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status="running", runtime=live_runtime, harness_session_id="abc",
             **({"hold": dict(HOLD)} if taken == "before" else {}))
    _session(tmp_path, "pal", NO_ID, status="running", runtime=live_runtime, harness_session_id="def")
    real_stop, held = live_runtime.stop, [RUNNING] if taken == "before" else []

    def hold_the_other(handle):
        if not held:  # the hold route writes between the first check and this session's turn
            held.append(NO_ID if handle.tmux_session_name == RUNNING else RUNNING)
            registry = SessionRegistry(tmp_path)
            registry._write("pal", held[0], {**registry._read("pal", held[0]), "hold": dict(HOLD)})
        return real_stop(handle)

    monkeypatch.setattr(live_runtime, "stop", hold_the_other)
    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert e.value.extra["held"] == [{"session": held[0], "reason": "npm run dev"}]
    assert held[0] in live_runtime.live and _record(tmp_path / "pal", held[0])["status"] == "running"
    assert len(live_runtime.stops) == (0 if taken == "before" else 1)
    assert not (tmp_path / ".archive" / "pal").exists()


@pytest.mark.parametrize("status, message, left_in", [
    ("running", f"session {RUNNING} could not be stopped", "pal"),  # before the move: nothing moves
    ("resting", f"archived pal, but session {RUNNING} could not be stopped", ".archive/pal"),  # N1: the sweep
])
def test_a_session_that_will_not_stop_is_a_500(tmp_path, live_runtime, monkeypatch, status, message, left_in):
    _wolt(tmp_path, "pal")
    _session(tmp_path, "pal", RUNNING, status=status, runtime=live_runtime, harness_session_id="abc")
    _revive_in_the_gap(tmp_path, live_runtime, monkeypatch, "pal", RUNNING)
    monkeypatch.setattr(live_runtime, "stop", lambda handle: False)

    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)

    assert (e.value.status, e.value.message) == (500, message)
    assert (tmp_path / left_in / "wolt" / "wolt.json").is_file()


def test_the_sweep_ends_exact_names_only_and_recovers_no_id(tmp_path, live_runtime, monkeypatch):
    """ledger-two survives archiving ledger; a session started in the gap without an id is stopped."""
    woke, fresh, two = "ledger-oak-elm-111111", "ledger-new-ash-333333", "ledger-two-elm-fir-222222"
    _wolt(tmp_path, "ledger")
    _wolt(tmp_path, "ledger-two")
    _session(tmp_path, "ledger", woke, status="resting", harness_session_id="l-1")
    _session(tmp_path, "ledger", fresh, status="stopped")
    _session(tmp_path, "ledger-two", two, status="running", runtime=live_runtime, harness_session_id="t-1")
    _revive_in_the_gap(tmp_path, live_runtime, monkeypatch, "ledger", woke, fresh)
    monkeypatch.setattr(sessions, "recover_resume_id", lambda data, taken: "found-1")

    assert archive_wolt("ledger", wolts_dir=tmp_path)["stopped"] == sorted([woke, fresh])

    archived = tmp_path / ".archive" / "ledger"
    assert live_runtime.live == {two} and _record(tmp_path / "ledger-two", two)["status"] == "running"
    assert [_record(archived, woke)[k] for k in ("status", "rest_reason")] == ["resting", "archived"]
    assert _record(archived, fresh)["status"] == "stopped" and "harness_session_id" not in _record(archived, fresh)
    assert not (tmp_path / ".archive" / ".state").exists()


@pytest.mark.parametrize("action", [archive_wolt, unarchive_wolt, delete_archived_wolt])
def test_bad_or_unknown_names_change_nothing(tmp_path, live_runtime, action):
    """Review Focus 1: a mistyped name creates nothing; a live wolt is never unarchived or deleted."""
    _wolt(tmp_path, "pal")
    for name in ["ledgr", "pal", "../x", ".archive", "a/b", "", "-x", "a" * 65]:
        if action is archive_wolt and name == "pal":
            continue
        with pytest.raises(ArchiveError) as e:
            action(name, wolts_dir=tmp_path)
        assert e.value.status == (404 if name in ("ledgr", "pal") else 400), name
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pal"]


def test_archive_refuses_a_name_the_archive_holds(tmp_path, live_runtime):
    _wolt(tmp_path, "pal")
    (tmp_path / ".archive" / "pal").mkdir(parents=True)
    with pytest.raises(ArchiveError) as e:
        archive_wolt("pal", wolts_dir=tmp_path)
    assert e.value.status == 409 and (tmp_path / "pal" / "wolt" / "wolt.json").is_file()


@pytest.mark.parametrize("name", ["apps", "projects"])
def test_the_apps_folders_are_no_wolts(tmp_path, live_runtime, name):
    """#575 review: wolts/apps (and the legacy wolts/projects) hold the lodge's apps, even with a wolt.json."""
    _wolt(tmp_path, name)
    (tmp_path / name / "todo").mkdir()
    (tmp_path / name / "todo" / "app.json").write_text("{}")
    for action in (archive_wolt, unarchive_wolt, delete_archived_wolt):
        with pytest.raises(ArchiveError) as e:
            action(name, wolts_dir=tmp_path)
        assert e.value.status == 400
    assert (tmp_path / name / "todo" / "app.json").is_file() and not (tmp_path / ".archive").exists()
    _wolt(tmp_path / ".archive", name)  # moved there by hand
    assert list_archived_wolts(wolts_dir=tmp_path) == []


def test_list_archived_shows_the_shown_name(tmp_path, live_runtime):
    assert list_archived_wolts(wolts_dir=tmp_path) == []
    _wolt(tmp_path, "pal", display_name="Pal The Builder")
    _wolt(tmp_path, "Old_Pal")  # older wolts can have capitals and underscores
    archive_wolt("pal", wolts_dir=tmp_path)
    archive_wolt("Old_Pal", wolts_dir=tmp_path)
    assert list_archived_wolts(wolts_dir=tmp_path) == [
        {"name": "Old_Pal", "display_name": None, "type": "beaver"},
        {"name": "pal", "display_name": "Pal The Builder", "type": "beaver"}]


# --- unarchive ---------------------------------------------------------------

def test_unarchive_brings_it_all_back_past_an_empty_ghost(tmp_path, live_runtime):
    """Review Focus 3 / H1: a message racing the move leaves folders and an empty <s>.lock."""
    home = _wolt(tmp_path, "pal")
    (home / "wolt" / "wolf.json").write_text(json.dumps(CRONS))
    _session(tmp_path, "pal", RESTING, status="resting", harness_session_id="abc")
    archive_wolt("pal", wolts_dir=tmp_path)
    (home / ".state" / "sessions").mkdir(parents=True)
    (home / ".state" / "sessions" / f"{RESTING}.lock").touch()

    assert unarchive_wolt("pal", wolts_dir=tmp_path) == {"unarchived": "pal"}

    assert not (tmp_path / ".archive" / "pal").exists() and "pal" in _owners(tmp_path)
    record = SessionRegistry(tmp_path).get(RESTING, check_alive=False)
    assert (record["status"], record["harness_session_id"]) == ("resting", "abc")


@pytest.mark.parametrize("leftover", ["wolt/wolt.json", "notes.txt", ".state/sessions/x.lock", "link"])
def test_unarchive_refuses_anything_more_than_a_ghost(tmp_path, live_runtime, leftover):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    (tmp_path / "pal" / "a" / "empty").mkdir(parents=True)
    path = tmp_path / "pal" / leftover
    path.parent.mkdir(parents=True, exist_ok=True)
    if leftover == "link":
        (tmp_path / "elsewhere").mkdir()
        path.symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    else:  # a live wolt by that name, a note, a lock that holds something
        path.write_text("keep me")

    with pytest.raises(ArchiveError) as e:
        unarchive_wolt("pal", wolts_dir=tmp_path)

    assert e.value.status == 409
    assert (tmp_path / "pal" / "a" / "empty").is_dir()  # a refusal removes nothing
    assert (tmp_path / ".archive" / "pal" / "wolt" / "wolt.json").is_file()


# --- delete ------------------------------------------------------------------

def test_delete_revokes_every_grant_and_the_cron_stamps(tmp_path, live_runtime):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    for wolt, workdir in [("pal", "a"), ("pal", "b"), ("other", "a")]:
        _grant(tmp_path, wolt, tmp_path / workdir)
        (tmp_path / ".space" / "wolf" / wolt).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".space" / "wolf" / "pal" / "digest.last").write_text("2026-10-17-09:00")

    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 2}

    assert not (tmp_path / ".archive" / "pal").exists()
    assert [g.wolt_id for g in AutoGrantStore(tmp_path).list()] == ["other"]
    assert [p.name for p in (tmp_path / ".space" / "wolf").iterdir()] == ["other"]


@pytest.mark.parametrize("mode", [0o555, 0o000, 0o300])
def test_delete_removes_folders_it_cannot_write_or_read(tmp_path, live_runtime, mode):
    """Review Focus 4 / F2: a Go module cache, an unpacked tarball, a test's chmod."""
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    locked = tmp_path / ".archive" / "pal" / "pkg" / "locked"
    locked.mkdir(parents=True)
    (locked / "mod.go").write_text("package mod")
    (locked / "mod.go").chmod(0o444)
    locked.chmod(mode)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o500)
    (tmp_path / ".archive" / "pal" / "link").symlink_to(outside, target_is_directory=True)

    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 1}
    assert not (tmp_path / ".archive" / "pal").exists() and AutoGrantStore(tmp_path).list() == []
    assert outside.stat().st_mode & 0o777 == 0o500  # never chmodded through the link


def test_a_folder_that_stays_unreadable_ends_as_a_500(tmp_path, live_runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    locked = tmp_path / ".archive" / "pal" / "locked"
    locked.mkdir()
    locked.chmod(0o000)
    monkeypatch.setattr(os, "chmod", lambda *args, **kwargs: None)
    try:
        with pytest.raises(ArchiveError) as e:  # never a TypeError from rmtree's handler
            delete_archived_wolt("pal", wolts_dir=tmp_path)
    finally:
        monkeypatch.undo()
        locked.chmod(0o700)
    assert e.value.status == 500 and locked.is_dir()


def test_a_delete_that_failed_halfway_can_run_again(tmp_path, live_runtime, monkeypatch):
    _wolt(tmp_path, "pal")
    archive_wolt("pal", wolts_dir=tmp_path)
    _grant(tmp_path, "pal", tmp_path / "a")
    target, real_rmtree = tmp_path / ".archive" / "pal", shutil.rmtree

    def halfway(path, **kwargs):
        if Path(path) != target:
            return real_rmtree(path, **kwargs)
        (target / "wolt" / "wolt.json").unlink()
        raise PermissionError(13, "Permission denied", str(target / "stuck.bin"))

    monkeypatch.setattr(wolt_archive.shutil, "rmtree", halfway)
    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)
    monkeypatch.setattr(wolt_archive.shutil, "rmtree", real_rmtree)

    assert e.value.status == 500 and e.value.message.endswith("run delete again to retry")
    # The grants went first: a half-done delete never leaves one for a later wolt by this name.
    assert AutoGrantStore(tmp_path).list() == [] and list_archived_wolts(wolts_dir=tmp_path) == []
    assert delete_archived_wolt("pal", wolts_dir=tmp_path) == {"deleted": "pal", "grants_revoked": 0}
    assert not target.exists()


def test_delete_refuses_a_symlink_in_the_archive(tmp_path, live_runtime):
    (tmp_path / "elsewhere" / "wolt").mkdir(parents=True)
    (tmp_path / ".archive").mkdir()
    (tmp_path / ".archive" / "pal").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    with pytest.raises(ArchiveError) as e:
        delete_archived_wolt("pal", wolts_dir=tmp_path)
    assert e.value.status == 404 and (tmp_path / "elsewhere" / "wolt").is_dir()


# --- links out of the wolts folder (#575 review) -----------------------------

@pytest.mark.parametrize("action, name, link", [
    (archive_wolt, "kit", ".archive"), (unarchive_wolt, "pal", ".archive"),
    (delete_archived_wolt, "pal", ".archive"), (unarchive_wolt, "pal", ".archive/pal"), (archive_wolt, "pal", "pal")])
def test_a_link_out_of_the_wolts_folder_is_never_followed(tmp_path, live_runtime, action, name, link):
    """.archive, a wolt in it or a live wolt linked to a sibling folder: nothing there moves, goes or lists."""
    wolts_dir, outside = tmp_path / "wolts", tmp_path / "outside"
    _wolt(wolts_dir, "kit")
    _wolt(outside, "pal")
    if link == ".archive/pal":
        (wolts_dir / ".archive").mkdir()
    (wolts_dir / link).symlink_to(outside if link == ".archive" else outside / "pal", target_is_directory=True)

    with pytest.raises(ArchiveError) as e:
        action(name, wolts_dir=wolts_dir)

    assert e.value.status == 409
    assert os.listdir(outside) == ["pal"] and (outside / "pal" / "wolt" / "wolt.json").is_file()
    assert sorted(os.listdir(wolts_dir)) == sorted(["kit", Path(link).parts[0]])
    if link == ".archive":
        assert list_archived_wolts(wolts_dir=wolts_dir) == []


def test_a_wolts_folder_that_is_itself_a_link_still_works(tmp_path, live_runtime):
    """A lodge on another disk: the link checks resolve both sides."""
    disk, wolts_dir = tmp_path / "disk", tmp_path / "wolts"
    _wolt(disk, "pal")
    (disk / ".space" / "wolf" / "pal").mkdir(parents=True)
    wolts_dir.symlink_to(disk, target_is_directory=True)

    archive_wolt("pal", wolts_dir=wolts_dir)
    assert [w["name"] for w in list_archived_wolts(wolts_dir=wolts_dir)] == ["pal"]
    unarchive_wolt("pal", wolts_dir=wolts_dir)
    archive_wolt("pal", wolts_dir=wolts_dir)
    assert delete_archived_wolt("pal", wolts_dir=wolts_dir)["deleted"] == "pal"
    assert not (disk / ".archive" / "pal").exists() and os.listdir(disk / ".space" / "wolf") == []


@pytest.mark.parametrize("link", [".space/wolf", ".space"])
def test_delete_never_follows_a_link_to_the_cron_stamps(tmp_path, live_runtime, link):
    """A link anywhere on .space/wolf/<name> skips the stamps; the delete still goes through."""
    wolts_dir, outside = tmp_path / "wolts", tmp_path / "outside"
    _wolt(wolts_dir, "pal")
    archive_wolt("pal", wolts_dir=wolts_dir)
    stamp = outside / ".space" / "wolf" / "pal" / "digest.last"
    stamp.parent.mkdir(parents=True)
    stamp.write_text("2026-10-17-09:00")
    if link == ".space/wolf":
        (wolts_dir / ".space").mkdir()
    (wolts_dir / link).symlink_to(outside / link, target_is_directory=True)

    assert delete_archived_wolt("pal", wolts_dir=wolts_dir)["deleted"] == "pal"
    assert stamp.read_text() == "2026-10-17-09:00" and not (wolts_dir / ".archive" / "pal").exists()
