"""backup: a copy's inbox restores as it is; one this restore cannot read is refused."""

import json
import sys
import time
from pathlib import Path

import pytest

from inbox_support import acked, limits, reader, rows, session

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import inbox  # noqa: E402
import lodge_db  # noqa: E402
from woltspace import backup  # noqa: E402

RUNTIME = ".space/platform/adoption.json"


def lodge(root, mail=True):
    """A lodge with an ordinary session, and with a reader whose mailbox holds mail."""
    (root / ".space" / "platform").mkdir(parents=True)
    (root / RUNTIME).write_text(json.dumps({"adopted": []}))
    (root / "pal" / "wolt").mkdir(parents=True)
    session(root, "plain", status="stopped")
    if mail:
        store = inbox.Store(root, limits())
        reader(store, "s1")
        store.admit("ops", "k1", "pal", "hi")
    return root


def inbox_line(restored):
    return [line for line in backup.disarm_lines(restored) if line.startswith("inbox restored:")]


def test_acknowledged_mail_restores_with_its_key_so_a_resend_is_not_admitted_twice(tmp_path):
    source = lodge(tmp_path / "wolts", mail=False)
    store = inbox.Store(source, limits())
    token = reader(store, "s1")
    store.admit("ops", "k1", "pal", "hi")
    acked(store, token)
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    restored = backup.restore_backup(archive, to=tmp_path / "back")
    copy = restored.wolts_dir
    kept = "SELECT seq, sender, key, state, digest FROM inbox_messages"
    assert rows(copy, kept) == rows(source, kept) and (copy / backup.RESTORED_MARKER).exists()
    again = inbox.Store(copy, limits()).admit("ops", "k1", "pal", "hi")
    assert (again.seq, again.state) == (1, "acknowledged")
    assert rows(copy, "SELECT count(*) FROM inbox_messages") == [(1,)]
    assert inbox_line(restored)[0].startswith("inbox restored: 0 queued (0 re-queued from claims), 1 acknowledged.")


def test_mail_claimed_in_the_old_lodge_comes_back_queued_for_a_new_reader(tmp_path):
    source = lodge(tmp_path / "wolts", mail=False)
    store = inbox.Store(source, limits())
    old = reader(store, "s1")
    store.admit("ops", "k1", "pal", "hi")
    assert store.fetch(old).body == "hi"
    assert rows(source, "SELECT wake IS NOT NULL FROM inbox_mailboxes") == [(1,)]
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    restored = backup.restore_backup(archive, to=tmp_path / "back")
    copy = restored.wolts_dir
    assert rows(copy, "SELECT state, reader FROM inbox_messages") == [("queued", None)]
    assert rows(copy, "SELECT seq, state, reason FROM inbox_events ORDER BY cursor DESC LIMIT 1") == [
        (1, "queued", "restored")]
    assert rows(copy, "SELECT wake FROM inbox_mailboxes") == [(None,)]
    restored_store = inbox.Store(copy, limits())
    got = restored_store.fetch(reader(restored_store, "s2"))
    assert (got.seq, got.body, got.claim) == (1, "hi", 2)
    assert "Keep the old lodge stopped." in inbox_line(restored)[0]
    assert inbox_line(restored)[0].startswith("inbox restored: 1 queued (1 re-queued from claims), 0 acknowledged.")


def test_a_restored_lodge_admits_new_mail_after_the_old_without_reusing_ids(tmp_path):
    source = lodge(tmp_path / "wolts")
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    copy = backup.restore_backup(archive, to=tmp_path / "back").wolts_dir
    assert inbox.Store(copy, limits()).admit("ops", "k2", "pal", "more").seq == 2
    assert rows(copy, "SELECT seq, key, state FROM inbox_messages ORDER BY seq") == [
        (1, "k1", "queued"), (2, "k2", "queued")]


@pytest.mark.parametrize("entry", ["disarm", "helper"])
def test_both_ways_into_disarm_keep_the_mail_and_revoke_its_readers(tmp_path, entry):
    root = lodge(tmp_path / "copy")
    mail = rows(root, "SELECT * FROM inbox_messages")
    if entry == "disarm":
        backup.disarm(root, "/elsewhere", tmp_path)
    else:
        report = {"apps": [], "sessions_stopped": 0, "sessions_rebased": 0, "sessions_set_aside": [],
                  "grants_rebased": 0, "grants_dropped": []}
        backup._disarm_lodge_db(root, "/elsewhere", tmp_path, report)
    assert rows(root, "SELECT * FROM inbox_messages") == mail
    assert rows(root, "SELECT count(*) FROM inbox_readers WHERE revoked=0") == [(0,)]


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


def test_a_lodge_whose_inbox_holds_no_mail_restores_and_its_old_readers_stay_dead(tmp_path):
    source = lodge(tmp_path / "wolts", mail=False)
    token = reader(inbox.Store(source, limits()), "s1")
    archive = backup.create_backup(source, out_dir=tmp_path / "out").archive
    restored = backup.restore_backup(archive, to=tmp_path / "back").wolts_dir
    assert rows(restored, "SELECT id, state, revoked FROM inbox_mailboxes, inbox_readers") == [(1, "open", 1)]
    session(restored, "s1")  # running again later, as a resumed session would be
    with pytest.raises(inbox.Refused, match="reader_invalid"):
        inbox.Store(restored, limits()).fetch(token)


def test_mail_acknowledged_more_than_30_days_ago_restores_until_a_write_prunes_it(tmp_path, monkeypatch):
    source = lodge(tmp_path / "wolts", mail=False)
    store = inbox.Store(source, limits())
    token = reader(store, "s1")
    store.send(token, "pal", "k", "x")
    acked(store, token)
    later = time.time() + inbox.RETAIN_SECONDS + 1
    monkeypatch.setattr(inbox.time, "time", lambda: later)
    expired = backup.create_backup(source, out_dir=tmp_path / "out1").archive
    kept = backup.restore_backup(expired, to=tmp_path / "back1").wolts_dir
    assert rows(kept, "SELECT state FROM inbox_messages") == [("acknowledged",)]  # no clock here
    assert store.fetch(token) is None  # an ordinary, successful write: the expired receipt goes
    archive = backup.create_backup(source, out_dir=tmp_path / "out2").archive
    restored = backup.restore_backup(archive, to=tmp_path / "back2").wolts_dir
    assert rows(restored, "SELECT id, state, revoked FROM inbox_mailboxes, inbox_readers") == [(1, "open", 1)]
    assert rows(restored, "SELECT scope, pending, bytes, keys FROM inbox_usage ORDER BY 1") == [
        ("", 0, 0, 0), ("1", 0, 0, 0)]
    assert rows(restored, "SELECT seq FROM sqlite_sequence WHERE name='inbox_messages'") == [(1,)]
