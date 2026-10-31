"""backup: a copy holding inbox state is refused before disarm touches it (RFC #615)."""

import json
import sys
from pathlib import Path

import pytest

from inbox_support import authority, enroll, rows, session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lodge_db  # noqa: E402
from woltspace import backup  # noqa: E402

RUNTIME = ".space/platform/adoption.json"


def lodge(root, mail=True):
    """A lodge with an ordinary session, and with an enrolled one holding mail."""
    (root / ".space" / "platform").mkdir(parents=True)
    (root / RUNTIME).write_text(json.dumps({"adopted": []}))
    (root / "pal" / "wolt").mkdir(parents=True)
    session(root, "plain", status="stopped")
    if mail:
        auth = authority(root)
        enroll(auth, "s1")
        auth.admit("k1", "session-name", "s1", "hi")
    return root


def state(root):
    return [rows(root, f"SELECT * FROM {t} ORDER BY 1") for t in ("sessions", "inbox_messages", "meta")]


def test_a_backup_with_inbox_mail_restores_to_a_refused_untouched_copy(tmp_path):
    source = lodge(tmp_path / "wolts")
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    with pytest.raises(backup.RestoreRefused) as refused:
        backup.restore_backup(archive, to=tmp_path / "back")
    copy = tmp_path / "back" / backup.ARCHIVE_ROOT
    assert refused.value.code == "safe_state_present" and "hi" not in str(refused.value)
    assert state(copy) == state(source)  # nothing rebased, stopped or set aside
    assert (copy / RUNTIME).exists() and not (copy / backup.RESTORED_MARKER).exists()
    with pytest.raises(ValueError, match="safe_state_present"):  # the CLI's "restore failed"
        backup.restore_backup(archive, to=tmp_path / "again")


@pytest.mark.parametrize("entry", ["disarm", "helper"])
def test_both_ways_into_disarm_refuse_and_leave_the_files_as_they_were(tmp_path, entry):
    root = lodge(tmp_path / "copy")
    files = {p: p.read_bytes() for p in (root / ".space").rglob("*") if p.is_file()}
    with pytest.raises(backup.RestoreRefused, match="safe_state_present"):
        if entry == "disarm":
            backup.disarm(root, "/elsewhere", tmp_path)
        else:
            backup._disarm_lodge_db(root, "/elsewhere", tmp_path, {})
    assert {p: p.read_bytes() for p in (root / ".space").rglob("*") if p.is_file()} == files
    assert not list(tmp_path.glob("**/quarantine*"))


def test_a_lodge_without_inbox_state_still_restores_and_disarms(tmp_path):
    source = lodge(tmp_path / "wolts", mail=False)
    lodge_db.connect(source).close()  # the empty inbox tables of a migrated lodge
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    restored = backup.restore_backup(archive, to=tmp_path / "back")
    assert rows(restored.wolts_dir, "SELECT name, status FROM sessions") == [("plain", "stopped")]
    assert (restored.wolts_dir / backup.RESTORED_MARKER).exists()
    assert not (restored.wolts_dir / RUNTIME).exists()
    assert backup._KNOWN_SCHEMA == max(lodge_db._MIGRATIONS)[0]  # the guard knows every migration


def test_an_inspection_outside_the_target_is_refused_before_reading(tmp_path):
    root = lodge(tmp_path / "elsewhere")
    target = tmp_path / "target"
    target.mkdir()
    (target / "link").symlink_to(tmp_path / "elsewhere")
    for candidate in (root, target / "link", target / "link" / ".." / "elsewhere"):  # a link, its .. too
        with pytest.raises(backup.RestoreRefused, match="inspect_path_unsafe") as refused:
            backup.disarm(candidate, "/x", target)
        assert str(tmp_path) not in str(refused.value)
    assert (root / RUNTIME).exists()


@pytest.mark.parametrize("journal", [b"", b"\0" * 512])  # empty or not: its presence decides
@pytest.mark.parametrize("entry", ["disarm", "helper"])
def test_a_rollback_journal_beside_lodge_db_refuses_before_anything_is_read(tmp_path, entry, journal,
                                                                            monkeypatch):
    root = lodge(tmp_path / "copy", mail=False)
    db = root / backup.LODGE_DB
    db.with_name(db.name + "-journal").write_bytes(journal)
    files = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    opened = []
    monkeypatch.setattr(backup.shutil, "copyfile", lambda *a: opened.append(a))
    monkeypatch.setattr(backup.sqlite3, "connect", lambda *a, **k: opened.append(a))
    report = {"untouched": True}
    with pytest.raises(backup.RestoreRefused, match="safe_state_unknown"):
        if entry == "disarm":
            backup.disarm(root, "/elsewhere", tmp_path)
        else:
            backup._disarm_lodge_db(root, "/elsewhere", tmp_path, report)
    assert opened == [] and report == {"untouched": True}
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == files
