"""SessionTarget persistence and arbitrary-repository behavior."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from session_targets import SessionTarget, normalize_session_target


def _wolt(tmp_path: Path, name: str = "testwolt") -> Path:
    home = tmp_path / "wolts" / name
    (home / "wolt" / "site").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": name, "type": "raccoon"})
    )
    (home / "wolt" / "site" / "index.html").write_text("ok")
    return home


def test_target_defaults_to_canonical_wolt_home(tmp_path):
    home = _wolt(tmp_path)
    target = SessionTarget.resolve("testwolt", None, wolts_dir=tmp_path / "wolts")
    assert target.wolt_id == "testwolt"
    assert target.canonical_workdir == home.resolve()


def test_target_canonicalizes_symlinked_repo(tmp_path):
    _wolt(tmp_path)
    repo = tmp_path / "real-repo"
    repo.mkdir()
    link = tmp_path / "repo-link"
    link.symlink_to(repo, target_is_directory=True)

    target = SessionTarget.resolve("testwolt", link, wolts_dir=tmp_path / "wolts")
    assert target.canonical_workdir == repo.resolve()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_target_rejects_non_directory_workdir(tmp_path, kind):
    _wolt(tmp_path)
    requested = tmp_path / kind
    if kind == "file":
        requested.write_text("not a directory")
    with pytest.raises(ValueError, match="workdir"):
        SessionTarget.resolve("testwolt", requested, wolts_dir=tmp_path / "wolts")


def test_legacy_record_gets_non_destructive_target_backfill(tmp_path):
    workdir = tmp_path / "old-repo"
    record = {"name": "old", "wolt": "testwolt", "dir": str(workdir)}
    normalized = normalize_session_target(record, wolts_dir=tmp_path / "wolts")

    assert normalized["target"] == {
        "wolt_id": "testwolt",
        "canonical_workdir": str(workdir.resolve()),
    }
    assert normalized["wolt_id"] == "testwolt"
    assert normalized["workdir"] == str(workdir.resolve())
    assert record == {"name": "old", "wolt": "testwolt", "dir": str(workdir)}


def test_registry_persists_new_target_and_legacy_aliases(tmp_path):
    from sessions import SessionRegistry

    home = _wolt(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    target = SessionTarget.resolve("testwolt", repo, wolts_dir=tmp_path / "wolts")
    reg = SessionRegistry(tmp_path / "wolts")
    reg.create("testwolt-session", wolt="testwolt", target=target)

    stored = json.loads(
        (home / ".state" / "sessions" / "testwolt-session.json").read_text()
    )
    assert stored["target"] == target.to_record()
    assert stored["wolt"] == stored["wolt_id"] == "testwolt"
    assert stored["dir"] == stored["workdir"] == str(repo.resolve())


@patch("sessions.ensure_site")
def test_one_wolt_can_start_sessions_in_two_repositories(
    mock_site, tmp_path, monkeypatch, fake_runtime
):
    import paths
    import sessions

    _wolt(tmp_path)
    repos = [tmp_path / "repo-a", tmp_path / "repo-b"]
    for repo in repos:
        repo.mkdir()

    wolts_dir = tmp_path / "wolts"
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts_dir)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts_dir)
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))

    first = sessions.start_session(wolt="testwolt", workdir=repos[0])
    second = sessions.start_session(wolt="testwolt", workdir=repos[1])

    assert first["name"] != second["name"]
    assert first["wolt_id"] == second["wolt_id"] == "testwolt"
    assert {first["workdir"], second["workdir"]} == {
        str(repos[0].resolve()), str(repos[1].resolve())
    }
    assert [spawn[1] for spawn in fake_runtime.spawns[-2:]] == [
        str(repos[0].resolve()), str(repos[1].resolve())
    ]


def test_resume_uses_persisted_canonical_workdir(tmp_path, monkeypatch, fake_runtime):
    import paths
    import sessions

    _wolt(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    wolts_dir = tmp_path / "wolts"
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts_dir)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts_dir)
    monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))

    reg = sessions.SessionRegistry(wolts_dir)
    target = SessionTarget.resolve("testwolt", repo, wolts_dir=wolts_dir)
    reg.create("testwolt-session", wolt="testwolt", target=target)
    fake_runtime._alive = False

    sessions.resume_session("testwolt-session")
    assert fake_runtime.spawns[-1][1] == str(repo.resolve())
