"""inbox_binding: enrolled sessions, and which one a bound request reaches, for good."""

import sqlite3
import uuid
from contextlib import closing

import pytest

from inbox_support import accept, authority, enroll, legacy_write, ordered, rows, session, state, usage

import inbox
import inbox_binding
import lodge_db
import sessions


def written(root):
    return [rows(root, f"SELECT count(*) FROM {t}")[0][0]
            for t in ("inbox_messages", "inbox_events", "inbox_usage")]


def inbox_columns(root, name):
    return rows(root, "SELECT inbox_incarnation, inbox_harness, inbox_conversation, inbox_readiness,"
                      " inbox_generation, inbox_revision FROM sessions WHERE name=?", name)


def test_enrollment_makes_its_own_incarnation_and_freezes_the_session_it_found(tmp_path):
    auth = authority(tmp_path)
    session(tmp_path, "s1")
    record = rows(tmp_path, "SELECT record FROM sessions")
    binding = auth.enroll("s1", harness="claude", conversation="conv-s1", readiness="running",
                          generation="g1")
    assert uuid.UUID(binding.incarnation).version == 4
    assert (binding.name, binding.harness, binding.conversation, binding.status, binding.revision) == (
        "s1", "claude", "conv-s1", "running", 1)
    assert auth.enroll("s1", harness="claude", conversation="conv-s1", readiness="running",
                       generation="g1") == binding  # the same again: no second slot
    assert rows(tmp_path, "SELECT value FROM meta WHERE key='inbox:enrollments'") == [("1",)]
    assert rows(tmp_path, "SELECT record FROM sessions") == record
    for change in (dict(readiness="resting"), dict(generation="g2")):
        with pytest.raises(inbox.Refused, match="already_enrolled"):
            auth.enroll("s1", **{**dict(harness="claude", conversation="conv-s1", readiness="running",
                                        generation="g1"), **change})


@pytest.mark.parametrize("row, ask, code", [
    (None, {}, "no_target"),
    ({}, dict(conversation="conv-other"), "binding_mismatch"),
    ({}, dict(harness="codex"), "binding_mismatch"),
    (dict(status="stopped"), {}, "target_ended"),
    (dict(status="failed"), {}, "target_ended"),
    (dict(status="archived"), {}, "target_ended"),
    (dict(status=sessions.STOPPING), {}, "target_stopping"),
    ({}, dict(readiness="awake"), "Invalid"),
    ({}, dict(generation="g" * 41), "Invalid"),
])
def test_enrollment_refuses_what_it_cannot_bind_and_writes_nothing(tmp_path, row, ask, code):
    auth = authority(tmp_path)
    if row is not None:
        session(tmp_path, "s1", **row)
    lodge_db.connect(tmp_path).close()
    before = rows(tmp_path, "SELECT * FROM sessions"), rows(tmp_path, "SELECT * FROM meta")
    with pytest.raises(inbox.InboxError) as caught:
        auth.enroll("s1", **{**dict(harness="claude", conversation="conv-s1", readiness="running",
                                    generation="g1"), **ask})
    assert getattr(caught.value, "code", type(caught.value).__name__) == code
    assert (rows(tmp_path, "SELECT * FROM sessions"), rows(tmp_path, "SELECT * FROM meta")) == before


def test_enrollments_are_counted_for_good_and_refused_past_the_limit(tmp_path):
    auth = authority(tmp_path, enrollments=2)
    enroll(auth, "s1")
    with lodge_db.write(tmp_path) as conn:
        conn.execute("DELETE FROM sessions WHERE name='s1'")
    first = enroll(auth, "s1")  # made again: a new incarnation, and a second slot
    with pytest.raises(inbox.Refused, match="enrollments"):
        enroll(auth, "s2")
    assert inbox_columns(tmp_path, "s2") == [(None,) * 6]
    assert rows(tmp_path, "SELECT inbox_incarnation FROM sessions ORDER BY name") == [(first.incarnation,), (None,)]


