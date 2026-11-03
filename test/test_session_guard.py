"""sessions: a row the inbox owns takes only metadata from the registry's writers (RFC #615)."""

import json
import time

import pytest

from inbox_support import authority, enroll, legacy_write, ordered, rows, session

import lodge_db
import sessions

Refused = sessions.SessionWriteRefused
TABLES = ("sessions", "inbox_messages", "inbox_attempts", "inbox_events", "inbox_usage", "meta")


def snapshot(root):
    return [rows(root, f"SELECT * FROM {table} ORDER BY 1") for table in TABLES]


def stored(root, name="s1"):
    return json.loads(rows(root, "SELECT record FROM sessions WHERE name=?", name)[0][0])


@pytest.fixture
def owned(tmp_path, monkeypatch):
    """s1 enrolled with mail; s2 an ordinary session of the same wolt."""
    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k1", "session-name", "s1", "hi")
    session(tmp_path, "s2")
    return auth, binding, sessions.SessionRegistry(wolts_dir=tmp_path)


def _rename(reg):
    with reg.edit("s1") as data:
        data["name"] = "s3"  # a callback that moves the record to a free name


def _set(reg, key, value):
    with reg.edit("s1") as data:
        data[key] = value


def _drop(reg, key):
    with reg.edit("s1") as data:
        data.pop(key)


def _put(root, **change):
    with lodge_db.write(root) as conn:
        sessions.put_session(conn, {**stored(root), "name": "s1", **change})


@pytest.mark.parametrize("write", [
    lambda reg, root: reg.update("s1", status="stopped"),
    lambda reg, root: reg.update("s1", status="archived"),
    lambda reg, root: reg.update("s1", harness_session_id="conv-other"),
    lambda reg, root: _set(reg, "wolt", "nunu"),  # update(wolt=) is its filter, not a field
    lambda reg, root: reg.update("s1", workdir="/elsewhere", dir="/elsewhere"),
    lambda reg, root: reg.update("s1", target={"wolt_id": "nunu"}),
    lambda reg, root: reg.update("s1", runtime={"tmux": "x"}),
    lambda reg, root: reg.update("s1", pending_boot_prompt="go"),
    lambda reg, root: reg.update("s1", finished_at=None),  # missing is not null
    lambda reg, root: reg.update("s1", brand_new=1),  # an unknown field
    lambda reg, root: reg.update("s1", inbox_revision=9),
    lambda reg, root: reg.finish("s1", 0),
    lambda reg, root: reg.claim("s1", reg._read("pal", "s1"), "resting"),
    lambda reg, root: reg.create("s1", wolt="pal"),
    lambda reg, root: _rename(reg),
    lambda reg, root: _drop(reg, "harness"),
    lambda reg, root: _put(root, status="stopped"),
    lambda reg, root: _put(root, harness_session_id=None),
    lambda reg, root: reg.delete("s1"),
    lambda reg, root: reg.forget_wolt("pal"),
])
def test_a_legacy_write_to_an_owned_session_is_refused_and_changes_nothing(tmp_path, owned, write):
    auth, binding, reg = owned
    before = snapshot(tmp_path)
    with pytest.raises(Refused) as refused:
        write(reg, tmp_path)
    assert refused.value.code in ("owned_session", "binding_invalid")
    assert not isinstance(refused.value, (ValueError, lodge_db.sqlite3.IntegrityError))
    assert snapshot(tmp_path) == before  # s2 too: a bulk forget is all or nothing


def test_metadata_still_changes_and_only_metadata_is_stored(tmp_path, owned):
    auth, binding, reg = owned
    raw = stored(tmp_path)
    binding_row = rows(tmp_path, "SELECT inbox_incarnation, inbox_revision FROM sessions WHERE name='s1'")
    reg.describe("s1", "A title", "A summary")
    reg.set_viewport("s1", "/x.html")
    reg.update("s1", hold=1)
    assert reg.touch("s1")
    after = stored(tmp_path)
    unbenign = lambda record: {k: v for k, v in record.items() if k not in sessions.BENIGN_FIELDS}
    assert unbenign(after) == unbenign(raw)  # and no field of the read view is kept
    assert (after["title"], after["summary"], after["viewport_url"], after["hold"]) == (
        "A title", "A summary", "/x.html", 1)
    assert rows(tmp_path, "SELECT inbox_incarnation, inbox_revision FROM sessions WHERE name='s1'") == binding_row
    reg.update("s1", hold=None)
    assert stored(tmp_path)["hold"] is None
    assert reg.get("s1", check_alive=False)["target"]  # the read view still derives it
    assert auth.transition(binding.incarnation, 1, status="resting", readiness="resting")[0].revision == 2


