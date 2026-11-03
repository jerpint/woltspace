"""Adversarial OFFLINE copies: rejected native state retains every original byte."""
import hashlib
import json
import os
import shutil
import sqlite3
import traceback
from contextlib import closing
from pathlib import Path

import pytest

from inbox_support import limits, session
import inbox
import lodge_db
from woltspace import backup

PRIVATE = "NEUTRAL_" + "PRIVATE_PATH"
NATIVE = {
    "attempt": "INSERT INTO inbox_attempts(id,seq,ordinal,generation) VALUES('a',999,1,'g')",
    "event": "INSERT INTO inbox_events(seq,state) VALUES(999,'queued')",
    "usage": "INSERT INTO inbox_usage VALUES('',0,0,0)",
    "counter": "INSERT INTO meta VALUES('inbox:enrollments','0')",
    "partial-marker": "UPDATE sessions SET inbox_revision=1",
    "sequence": "INSERT INTO sqlite_sequence VALUES('inbox_messages',7)",
    "extra-column": "ALTER TABLE inbox_messages ADD COLUMN future_field TEXT",
    "partial-tables": "DROP TABLE inbox_attempts",
    "missing-tables": ";".join(f"DROP TABLE {t}" for t in
                              ("inbox_attempts", "inbox_events", "inbox_usage", "inbox_messages")),
    "dummy-tables": ";".join(f"DROP TABLE {t}; CREATE TABLE {t}(dummy TEXT)" for t in
                            ("inbox_attempts", "inbox_events", "inbox_usage", "inbox_messages")),
    "future-version": "PRAGMA user_version=9",
    "future-floor": "UPDATE meta SET value='99' WHERE key='min_schema'",
    "missing-meta": "DROP TABLE meta",
}


def fingerprint(root):
    result = {}
    for path in sorted(root.rglob("*")):
        key = str(path.relative_to(root))
        if path.is_symlink():
            result[key] = ("link", os.readlink(path))
        elif path.is_file():
            result[key] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
        else:
            result[key] = ("directory", "")
    return result


def call(entry, target, root, report):
    if entry == "disarm":
        return backup.disarm(root, "/dummy-source", target)
    return backup._disarm_lodge_db(root, "/dummy-source", target, report)


def offline(root, kind, temporary):
    (root / ".space/platform").mkdir(parents=True)
    (root / ".space/platform/adoption.json").write_text('{"adopted":[]}')
    db = lodge_db.path(root)
    if kind == "wal-only":
        seed = temporary / "wal-seed"
        with closing(lodge_db.connect(seed)) as keeper:
            keeper.execute("PRAGMA wal_autocheckpoint=0")
            keeper.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            inbox.Store(seed, limits()).admit("k", "pal", "c", "dummy-body")
            for source in lodge_db.path(seed).parent.glob("lodge.db*"):
                shutil.copyfile(source, db.parent / source.name)
        probe = temporary / "main-only.db"
        shutil.copyfile(db, probe)
        with closing(sqlite3.connect(probe)) as conn:
            assert conn.execute("SELECT count(*) FROM inbox_messages").fetchone()[0] == 0
        probe.unlink()  # native custody is committed in the copied WAL, not this main DB
    else:
        session(root, "s1")
        if kind == "accepted":
            store = inbox.Store(root, limits())
            store.admit("k", "pal", "c", "dummy-body")
            store.record(store.claim("pal", "c", "g"), inbox.Outcome.ACCEPTED)
        elif kind in NATIVE:
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(NATIVE[kind])
        elif kind == "invalid-db":
            db.write_bytes(b"not a SQLite database")
        elif kind.startswith("orphan-"):
            db.unlink()
            db.with_name(db.name + "-" + kind.removeprefix("orphan-")).write_bytes(b"ambiguous")


@pytest.fixture
def scratch(monkeypatch):
    created, original = [], backup.tempfile.mkdtemp
    def tracked(*args, **kwargs):
        path = original(*args, **kwargs)
        created.append(Path(path))
        return path
    monkeypatch.setattr(backup.tempfile, "mkdtemp", tracked)
    return created