def test_a_wolt_request_takes_the_newest_eligible_session_and_never_moves(tmp_path):
    auth = authority(tmp_path)
    picked = enroll(auth, "b", last_activity=20, created_at=2)
    enroll(auth, "c", last_activity=20, created_at=2)  # a tie: the name decides
    early = enroll(auth, "a", last_activity=20, created_at=1)
    session(tmp_path, "d", last_activity=90)  # not enrolled
    enroll(auth, "e", last_activity=80)
    legacy_write(tmp_path, "e", status="stopped")
    enroll(auth, "f", last_activity=70, readiness="unknown")
    enroll(auth, "g", last_activity=60, wolt="nunu")
    first = auth.admit("k1", "wolt", "pal", "hi")
    assert (first.recipient, first.conversation) == (picked.incarnation, "conv-b")
    session(tmp_path, "a", last_activity=99)
    assert auth.admit("k1", "wolt", "pal", "hi") == first
    assert auth.admit("k2", "wolt", "pal", "hi").recipient == early.incarnation
    by_name, by_incarnation = (auth.admit("k3", "session-name", "b", "hi"),
                               auth.admit("k4", "incarnation", picked.incarnation, "hi"))
    assert {(s.recipient, s.conversation) for s in (by_name, by_incarnation)} == {
        (picked.incarnation, "conv-b")}
    assert rows(tmp_path, "SELECT request_kind, request_address, bound_harness FROM inbox_messages"
                          " ORDER BY seq") == [("wolt", "pal", "claude"), ("wolt", "pal", "claude"),
                                               ("session-name", "b", "claude"),
                                               ("incarnation", picked.incarnation, "claude")]


def _ended(tmp_path, auth):
    legacy_write(tmp_path, "s1", status="completed")


def _recreated(tmp_path, auth):
    with lodge_db.write(tmp_path) as conn:
        conn.execute("DELETE FROM sessions WHERE name='s1'")
    session(tmp_path, "s1")  # the same name, not enrolled


@pytest.mark.parametrize("change, by_name, by_incarnation", [
    (lambda root, auth: legacy_write(root, "s1", status=sessions.STOPPING), "target_stopping", "target_stopping"),
    (_ended, "target_ended", "target_ended"),
    (lambda root, auth: legacy_write(root, "s1", status="archived"), "target_ended", "target_ended"),
    (lambda root, auth: legacy_write(root, "s1", status="orphaned"), "target_unknown", "target_unknown"),
    (lambda root, auth: legacy_write(root, "s1", status="resting"), "target_unknown", "target_unknown"),
    (lambda root, auth: legacy_write(root, "s1", conversation="conv-new"), "binding_mismatch", "binding_mismatch"),
    (lambda root, auth: legacy_write(root, "s1", harness="codex"), "binding_mismatch", "binding_mismatch"),
    (_recreated, "target_unbound", "no_target"),
])
def test_a_new_key_for_a_session_that_cannot_take_mail_is_refused_whole(tmp_path, change, by_name,
                                                                         by_incarnation):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    change(tmp_path, auth)
    for kind, address, code in (("session-name", "s1", by_name),
                                ("incarnation", binding.incarnation, by_incarnation),
                                ("wolt", "pal", "no_target")):
        with pytest.raises(inbox.Refused, match=code):
            auth.admit("k1", kind, address, "hi")
    assert written(tmp_path) == [0, 0, 0]