def test_key_order_is_not_a_change_but_true_is_not_one(tmp_path, owned):
    auth, binding, reg = owned
    with lodge_db.write(tmp_path) as conn:  # an older writer's order, and a flag
        record = dict(reversed(list({**stored(tmp_path), "flag": True}.items())))
        conn.execute("UPDATE sessions SET record=? WHERE name='s1'", (json.dumps(record),))
    reg.describe("s1", "t", "s")
    _put(tmp_path, flag=True, title="t2")
    assert stored(tmp_path)["title"] == "t2"
    for flag in (1, None):
        with pytest.raises(Refused, match="owned_session"):
            _put(tmp_path, flag=flag)


def test_a_stale_full_record_cannot_bring_back_an_older_lifecycle(tmp_path, owned):
    auth, binding, reg = owned
    stale = reg._read("pal", "s1")
    auth.transition(binding.incarnation, 1, status="resting", readiness="resting")
    for write in (lambda: reg._write("pal", "s1", {**stale, "title": "new"}),
                  lambda: _put(tmp_path, **{**stale, "title": "new"})):
        with pytest.raises(Refused, match="owned_session"):
            write()
    assert rows(tmp_path, "SELECT status, json_extract(record, '$.status') FROM sessions WHERE name='s1'") == [
        ("resting", "resting")]


@pytest.mark.parametrize("marker", [
    dict(inbox_revision=3),  # a lone marker
    dict(inbox_incarnation="not-a-uuid"),
    dict(inbox_revision=0),
    dict(inbox_readiness="awake"),
    dict(inbox_conversation="conv-other"),  # disagrees with the row it froze
    dict(record='{"name": "s1", "created_at": "soon"}'),  # a record its columns cannot come from
    dict(record='{"name": "other", "wolt": "pal", "status": "running", "created_at": 1,'
                ' "harness": "claude", "harness_session_id": "conv-s1"}'),  # names another row
    dict(record='{"wolt": "pal", "status": "running", "created_at": 1,'
                ' "harness": "claude", "harness_session_id": "conv-s1"}'),  # names none
    dict(record='{"name": '),  # not JSON
    dict(record='[1]'),  # not an object
])
def test_a_partial_or_malformed_binding_is_not_an_ordinary_session(tmp_path, owned, marker):
    auth, binding, reg = owned
    column, value = next(iter(marker.items()))
    target = "s2" if column == "inbox_revision" and value == 3 else "s1"
    with lodge_db.write(tmp_path) as conn:
        conn.execute(f"UPDATE sessions SET {column}=? WHERE name=?", (value, target))
    before = snapshot(tmp_path)
    readable = column != "record" or value.startswith("{") and value.endswith("}")
    direct = [lambda: _put(tmp_path, title="t")] if target == "s1" and readable else []
    for write in (lambda: reg.describe(target, "t", "s"), lambda: reg.touch(target), *direct):
        with pytest.raises(Refused, match="binding_invalid"):
            write()
    for write in (lambda: reg.delete(target), lambda: reg.forget_wolt("pal")):
        with pytest.raises(Refused):  # a delete refuses whichever it is
            write()
    assert snapshot(tmp_path) == before


def test_an_ordinary_session_keeps_every_legacy_write_and_its_normal_writes(tmp_path, owned):
    auth, binding, reg = owned
    reg.update("s2", status="stopped", harness_session_id="conv-x")
    assert reg.finish("s2", 1)["status"] == "failed"
    reg.create("s4", wolt="pal")
    assert reg.delete("s4") and reg.touch("s2") is False  # not running: a no-op, as before
    with lodge_db.write(tmp_path) as conn:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        with pytest.raises(Refused, match="binding_invalid"):  # no record may carry the fields
            sessions.put_session(conn, {"name": "s5", "wolt": "pal", "inbox_incarnation": binding.incarnation})
    with pytest.raises(Refused, match="transaction_required"):
        sessions.put_session(lodge_db.connect(tmp_path), {"name": "s5", "wolt": "pal"})
    assert reg.forget_wolt("nobody") == 0
    session(tmp_path, "n1", wolt="nunu")
    assert reg.forget_wolt("nunu") == 1


def test_import_never_overwrites_an_owned_session(tmp_path, owned):
    auth, binding, reg = owned
    folder = tmp_path / "pal" / ".state" / "sessions"
    folder.mkdir(parents=True)
    (folder / "s1.json").write_text(json.dumps({"name": "s1", "wolt": "pal", "status": "stopped"}))
    before = snapshot(tmp_path)
    with lodge_db.write(tmp_path) as conn:
        counts, _ = sessions.import_sessions(conn, tmp_path)
    assert snapshot(tmp_path) == before and counts.get("imported", 0) == 0


def test_a_refusal_caught_inside_a_transaction_leaves_its_other_writes(tmp_path, owned):
    auth, binding, reg = owned
    with lodge_db.write(tmp_path) as conn:
        sessions.put_session(conn, {"name": "s6", "wolt": "pal", "status": "running"})
        with pytest.raises(Refused):
            sessions.put_session(conn, {**stored(tmp_path), "name": "s1", "status": "stopped"})
        sessions.put_session(conn, {"name": "s7", "wolt": "pal", "status": "running"})
    assert rows(tmp_path, "SELECT name, status FROM sessions ORDER BY name") == [
        ("s1", "running"), ("s2", "running"), ("s6", "running"), ("s7", "running")]