@pytest.mark.parametrize("entry", ["disarm", "helper"])
@pytest.mark.parametrize("kind", [*NATIVE, "accepted", "wal-only", "invalid-db", "orphan-shm", "orphan-journal"])
def test_native_unknown_and_wal_custody_refuse_before_any_mutation(tmp_path, monkeypatch, request, scratch, entry, kind):
    target, root = tmp_path / "target", tmp_path / "target/copy"
    offline(root, kind, tmp_path)
    before, report = fingerprint(target), {"untouched": "sentinel"}
    def forbidden(*args, **kwargs):
        pytest.fail("inspection crossed into rebase/quarantine/usable-result mutation")
    monkeypatch.setattr(backup, "_disarm_session", forbidden)
    try:
        with pytest.raises(backup.RestoreRefused) as caught:
            call(entry, target, root, report)
        assert caught.value.code in ("safe_state_present", "safe_state_unknown")
        if kind in ("accepted", "wal-only", "attempt", "event", "usage", "counter", "partial-marker"):
            assert caught.value.code == "safe_state_present"
    finally:
        after = fingerprint(target)
        request.node.user_properties.extend([("tree_before", json.dumps(before)), ("tree_after", json.dumps(after)),
                                              ("scratch_removed", str(all(not p.exists() for p in scratch)))])
        assert after == before
        assert report == {"untouched": "sentinel"}
        assert all(not path.exists() for path in scratch)


@pytest.mark.parametrize("entry", ["disarm", "helper"])
@pytest.mark.parametrize("unsafe", ["trusted-anchor-link", "space-link", "db-link", "wal-link", "shm-link",
                                   "db-directory", "wal-directory", "shm-directory"])
def test_inspector_path_refuses_before_following_or_copying_with_fixed_public_error(tmp_path, monkeypatch, entry, unsafe):
    target, root = tmp_path / "target", tmp_path / "target/copy"
    offline(root, "empty", tmp_path)
    foreign = tmp_path / PRIVATE
    foreign.mkdir()
    foreign_file = foreign / "db"
    foreign_file.write_bytes(b"foreign bytes must never be inspected")
    if unsafe == "trusted-anchor-link":
        alias = tmp_path / "alias"
        alias.symlink_to(target, target_is_directory=True)
        target, root = alias, alias / "copy"
    elif unsafe == "space-link":
        shutil.rmtree(root / ".space")
        (root / ".space").symlink_to(foreign, target_is_directory=True)
    else:
        suffix = "" if unsafe.startswith("db-") else "-wal" if unsafe.startswith("wal-") else "-shm"
        path = lodge_db.path(root).with_name("lodge.db" + suffix)
        path.unlink(missing_ok=True)
        if unsafe.endswith("link"):
            path.symlink_to(foreign_file)
        else:
            path.mkdir()
    before = fingerprint(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("unsafe candidate was read or copied before path precondition")
    monkeypatch.setattr(backup.shutil, "copyfile", forbidden)
    monkeypatch.setattr(backup.sqlite3, "connect", forbidden)
    with pytest.raises(backup.RestoreRefused) as caught:
        call(entry, target, root, {})
    assert caught.value.code == "inspect_path_unsafe"
    rendered = "".join(traceback.format_exception(caught.value))
    assert PRIVATE not in rendered and str(tmp_path) not in rendered
    assert fingerprint(tmp_path) == before


@pytest.mark.parametrize("entry", ["disarm", "helper"])
@pytest.mark.parametrize("stage", ["allocate", "copy", "read"])
def test_inspector_copy_failure_is_fixed_and_scratch_removed(tmp_path, monkeypatch, scratch, entry, stage):
    target, root = tmp_path / "target", tmp_path / "target/copy"
    offline(root, "empty", tmp_path)
    before = fingerprint(target)
    def failed(*args, **kwargs):
        raise OSError(PRIVATE)
    module, method = {"allocate": (backup.tempfile, "mkdtemp"), "copy": (backup.shutil, "copyfile"),
                      "read": (backup.sqlite3, "connect")}[stage]
    monkeypatch.setattr(module, method, failed)
    with pytest.raises(backup.RestoreRefused) as caught:
        call(entry, target, root, {})
    assert caught.value.code == "safe_state_unknown"
    assert PRIVATE not in "".join(traceback.format_exception(caught.value))
    assert fingerprint(target) == before and all(not path.exists() for path in scratch)
    assert scratch or stage == "allocate"


def test_raw_helper_rechecks_predicate_in_its_actual_write_transaction(tmp_path, monkeypatch):
    target, root = tmp_path / "target", tmp_path / "target/copy"
    offline(root, "empty", tmp_path)
    inspect, after_injection = backup._refuse_native_state, []
    def inspected_then_changed(*args):
        inspect(*args)
        with closing(sqlite3.connect(lodge_db.path(root))) as conn:
            conn.execute("INSERT INTO inbox_usage VALUES('',0,0,0)")
            conn.commit()
        after_injection.append(fingerprint(target))
    monkeypatch.setattr(backup, "_refuse_native_state", inspected_then_changed)
    report = {"untouched": "sentinel"}
    with pytest.raises(backup.RestoreRefused, match="safe_state_present"):
        call("helper", target, root, report)
    assert fingerprint(target) == after_injection[0] and report == {"untouched": "sentinel"}