def test_a_resting_session_on_its_own_conversation_takes_mail(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1", status="resting", readiness="resting")
    assert auth.admit("k1", "wolt", "pal", "hi").recipient == binding.incarnation
    with pytest.raises(inbox.Refused, match="no_target"):
        auth.admit("k2", "session-name", "nobody", "hi")
    session(tmp_path, "s2")
    with pytest.raises(inbox.Refused, match="target_unbound"):
        auth.admit("k2", "session-name", "s2", "hi")


@pytest.mark.parametrize("change", ["activity", "ended", "deleted", "full", "purged"])
def test_a_retry_returns_the_original_whatever_became_of_its_session(tmp_path, change):
    auth = authority(tmp_path)
    enroll(auth, "s1", last_activity=20)
    enroll(auth, "s2", last_activity=10)
    first = auth.admit("k1", "wolt", "pal", "héllo")
    if change == "activity":
        session(tmp_path, "s2", last_activity=99)
    elif change == "ended":
        legacy_write(tmp_path, "s1", status="stopped")
    elif change == "deleted":
        with lodge_db.write(tmp_path) as conn:
            conn.execute("DELETE FROM sessions WHERE name='s1'")
    elif change == "full":
        auth = authority(tmp_path, keys=1, pending=1, recipient_pending=1)
        with pytest.raises(inbox.Refused, match="keys"):
            auth.admit("k2", "wolt", "pal", "hi")
    else:
        accept(auth.store, first.recipient, first.conversation)
        first = auth.store.status("k1")
    before = rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path)
    assert auth.admit("k1", "wolt", "pal", "héllo") == first
    for kind, address, body in (("wolt", "nunu", "héllo"), ("session-name", "s1", "héllo"),
                                ("incarnation", first.recipient, "héllo"), ("wolt", "pal", "hello")):
        with pytest.raises(inbox.Conflict):
            auth.admit("k1", kind, address, body)
    with pytest.raises(inbox.Conflict):  # the direct API under a bound key, even to the same place
        auth.store.admit("k1", first.recipient, first.conversation, "héllo")
    assert (rows(tmp_path, "SELECT * FROM inbox_events"), usage(tmp_path)) == before