@pytest.mark.parametrize("first", ["enroll", "write"])
@pytest.mark.parametrize("write, effect, refused", [
    (("registry.update", ["s1"], {"status": "stopped"}), ("stopped",), "target_ended"),
    (("registry.delete", ["s1"], {}), (), "no_target"),
])
def test_enrollment_and_a_legacy_write_in_two_processes_take_the_lock_in_turn(
        tmp_path, first, write, effect, refused):
    session(tmp_path, "s1")
    enrolling = ("enroll", ["s1"], dict(harness="claude", conversation="conv-s1", readiness="running",
                                        generation="g1"))
    results = ordered(*((enrolling, write) if first == "enroll" else (write, enrolling)),
                      tmp_path / "pause", tmp_path)
    enrolled, written = results if first == "enroll" else results[::-1]
    if first == "enroll":
        assert "ok" in enrolled and written == {"refused": "owned_session"}
        assert rows(tmp_path, "SELECT status, inbox_revision FROM sessions") == [("running", 1)]
    else:  # the enrollment judged the row the write left
        assert "ok" in written and enrolled == {"refused": refused}
        assert [r[0] for r in rows(tmp_path, "SELECT status FROM sessions")] == list(effect)
        assert rows(tmp_path, "SELECT count(*) FROM sessions WHERE inbox_incarnation IS NOT NULL") == [(0,)]


def test_higher_callers_report_the_refusal_and_some_act_before_it(tmp_path, owned, monkeypatch):
    """The guard is on the stored row only: what a caller did to tmux first stays done."""
    auth, binding, reg = owned
    stops = []
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: True)
    monkeypatch.setattr(sessions, "_tmux_stop", lambda data: stops.append(data["name"]) or True)
    monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())
    before = snapshot(tmp_path)
    for call in (sessions.stop_session, sessions.archive_session, sessions.delete_session):
        with pytest.raises(Refused, match="owned_session"):
            call("s1")
    assert stops == ["s1", "s1", "s1"]  # each stopped tmux, then could not record it
    settle = []
    assert pytest.raises(Refused, sessions.stop_and_settle, reg, "s1", reg._read("pal", "s1"),
                         lambda seen: settle.append("stop"), lambda record: None)
    assert settle == []  # its claim comes first: refused before the stop
    with pytest.raises(Refused):
        reg.reconcile()  # running s1 with no tmux would be marked orphaned
    assert snapshot(tmp_path) == before
    legacy_write(tmp_path, "s1", status="resting")  # what an unguarded older build could still do
    assert rows(tmp_path, "SELECT status FROM sessions WHERE name='s1'") == [("resting",)]


def test_a_fault_inside_a_nested_registry_write_is_a_fixed_code_too(tmp_path, owned, monkeypatch):
    auth, binding, reg = owned
    real = sessions._fetch
    caught = []
    with reg.edit("s2") as outer:  # a nested call reuses this transaction
        def fail(conn, sql, args):
            raise lodge_db.sqlite3.OperationalError("SENTINEL_NESTED")
        monkeypatch.setattr(sessions, "_fetch", fail)
        try:
            reg.describe("s1", "t", "s")
        except Exception as exc:  # a caller that carries on
            caught.append(exc)
        monkeypatch.setattr(sessions, "_fetch", real)
        outer["title"] = "still mine"
    assert isinstance(caught[0], sessions.SessionStoreUnavailable)
    assert "SENTINEL" not in "".join(__import__("traceback").format_exception(caught[0]))
    assert stored(tmp_path, "s2")["title"] == "still mine"


def test_only_a_real_uniqueness_conflict_stays_an_integrity_error(tmp_path, owned):
    auth, binding, reg = owned
    with pytest.raises(lodge_db.sqlite3.IntegrityError, match="session_integrity"):  # resume-id retry
        reg.update("s2", harness_session_id="conv-s1")
    caught = []
    with reg.edit("s1") as outer:  # the same conflict, caught inside an outer write
        try:
            reg.update("s2", harness_session_id="conv-s1")
        except lodge_db.sqlite3.IntegrityError as exc:
            caught.append(exc)
        outer["title"] = "kept"
    assert caught[0].sqlite_errorname == "SQLITE_CONSTRAINT_UNIQUE" and stored(tmp_path)["title"] == "kept"
    with pytest.raises(lodge_db.sqlite3.IntegrityError, match="session_integrity"):  # and escaping it
        with reg.edit("s1"):
            reg.update("s2", harness_session_id="conv-s1")
    with lodge_db.write(tmp_path) as conn:
        conn.execute("CREATE TRIGGER fail BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'SENTINEL_T'); END")
    with pytest.raises(sessions.SessionStoreUnavailable) as failed:  # not a conflict to retry on
        reg.update("s2", title="t")
    assert "SENTINEL" not in "".join(__import__("traceback").format_exception(failed.value))