def test_both_apis_share_one_key_space_and_a_bound_session_only_takes_bound_mail(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    direct = auth.store.admit("k1", "s1", "conv-s1", "hi")
    with pytest.raises(inbox.Conflict):
        auth.admit("k1", "session-name", "s1", "hi")
    with pytest.raises(inbox.Refused, match="bound_recipient"):
        auth.store.admit("k2", binding.incarnation, "conv-s1", "hi")
    assert auth.store.admit("k1", "s1", "conv-s1", "hi") == direct
    assert rows(tmp_path, "SELECT key, request_kind FROM inbox_messages") == [("k1", None)]


@pytest.mark.parametrize("outer", ["lodge_write", "registry"])
def test_bound_calls_refuse_to_run_inside_another_write(tmp_path, outer):
    auth = authority(tmp_path)
    enroll(auth, "s1")
    registry = sessions.SessionRegistry(wolts_dir=tmp_path)
    with (lodge_db.write(tmp_path) if outer == "lodge_write" else registry._tx()):
        for call in (lambda: auth.admit("k1", "wolt", "pal", "hi"),
                     lambda: auth.enroll("s1", harness="claude", conversation="conv-s1",
                                         readiness="running", generation="g1")):
            with pytest.raises(inbox.Refused, match="transaction_conflict"):
                call()
    assert written(tmp_path) == [0, 0, 0]


@pytest.mark.parametrize("bad", [
    dict(kind="name"), dict(address=""), dict(address="a" * 41), dict(key="k" * 41), dict(body=b"hi"),
])
def test_a_malformed_bound_request_is_refused_before_the_store_is_touched(tmp_path, bad):
    auth = authority(tmp_path)
    with pytest.raises(inbox.Invalid):
        auth.admit(**{**dict(key="k1", kind="wolt", address="pal", body="hi"), **bad})
    assert not lodge_db.path(tmp_path).exists()
    for incarnation, revision in (("x" * 37, 1), ("", 1), (str(uuid.uuid4()), 0), (str(uuid.uuid4()), True)):
        with pytest.raises(inbox.Invalid):
            auth.delete(incarnation, revision)
        with pytest.raises(inbox.Invalid):
            auth.transition(incarnation, revision, status="running", readiness="running")
    assert not lodge_db.path(tmp_path).exists()
    with pytest.raises(inbox.Invalid, match="incarnation"):
        authority(tmp_path, name_chars=35)
    with pytest.raises(inbox.Invalid):
        inbox_binding.BindingLimits(0, 40)


def test_bound_admission_reads_through_indexes_not_whole_tables(tmp_path, monkeypatch):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    statements, connect = [], lodge_db.connect

    def traced(*args, **kw):
        conn = connect(*args, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(lodge_db, "connect", traced)
    for kind, address in (("wolt", "pal"), ("session-name", "s1"), ("incarnation", binding.incarnation)):
        auth.admit(f"k-{kind}", kind, address, "hi")
    reads = [s for s in statements if s.startswith("SELECT") and ("sessions" in s or "inbox_messages" in s)]
    with closing(sqlite3.connect(lodge_db.path(tmp_path))) as conn:
        plans = [" ".join(r[3] for r in conn.execute(f"EXPLAIN QUERY PLAN {s}")) for s in reads]
    assert plans and not [p for p in plans if "SCAN" in p], plans
    assert any("sessions_inbox_pick" in p for p in plans)


def test_one_key_admitted_by_two_processes_at_once_is_one_message(tmp_path):
    auth = authority(tmp_path)
    enroll(auth, "s1")
    op = ("admit", ["k1", "wolt", "pal", "hi"], {})
    first, second = ordered(op, op, tmp_path / "pause", tmp_path)
    assert first == second and "ok" in first
    assert rows(tmp_path, "SELECT count(*) FROM inbox_messages") == [(1,)]
    assert usage(tmp_path)[""] == (1, 2, 1)


def test_the_last_enrollment_slot_goes_to_one_process_only(tmp_path):
    session(tmp_path, "s1")
    session(tmp_path, "s2")
    ask = dict(harness="claude", readiness="running", generation="g1")
    first, second = ordered(("enroll", ["s1"], {**ask, "conversation": "conv-s1"}),
                            ("enroll", ["s2"], {**ask, "conversation": "conv-s2"}),
                            tmp_path / "pause", tmp_path, enrollments=1)
    assert "ok" in first and second == {"refused": "enrollments"}
    assert rows(tmp_path, "SELECT name FROM sessions WHERE inbox_incarnation IS NOT NULL") == [("s1",)]



# -- lifecycle ----------------------------------------------------------------

def end(auth, binding, how):
    if how == "delete":
        return auth.delete(binding.incarnation, binding.revision)
    return auth.transition(binding.incarnation, binding.revision, status=how, readiness="unknown")[1]


def delete_row(root):
    with lodge_db.write(root) as conn:
        conn.execute("DELETE FROM sessions WHERE name='s1'")


def test_a_transition_needs_the_current_revision_and_activity_is_not_one(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    session(tmp_path, "s1", last_activity=50)  # the registry's own updates: activity, a hold
    with lodge_db.write(tmp_path) as conn:
        conn.execute("UPDATE sessions SET record=json_set(record, '$.hold', 1)")
    auth.admit("k1", "session-name", "s1", "hi")
    assert auth.store.claim(binding.incarnation, "conv-s1", "g1").key == "k1"
    rested, _ = auth.transition(binding.incarnation, 1, status="resting", readiness="resting")
    assert (rested.status, rested.readiness, rested.revision) == ("resting", "resting", 2)
    assert rows(tmp_path, "SELECT status, json_extract(record, '$.status'), json_extract(record, '$.hold')"
                          " FROM sessions") == [("resting", "resting", 1)]
    for revision in (1, 3):
        with pytest.raises(inbox.Refused, match="stale_revision"):
            auth.transition(binding.incarnation, revision, status="running", readiness="running")
    with pytest.raises(inbox.Refused, match="no_target"):
        auth.transition(str(uuid.uuid4()), 1, status="running", readiness="running")
    with pytest.raises(inbox.Invalid):
        auth.transition(binding.incarnation, 2, status="archived", readiness="running")
    with lodge_db.write(tmp_path) as conn:
        conn.execute("UPDATE sessions SET inbox_revision=?", (2**63 - 1,))
    with pytest.raises(inbox.Refused, match="revision_overflow"):
        auth.transition(binding.incarnation, 2**63 - 1, status="running", readiness="running")


def test_resting_mail_is_held_unsent_until_the_same_conversation_runs_again(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1", status="resting", readiness="resting")
    receipt = auth.admit("k1", "wolt", "pal", "hi")
    assert (receipt.state, receipt.reason) == ("blocked", "target_resting")  # one commit with the hold
    assert auth.admit("k1", "wolt", "pal", "hi") == receipt
    assert auth.store.claim(binding.incarnation, "conv-s1", "g1") is None
    for change in (dict(status="running"), dict(status="resting", predecessor_fenced=True)):
        with pytest.raises(inbox.Refused, match="generation_unfenced"):
            auth.transition(binding.incarnation, 1, **{"readiness": "running", "generation": "g2", **change})
    woken, settled = auth.transition(binding.incarnation, 1, status="running", readiness="running",
                                     generation="g2", predecessor_fenced=True)
    assert (woken.generation, woken.conversation) == ("g2", "conv-s1")
    assert [(s.key, s.state, s.reason) for s in settled] == [("k1", "queued", None)]
    assert auth.store.claim(binding.incarnation, "conv-s1", "g1") is None  # the old generation
    attempt = auth.store.claim(binding.incarnation, "conv-s1", "g2")
    assert rows(tmp_path, "SELECT generation, revision FROM inbox_attempts") == [("g2", 2)]
    assert auth.store.record(attempt, inbox.Outcome.ACCEPTED).state == "native_accepted"


def test_lifecycle_holds_follow_the_session_and_only_they_are_released(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    inc = binding.incarnation
    for key in ("k1", "k2", "k3"):
        auth.admit(key, "session-name", "s1", key)
    auth.store.record(auth.store.claim(inc, "conv-s1", "g1"), inbox.Outcome.USER_STOP)
    revision, seen = 1, []
    for status, readiness in ((sessions.STOPPING, "unknown"), ("running", "unknown"), ("resting", "resting"),
                              ("running", "running")):
        binding, settled = auth.transition(inc, revision, status=status, readiness=readiness)
        revision = binding.revision
        seen.append([(s.key, s.state, s.reason) for s in settled])
    assert seen == [[("k2", "blocked", "target_stopping"), ("k3", "blocked", "target_stopping")],
                    [("k2", "blocked", "target_unknown"), ("k3", "blocked", "target_unknown")],
                    [("k2", "blocked", "target_resting"), ("k3", "blocked", "target_resting")],
                    [("k2", "queued", None), ("k3", "queued", None)]]
    assert state(tmp_path, "k1") == [("blocked", "user_stop", 1, 0)]  # never released
    assert auth.store.status("k2").state == "queued"
    assert auth.store.claim(inc, "conv-s1", "g1") is None  # k1, stopped by the user, still holds them
    assert rows(tmp_path, "SELECT state, reason FROM inbox_events WHERE seq=? ORDER BY cursor",
                auth.store.status("k3").seq) == [
        ("queued", None), ("blocked", "target_stopping"), ("blocked", "target_unknown"),
        ("blocked", "target_resting"), ("queued", None)]
    binding, settled = auth.transition(inc, revision, status="resting", readiness="resting")
    assert [s.reason for s in settled] == ["target_resting", "target_resting"]
    assert [(s.key, s.state, s.reason) for s in end(auth, binding, "stopped")] == [
        ("k2", "blocked", "target_ended"), ("k3", "blocked", "target_ended")]
    assert state(tmp_path, "k1") == [("blocked", "user_stop", 1, 0)]


def test_a_repeated_transition_changes_nothing_and_a_fenced_verdict_must_be_true(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k1", "wolt", "pal", "hi")
    attempt = auth.store.claim(binding.incarnation, "conv-s1", "g1")
    events = rows(tmp_path, "SELECT * FROM inbox_events")
    assert auth.transition(binding.incarnation, 1, status="running", readiness="running") == (binding, [])
    assert rows(tmp_path, "SELECT * FROM inbox_events") == events
    for verdict in ("yes", 1):
        with pytest.raises(inbox.Refused, match="generation_unfenced"):
            auth.transition(binding.incarnation, 1, status="running", readiness="running", generation="g2",
                            predecessor_fenced=verdict)
    assert auth.store.record(attempt, inbox.Outcome.ACCEPTED).state == "native_accepted"


@pytest.mark.parametrize("change", [
    lambda root: legacy_write(root, "s1", conversation="conv-new"),  # an unsupported writer
    lambda root: legacy_write(root, "s1", status="orphaned"),
    delete_row,
])
def test_a_claim_sends_bound_mail_only_to_its_session_as_enrolled(tmp_path, change):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k1", "session-name", "s1", "hi")
    change(tmp_path)
    for recipient in (binding.incarnation, "s1"):
        assert auth.store.claim(recipient, "conv-s1", "g1") is None
    assert auth.store.reconcile() == [(binding.incarnation, "conv-s1")]
    assert state(tmp_path, "k1") == [("queued", None, 0, 0)]


@pytest.mark.parametrize("kind", ["wolt", "session-name"])
@pytest.mark.parametrize("how", ["stopped", "completed", "delete"])
def test_an_ended_session_keeps_its_mail_blocked_and_its_settled_mail_as_it_was(tmp_path, kind, how):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    inc = binding.incarnation
    for key in ("k1", "k2", "k3", "k4"):
        auth.admit(key, kind, "pal" if kind == "wolt" else "s1", key)
    accept(auth.store, inc, "conv-s1")
    auth.store.record(auth.store.claim(inc, "conv-s1", "g1"), inbox.Outcome.USER_STOP)
    settled = end(auth, binding, how)
    assert [(s.key, s.state, s.reason) for s in settled] == [("k3", "blocked", "target_ended"),
                                                             ("k4", "blocked", "target_ended")]
    assert rows(tmp_path, "SELECT key, state, reason, body FROM inbox_messages ORDER BY seq") == [
        ("k1", "native_accepted", None, None), ("k2", "blocked", "user_stop", b"k2"),
        ("k3", "blocked", "target_ended", b"k3"), ("k4", "blocked", "target_ended", b"k4")]
    assert auth.admit("k3", kind, "pal" if kind == "wolt" else "s1", "k3") == settled[0]
    assert usage(tmp_path)[inc] == (3, 6, 4)
    with pytest.raises(inbox.Refused, match="no_target" if how == "delete" else "target_ended"):
        auth.transition(inc, binding.revision + 1, status="running", readiness="running")
    if how != "delete":  # an ended session can still be deleted; its mail stays as it is
        assert auth.delete(inc, binding.revision + 1) == []
        assert rows(tmp_path, "SELECT count(*) FROM sessions") == [(0,)]
        assert state(tmp_path, "k3") == [("blocked", "target_ended", 0, 0)]
    if how == "delete":  # the name again is a new session: none of the old mail moves to it
        again = enroll(auth, "s1")
        assert again.incarnation != inc
        auth.admit("k5", "session-name", "s1", "new")
        assert rows(tmp_path, "SELECT key FROM inbox_messages WHERE recipient=?", again.incarnation) == [
            ("k5",)]
        assert auth.store.claim(inc, "conv-s1", "g1") is None


def test_an_archived_session_is_never_reopened_and_keeps_its_mail(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    first = auth.admit("k1", "session-name", "s1", "hi")
    legacy_write(tmp_path, "s1", status="archived")  # what archive_session stored before the guard
    before = rows(tmp_path, "SELECT * FROM sessions"), rows(tmp_path, "SELECT * FROM inbox_events")
    for status in ("running", "resting"):
        with pytest.raises(inbox.Refused, match="target_ended"):
            auth.transition(binding.incarnation, 1, status=status, readiness=status)
    with pytest.raises(inbox.Invalid):  # not a destination either
        auth.transition(binding.incarnation, 1, status="archived", readiness="unknown")
    assert (rows(tmp_path, "SELECT * FROM sessions"), rows(tmp_path, "SELECT * FROM inbox_events")) == before
    assert auth.store.claim(binding.incarnation, "conv-s1", "g1") is None
    assert auth.admit("k1", "session-name", "s1", "hi") == first
    assert [s.reason for s in auth.delete(binding.incarnation, 1)] == ["target_ended"]
    assert state(tmp_path, "k1") == [("blocked", "target_ended", 0, 0)]


def test_a_call_in_flight_when_its_session_changes_is_uncertain_and_its_late_answer_stale(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k1", "wolt", "pal", "hi")
    attempt = auth.store.claim(binding.incarnation, "conv-s1", "g1")
    _, settled = auth.transition(binding.incarnation, 1, status=sessions.STOPPING, readiness="unknown")
    assert [(s.state, s.reason) for s in settled] == [("uncertain", "target_changed")]
    with pytest.raises(inbox.Stale):
        auth.store.record(attempt, inbox.Outcome.ACCEPTED)
    assert rows(tmp_path, "SELECT outcome FROM inbox_attempts") == [("unknown",)]
    assert state(tmp_path, "k1") == [("uncertain", "target_changed", 1, 0)]
    auth.transition(binding.incarnation, 2, status="running", readiness="running")
    assert auth.store.claim(binding.incarnation, "conv-s1", "g1") is None  # never sent again


def test_an_unsettled_binding_change_refuses_the_answer_and_reconcile_settles_it(tmp_path):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    auth.admit("k1", "wolt", "pal", "hi")
    attempt = auth.store.claim(binding.incarnation, "conv-s1", "g1")
    delete_row(tmp_path)  # a legacy writer, not the authority
    with pytest.raises(inbox.Stale):
        auth.store.record(attempt, inbox.Outcome.ACCEPTED)
    assert state(tmp_path, "k1") == [("attempting", None, 1, 0)]  # the body stays
    auth.store.reconcile()
    assert state(tmp_path, "k1") == [("uncertain", "interrupted", 1, 0)]


@pytest.mark.parametrize("first", ["admit", "end"])
def test_admission_and_end_in_two_processes_take_the_lock_in_turn(tmp_path, first):
    auth = authority(tmp_path)
    binding = enroll(auth, "s1")
    admit = ("admit", ["k1", "session-name", "s1", "hi"], {})
    stop = ("transition", [binding.incarnation, 1], dict(status="stopped", readiness="unknown"))
    results = ordered(*((admit, stop) if first == "admit" else (stop, admit)), tmp_path / "pause", tmp_path)
    admitted, ended = results if first == "admit" else results[::-1]
    assert "ok" in ended
    if first == "admit":
        assert admitted["ok"]["state"] == "queued"
        assert ended["ok"][1][0]["key"] == "k1"
        assert state(tmp_path, "k1") == [("blocked", "target_ended", 0, 0)]
        assert usage(tmp_path)[binding.incarnation] == (1, 2, 1)
    else:
        assert admitted == {"refused": "target_ended"}
        assert written(tmp_path) == [0, 0, 0]
    assert rows(tmp_path, "SELECT status, inbox_revision FROM sessions") == [("stopped", 2)]
