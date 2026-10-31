"""The agent lifecycle lock, the launch marker and its publication, and the inbox
wake (lifecycle.py, sessions.py, inbox_wake.py): real lodge.db, real flock, a
fake tmux and a fake native transport."""

import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from inbox_support import limits, rows, wolt

import inbox
import inbox_wake
import lifecycle
import sessions
from session_runtime import RuntimeHandle

LOCK = lifecycle._HELD


def held(root, name="pal"):
    """True while this thread holds the agent's lifecycle lock."""
    key = str(Path(root) / ".space" / "run" / "lifecycle" / f"{name}.lock")
    return bool(LOCK.__dict__.get("counts", {}).get(key))


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """A lodge with the inbox on, the wolt pal (auto policy), a fake tmux and transport."""
    (tmp_path / ".space").mkdir()
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")  # auto policy, whatever ran before
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True, "native_hints": True}}))
    wolt(tmp_path, "pal")
    for module, attr in ((sessions, "WOLTS_DIR"),):
        monkeypatch.setattr(module, attr, tmp_path)
    calls = types.SimpleNamespace(spawned=[], stopped=[], delivered=[], prepared=[], retired=[])

    def spawn(name, cwd, command):
        record = sessions.SessionRegistry(tmp_path).get(name, check_alive=False)
        calls.spawned.append((name, dict(record.get("launching") or {}), held(tmp_path)))
        return RuntimeHandle(woltspace_session_id=name, tmux_session_name=name, pane_id="%1")

    monkeypatch.setattr(sessions, "_tmux_spawn", spawn)
    monkeypatch.setattr(sessions, "_tmux_stop", lambda data: calls.stopped.append(data) or True)
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: True)
    monkeypatch.setattr(sessions, "harness_install_problem", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "ensure_site", lambda wolt: None)
    monkeypatch.setattr(sessions, "get_tunnel_url", lambda: "")
    native = types.SimpleNamespace(
        prepare=lambda root, name, harness, attempt: calls.prepared.append(attempt) or {
            "kind": harness, "addr": str(root / ".space" / name / attempt), "attempt": attempt},
        retire=lambda locator: calls.retired.append(locator["attempt"]),
        deliver=lambda record, hint: calls.delivered.append((record.get("name"), hint, held(tmp_path)))
        or ("submitted", "ok"))
    monkeypatch.setitem(sys.modules, "native_adapters", native)
    lodge = types.SimpleNamespace(root=tmp_path, calls=calls, registry=sessions.SessionRegistry(tmp_path),
                                  store=inbox.Store(tmp_path, limits()))
    monkeypatch.setattr(inbox, "LIMITS", limits())
    return lodge


def record(lodge, name):
    return lodge.registry.get(name, check_alive=False)


# -- the lock -------------------------------------------------------------------

def test_the_lock_is_reentrant_and_another_process_waits_for_it(lodge):
    holder = ("import fcntl, os, sys, time; fd = os.open(sys.argv[1], os.O_RDWR);"
              " fcntl.flock(fd, fcntl.LOCK_EX); print('held', flush=True); time.sleep(30)")
    with lifecycle.agent_lock(lodge.root, "pal"):
        with lifecycle.agent_lock(lodge.root, "pal"):
            assert held(lodge.root)
        assert held(lodge.root)
    assert not held(lodge.root)
    path = lodge.root / ".space" / "run" / "lifecycle" / "pal.lock"
    other = subprocess.Popen([sys.executable, "-c", holder, str(path)], stdout=subprocess.PIPE, text=True)
    try:
        assert other.stdout.readline().strip() == "held"
        with pytest.raises(lifecycle.LifecycleBusy):
            with lifecycle.agent_lock(lodge.root, "pal", wait=0.3):
                pass
        with lifecycle.agent_lock(lodge.root, "nunu", wait=0.3):  # per agent
            pass
    finally:
        other.kill()
        other.wait()
    with lifecycle.agent_lock(lodge.root, "pal", wait=0.3):
        pass


def test_a_child_started_under_the_lock_does_not_keep_it(lodge):
    with lifecycle.agent_lock(lodge.root, "pal"):
        child = subprocess.Popen(["sleep", "30"])
    try:
        probe = subprocess.run([sys.executable, "-c", (
            "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR);"
            " fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"),
            str(lodge.root / ".space" / "run" / "lifecycle" / "pal.lock")], timeout=10)
        assert probe.returncode == 0
    finally:
        child.kill()
        child.wait()


# -- the launch marker and its publication ----------------------------------------

def test_a_start_marks_the_attempt_before_the_spawn_and_publishes_it_in_one_write(lodge):
    started = sessions.start_session(wolt="pal", prompt="hi", name="pal-a-b-c")
    (name, marker, locked), = lodge.calls.spawned
    assert name == started["name"] == "pal-a-b-c" and locked
    assert marker["attempt"] and marker["native"]["attempt"] == marker["attempt"]
    assert Path(marker["native"]["addr"]).parent.name == name
    data = record(lodge, name)
    assert "launching" not in data and data["attempt"] == marker["attempt"]
    assert data["native"] == marker["native"] and data["runtime"]["pane_id"] == "%1"


def test_publication_needs_the_exact_attempt(lodge):
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    attempt = sessions.mark_launching(lodge.registry, "pal-a-b-c", "pal", "claude", "native")
    assert not sessions.publish_runtime(lodge.registry, "pal-a-b-c", "pal", "other", {"x": 1})
    assert record(lodge, "pal-a-b-c")["launching"]["attempt"] == attempt
    assert sessions.publish_runtime(lodge.registry, "pal-a-b-c", "pal", attempt, {"x": 1})
    assert not sessions.publish_runtime(lodge.registry, "pal-a-b-c", "pal", attempt, {"y": 2})
    assert record(lodge, "pal-a-b-c")["runtime"] == {"x": 1}


def test_with_the_inbox_off_there_is_no_locator_and_the_command_is_unchanged(lodge, monkeypatch):
    (lodge.root / "woltspace.json").write_text("{}")
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    assert lodge.calls.spawned[0][1] == {} and "native" not in record(lodge, "pal-a-b-c")  # no marker at all
    assert not lodge.calls.prepared


@pytest.fixture
def unresolved(lodge, monkeypatch):
    """A start whose spawn never returned: the attempt stays marked."""
    def lost(name, cwd, command):
        raise subprocess.TimeoutExpired("tmux", 10)
    monkeypatch.setattr(sessions, "_tmux_spawn", lost)
    monkeypatch.setattr(sessions, "_tmux_exists", lambda where: None)  # tmux did not answer either
    with pytest.raises(subprocess.TimeoutExpired):
        sessions.start_session(wolt="pal", name="pal-a-b-c")
    assert record(lodge, "pal-a-b-c")["launching"]
    return "pal-a-b-c"


@pytest.mark.parametrize("end", [sessions.stop_session, sessions.archive_session, sessions.delete_session])
def test_an_unresolved_launch_is_cleaned_up_but_never_settled(lodge, unresolved, end):
    before = record(lodge, unresolved)
    with pytest.raises(lifecycle.LifecycleUnknown):
        end(unresolved)
    assert lodge.calls.stopped  # the cleanup was still attempted
    after = record(lodge, unresolved)
    assert after["status"] == before["status"] == "running" and after["launching"] == before["launching"]


def test_no_reaper_adoption_or_relaunch_settles_an_unresolved_launch(lodge, unresolved, monkeypatch):
    monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())
    assert lodge.registry.reconcile() == []
    report = lodge.registry.adopt_runtime_sessions()
    assert {"session": unresolved, "wolt": "pal", "reason": "unresolved launch"} in report["unchanged"]
    seen = record(lodge, unresolved)
    assert sessions.stop_and_settle(lodge.registry, unresolved, seen, lambda s: True,
                                    lambda r: r.update(status="stopped"), wolt="pal") is None
    with pytest.raises(sessions.ResumeFailed, match="unresolved launch"):
        with sessions._relaunch(lodge.registry, unresolved, "pal", "claude"):
            pass
    assert record(lodge, unresolved)["status"] == "running" and record(lodge, unresolved)["launching"]


def test_a_relaunch_is_a_new_attempt_and_its_wait_runs_after_the_lock(lodge, monkeypatch):
    first = sessions.start_session(wolt="pal", name="pal-a-b-c")["name"]
    old = record(lodge, first)
    waits = []
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    monkeypatch.setattr(sessions, "_await_agent", lambda *a, **k: waits.append(held(lodge.root)) or True)
    lodge.registry.update(first, harness_session_id="conv-1")
    assert sessions.resume_session(first)["status"] == "respawned"
    new = record(lodge, first)
    assert new["attempt"] != old["attempt"] and new["native"]["attempt"] == new["attempt"]
    assert lodge.calls.retired == [old["attempt"]]  # retired before the replacement
    assert lodge.calls.spawned[-1][2] and waits == [False]


def test_stop_retires_the_locator_before_tmux_stops(lodge, monkeypatch):
    name = sessions.start_session(wolt="pal", name="pal-a-b-c")["name"]
    order = []
    monkeypatch.setattr(sessions, "_tmux_stop", lambda data: order.append(("stop", list(lodge.calls.retired))))
    sessions.stop_session(name)
    assert order == [("stop", [record(lodge, name).get("attempt")])]
    assert record(lodge, name)["status"] == "stopped" and "native" not in record(lodge, name)


# -- the wake ---------------------------------------------------------------------

@pytest.fixture
def wake(lodge, monkeypatch):
    """The wake's view of sessions, controlled: live session and fake starts."""
    state = types.SimpleNamespace(live=None, starts=[])

    def start(**kw):
        state.starts.append((kw, held(lodge.root)))
        sessions.SessionRegistry(lodge.root).create(kw["name"], wolt=kw["wolt"])
        sessions.SessionRegistry(lodge.root).update(kw["name"], attempt="a1", runtime={"x": 1})
        return {"name": kw["name"]}
    monkeypatch.setattr(sessions, "resolve_active_session", lambda wolt, registry=None: state.live)
    monkeypatch.setattr(sessions, "start_session", start)
    return state


def mail(lodge, key="k1", to="pal", human=True, route=None):
    return lodge.store.admit("telegram", key, to, "hello", human=human, route=route)


def wake_state(lodge):
    return json.loads(rows(lodge.root, "SELECT wake FROM inbox_mailboxes WHERE wolt='pal'")[0][0] or "{}")


def test_no_session_reserves_and_starts_one_with_the_fixed_fetch_instruction(lodge, wake):
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    (kw, locked), = wake.starts
    assert locked and kw["prompt"] == inbox_wake.START.format(sender=" from your human") and "hello" not in kw["prompt"]
    assert wake_state(lodge)["reserved"]["session"] == kw["name"]
    assert rows(lodge.root, "SELECT state FROM inbox_messages") == [("queued",)]  # no claim, no ack


def test_a_live_session_gets_one_hint_after_the_lock_and_custody_is_unchanged(lodge, wake):
    mail(lodge)
    wake.live = "pal-live"
    lodge.registry.create("pal-live", wolt="pal")
    for result in ("submitted", "unknown", "failed", "unavailable", "pending"):
        lodge.calls.delivered.clear()
        sys.modules["native_adapters"].deliver = (
            lambda record, hint, r=result: lodge.calls.delivered.append((record["name"], hint, held(lodge.root)))
            or (r, "x"))
        inbox_wake._wake(str(lodge.root), "pal")
        (name, hint, locked), = lodge.calls.delivered
        assert name == "pal-live" and not locked and "woltspace inbox fetch" in hint and "hello" not in hint
    assert not wake.starts
    assert rows(lodge.root, "SELECT state, body FROM inbox_messages") == [("queued", b"hello")]


@pytest.mark.parametrize("why", ["off", "nothing_queued", "archived"])
def test_nothing_happens_without_eligible_mail(lodge, wake, why):
    status = mail(lodge)
    if why == "off":
        (lodge.root / "woltspace.json").write_text("{}")
        inbox_wake.notify_admitted(lodge.root, status)
        time.sleep(0.2)
    else:
        if why == "nothing_queued":
            with sessions.lodge_db.write(lodge.root) as conn:
                conn.execute("UPDATE inbox_messages SET state='acknowledged', acked_at=?", (time.time(),))
        else:
            lodge.store.set_state("pal", "archived")
        inbox_wake._wake(str(lodge.root), "pal")
    assert not wake.starts and not lodge.calls.delivered


def test_a_burst_coalesces_into_one_wake_and_never_raises(lodge, monkeypatch):
    runs, gate = [], threading.Event()
    monkeypatch.setattr(inbox_wake, "_wake", lambda root, wolt, once=False: (runs.append(wolt), gate.wait(5)))
    statuses = [mail(lodge, f"k{i}") for i in range(8)]
    for status in statuses:
        inbox_wake.notify_admitted(lodge.root, status)
    gate.set()
    deadline = time.monotonic() + 5
    while inbox_wake._PENDING and time.monotonic() < deadline:
        time.sleep(0.05)
    assert runs == ["pal", "pal"]  # the one running, then one for everything that came meanwhile
    monkeypatch.setattr(inbox, "enabled", lambda root: 1 / 0)
    inbox_wake.notify_admitted(lodge.root, statuses[0])  # logged, never raised


def test_a_session_that_ended_without_an_ack_does_not_hold_later_mail(lodge, wake):
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    name = wake.starts[0][0]["name"]
    lodge.registry.update(name, status="completed")
    mail(lodge, "k2", human=False)
    inbox_wake._wake(str(lodge.root), "pal")  # all mail brings the wolt up
    assert len(wake.starts) == 2 and "inhibited" not in wake_state(lodge)


def test_progress_lets_the_next_mail_start_a_fresh_session(lodge, wake):
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    name = wake.starts[0][0]["name"]
    with sessions.lodge_db.write(lodge.root) as conn:
        conn.execute("INSERT INTO inbox_readers(hash, mailbox, session, created_at, acks)"
                     " VALUES (x'01', 1, ?, 0, 1)", (name,))
    lodge.registry.update(name, status="completed")
    mail(lodge, "k2")
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(wake.starts) == 2 and "inhibited" not in wake_state(lodge)


@pytest.mark.parametrize("ending", ["stopped", "unresolved"])
def test_a_stop_does_not_hold_the_wolt_but_an_unresolved_launch_waits(lodge, wake, ending):
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    name = wake.starts[0][0]["name"]
    if ending == "stopped":
        sessions.stop_session(name)  # the owner's explicit stop: that conversation only
    else:
        lodge.registry.update(name, status="running", launching={"attempt": "a2"})
    mail(lodge, "k2", human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    if ending == "stopped":
        assert len(wake.starts) == 2 and wake.starts[1][0]["name"] != name
    else:
        assert len(wake.starts) == 1 and "inhibited" not in wake_state(lodge)
        with pytest.raises(lifecycle.LifecycleUnknown):
            inbox_wake.rearm(lodge.root, "pal")


def test_a_policy_that_is_not_auto_inhibits_instead_of_starting(lodge, wake, monkeypatch):
    def refuse(**kw):
        raise PermissionError("not auto")
    monkeypatch.setattr(sessions, "start_session", refuse)
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    assert wake_state(lodge)["inhibited"] == "not_startable"


def test_boot_reconcile_wakes_every_agent_with_queued_mail(lodge, monkeypatch):
    woken = []
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt: woken.append(wolt))
    wolt(lodge.root, "nunu")
    mail(lodge)
    mail(lodge, "k2", "nunu")
    inbox_wake.reconcile(lodge.root)
    assert sorted(woken) == ["nunu", "pal"]


def test_rearm_is_refused_while_any_launch_of_the_agent_is_unresolved(lodge, unresolved):
    mail(lodge)
    with pytest.raises(lifecycle.LifecycleUnknown):
        inbox_wake.rearm(lodge.root, "pal")
    assert record(lodge, unresolved)["launching"]


# -- native wiring after publication, and definite launch failures ----------------

def test_the_child_prepares_after_publication_and_the_engine_gets_the_attempt(lodge, monkeypatch, tmp_path):
    """run-session.sh prepares in the child, after publish_runtime cleared the marker."""
    monkeypatch.delitem(sys.modules, "native_adapters")
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    row = record(lodge, "pal-a-b-c")
    assert "launching" not in row and row["native"]["attempt"] == row["attempt"]
    command = sessions.prepare_session_command("pal-a-b-c", "spawn", "hi")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "claude").write_text('#!/bin/sh\nenv | grep ^WOLTSPACE_NATIVE_ > "$OUT"; printf "%s\\n" "$@" >> "$OUT"\n')
    (fake / "claude").chmod(0o755)
    out = tmp_path / "engine.txt"
    subprocess.run(["bash", "-c", command], env={"PATH": f"{fake}:/usr/bin:/bin", "OUT": str(out)},
                   check=True, timeout=30)
    seen = out.read_text().splitlines()
    assert f"WOLTSPACE_NATIVE_ATTEMPT={row['attempt']}" in seen
    assert f"WOLTSPACE_NATIVE_DIR={row['native']['addr']}" in seen
    settings = Path(seen[seen.index("--settings") + 1])
    assert settings.parent == Path(row["native"]["addr"]) and settings.is_file()


def test_a_failed_new_session_is_a_definite_non_start(lodge, monkeypatch):
    def refused(name, cwd, command):
        raise subprocess.CalledProcessError(1, ["tmux", "new-session"])
    monkeypatch.setattr(sessions, "_tmux_spawn", refused)
    with pytest.raises(subprocess.CalledProcessError):
        sessions.start_session(wolt="pal", name="pal-a-b-c")
    row = record(lodge, "pal-a-b-c")
    assert "launching" not in row and (row["status"], row["stop_reason"]) == ("stopped", "launch_failed")
    assert lodge.calls.retired == lodge.calls.prepared  # its attempt is retired
    sessions.delete_session("pal-a-b-c")  # settles normally: nothing UNKNOWN left


def test_a_failed_resume_paste_keeps_the_prior_runtime_and_raises(lodge, monkeypatch):
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    lodge.registry.update("pal-a-b-c", harness_session_id="conv-1")
    before = record(lodge, "pal-a-b-c")
    monkeypatch.setattr(sessions, "resolve_agent_handle", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "_runtime", lambda: types.SimpleNamespace(handle_is_alive=lambda h: True))

    def broken(target, text, settle=0.0):
        raise subprocess.CalledProcessError(1, ["tmux", "paste-buffer"])
    monkeypatch.setattr(sessions, "_tmux_paste", broken)
    with pytest.raises(sessions.ResumeFailed, match="could not be pasted"):
        sessions.resume_session("pal-a-b-c")
    after = record(lodge, "pal-a-b-c")
    assert "launching" not in after and after["runtime"] == before["runtime"]
    assert after["status"] == before["status"] == "running" and "stop_reason" not in after
    assert len(lodge.calls.retired) == 2  # the old attempt before the paste, the failed one after


# -- a1c8ea5: hints despite an inhibition, once per attempt, human mail, owner notice ---------

@pytest.fixture
def notices(monkeypatch):
    """The owner's lines (outcome: how, seqs, session or reason, chat) and the senders' notices."""
    told = types.SimpleNamespace(lines=[], senders=[])
    monkeypatch.setitem(sys.modules, "inbox_notice", types.SimpleNamespace(
        notice=lambda root, wolt, reason, owner: told.senders.append((wolt, reason, owner)),
        outcome=lambda root, wolt, *, seqs, how, session=None, reason=None, chat=None:
        told.lines.append((how, seqs, session or reason, chat))))
    return told


def seqs(lodge):
    return [seq for seq, in rows(lodge.root, "SELECT seq FROM inbox_messages WHERE priority=0 ORDER BY seq")]


def test_an_inhibited_wolt_with_a_live_session_still_gets_its_hint(lodge, wake, notices):
    mail(lodge)
    lodge.store.set_wake("pal", {**wake_state(lodge), "inhibited": "not_startable"})
    lodge.registry.create("pal-live", wolt="pal")
    wake.live = "pal-live"
    inbox_wake._wake(str(lodge.root), "pal")
    assert [d[0] for d in lodge.calls.delivered] == ["pal-live"] and not wake.starts
    assert notices.lines == [("hinted", seqs(lodge), "pal-live", None)] and not notices.senders


def test_once_hints_each_attempt_once_and_retries_while_nothing_was_placed(lodge, wake):
    mail(lodge)
    lodge.registry.create("pal-live", wolt="pal")
    lodge.registry.update("pal-live", attempt="a1")
    wake.live = "pal-live"
    results = iter([("pending", "not_ready")] + [("submitted", "ok")] * 3)
    sys.modules["native_adapters"].deliver = lambda record, hint: (
        lodge.calls.delivered.append(record["attempt"]) or next(results))
    for _ in range(3):
        inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert lodge.calls.delivered == ["a1", "a1"]  # retried after not_ready, then never again
    lodge.registry.update("pal-live", attempt="a2")  # a resume: a new attempt
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert lodge.calls.delivered == ["a1", "a1", "a2"]
    inbox_wake._wake(str(lodge.root), "pal")  # a new arrival hints regardless
    assert len(lodge.calls.delivered) == 4


@pytest.mark.parametrize("human", [True, False])
def test_any_mail_starts_the_wolt_in_its_own_mode_when_nothing_is_resumable(lodge, wake, human):
    mail(lodge, human=human)
    inbox_wake._wake(str(lodge.root), "pal")
    (kw, _), = wake.starts
    assert not kw.get("require_auto")  # its own recorded mode: never refused for not being auto


def test_the_owner_is_told_whenever_mail_neither_hints_nor_starts(lodge, wake, notices, monkeypatch):
    def refuse(**kw):
        raise PermissionError("prompt mode")
    monkeypatch.setattr(sessions, "start_session", refuse)
    mail(lodge, human=False)  # agent mail only: no owner line, its sender's notice
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [] and notices.senders == [("pal", "not_startable", False)]
    mail(lodge, "k2")
    inbox_wake._wake(str(lodge.root), "pal")  # not_startable, retried for the owner's mail, refused again
    inbox_wake._wake(str(lodge.root), "pal")  # a repeat wake, a resend, a ready call: no second line
    k2, = seqs(lodge)
    assert notices.lines == [("blocked", [k2], "not_startable", None)]
    mail(lodge, "k3")  # new owner mail: its own line
    lodge.registry.create("pal-x", wolt="pal")
    lodge.registry.update("pal-x", launching={"attempt": "a9"})
    inbox_wake._wake(str(lodge.root), "pal")
    inbox_wake._wake(str(lodge.root), "pal")  # same mail, a changed reason: still its one line
    assert notices.lines[1:] == [("blocked", [k2 + 1], "launch_unknown", None)]


# -- readiness pickup: announce, validate, schedule once ---------------------------------

def test_only_the_sessions_own_reader_for_its_current_attempt_schedules_a_pickup(lodge, monkeypatch):
    scheduled = []
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt, once=False: scheduled.append((wolt, once)))
    for name in ("pal-a", "pal-b"):
        lodge.registry.create(name, wolt="pal")
        lodge.registry.update(name, attempt=f"{name}-1", runtime={"x": 1})
    store = inbox.Store(lodge.root, limits(name_chars=64))
    token = store.grant("pal-a", "pal")
    assert inbox_wake.ready(lodge.root, token, "pal-a", "pal-a-1") and scheduled == [("pal", True)]
    assert not inbox_wake.ready(lodge.root, token, "pal-b", "pal-b-1")  # not its own session
    assert not inbox_wake.ready(lodge.root, token, "pal-a", "pal-a-0")  # a stale attempt
    assert not inbox_wake.ready(lodge.root, "t" * 43, "pal-a", "pal-a-1")  # no reader
    lodge.registry.update("pal-a", launching={"attempt": "pal-a-2"})
    assert not inbox_wake.ready(lodge.root, token, "pal-a", "pal-a-1")  # a relaunch in flight
    assert scheduled == [("pal", True)]
    lodge.registry.update("pal-a", launching=None)
    assert inbox_wake.ready(lodge.root, token, "pal-a", "pal-a-1", "turn_end")  # a plain wake
    assert not inbox_wake.ready(lodge.root, token, "pal-a", "pal-a-1", "other")
    assert not inbox_wake.ready(lodge.root, token, "pal-b", "pal-b-1", "turn_end")  # not its own
    assert scheduled == [("pal", True), ("pal", False)]


def test_a_published_ready_file_announces_itself_once_and_a_failure_is_ignored(tmp_path, monkeypatch):
    import http.server
    import native_adapters
    calls = []

    class Lodge(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            calls.append((self.path, self.headers["X-Woltspace-Inbox-Token"],
                          json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Lodge)
    threading.Thread(target=server.handle_request, daemon=True).start()
    monkeypatch.setenv("WOLTSPACE_API", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("WOLTSPACE_INBOX_TOKEN", "tok")
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "pal-a")
    assert native_adapters.write_ready(tmp_path, {"attempt": "a1", "kind": "claude"})
    server.server_close()
    assert calls == [("/inbox/ready", "tok", {"session": "pal-a", "attempt": "a1"})]
    monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:9")  # nobody there: still published
    assert native_adapters.write_ready(tmp_path, {"attempt": "a2", "kind": "claude"})


def test_a_resume_that_finds_the_agent_running_schedules_a_pickup(lodge, monkeypatch):
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    picked = []
    monkeypatch.setattr(inbox_wake, "schedule_pickup", lambda root, wolt: picked.append(wolt))
    monkeypatch.setattr(sessions, "resolve_agent_handle",
                        lambda data, harness, include_launching=False: RuntimeHandle("pal-a-b-c", "pal-a-b-c"))
    monkeypatch.setattr(sessions, "_paste_into_live_agent", lambda *a: True)
    monkeypatch.setattr(sessions, "_await_input_ready", lambda *a, **k: True)
    assert sessions.resume_session("pal-a-b-c", "hello")["status"] == "delivered"
    assert picked == ["pal"]


@pytest.mark.parametrize("human", [True, False])
@pytest.mark.parametrize("inhibited", ["not_startable", "stopped", "no_progress"])
def test_no_earlier_inhibition_holds_new_mail(lodge, wake, notices, inhibited, human):
    mail(lodge, human=human)
    lodge.store.set_wake("pal", {"inhibited": inhibited})
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(wake.starts) == 1 and not any(kw.get("require_auto") for kw, _ in wake.starts)


def test_whichever_comes_later_of_publication_and_readiness_hints_once(lodge, monkeypatch):
    scheduled = []
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt, once=False: scheduled.append((wolt, once)))
    store = inbox.Store(lodge.root, limits(name_chars=64))
    early = []

    def spawn(name, cwd, command):  # the backend says ready before its publication
        attempt = record(lodge, name)["launching"]["attempt"]
        early.append(inbox_wake.ready(lodge.root, store.grant(name, "pal"), name, attempt))
        return RuntimeHandle(name, name, "%1")
    monkeypatch.setattr(sessions, "_tmux_spawn", spawn)
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    assert early == [False] and scheduled == [("pal", True)]  # refused while launching; publication schedules
    mail(lodge)  # publication before ready: the pickup finds the backend not ready, which counts for nothing
    monkeypatch.setattr(sessions, "resolve_active_session", lambda wolt, registry=None: "pal-a-b-c")
    results = iter([("pending", "not_ready"), ("submitted", "claimed")])
    sys.modules["native_adapters"].deliver = (
        lambda record_, hint: lodge.calls.delivered.append(record_["attempt"]) or next(results))
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # the publication's pickup
    token = store.grant("pal-a-b-c", "pal")
    assert inbox_wake.ready(lodge.root, token, "pal-a-b-c", record(lodge, "pal-a-b-c")["attempt"])
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # the ready call's pickup
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # a repeat: this attempt is hinted
    assert len(lodge.calls.delivered) == 2


@pytest.mark.parametrize("result", [("unknown", "timeout"), ("failed", "OSError"), ("pending", "not_ready")])
def test_only_a_hint_that_reached_the_engine_counts_once(lodge, wake, result):
    mail(lodge)
    lodge.registry.create("pal-live", wolt="pal")
    lodge.registry.update("pal-live", attempt="a1")
    wake.live = "pal-live"
    sys.modules["native_adapters"].deliver = lambda record_, hint: lodge.calls.delivered.append(1) or result
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert len(lodge.calls.delivered) == 2 and "hinted" not in wake_state(lodge)



# -- resume first (owner, 29 Oct): mail resumes a resting wolt in its own mode, no paste --------

@pytest.fixture
def resting(lodge, monkeypatch):
    """A resting prompt-mode session of pal with a conversation to resume; tmux gone."""
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)  # no pickup thread racing the test
    sessions.start_session(wolt="pal", name="pal-rest", execution_policy="prompt")
    lodge.registry.update("pal-rest", status="resting", harness_session_id="conv-1")
    sent = types.SimpleNamespace(spawned=[], input=[])
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    monkeypatch.setattr(sessions, "_await_agent", lambda *a, **k: True)
    monkeypatch.setattr(sessions, "_tmux_spawn", lambda name, cwd, command: sent.spawned.append(command)
                        or RuntimeHandle(name, name, "%2"))
    for typing in ("_tmux_paste", "_tmux_send_keys", "deliver_message"):
        if hasattr(sessions, typing):
            monkeypatch.setattr(sessions, typing, lambda *a, **k: sent.input.append(a))
    monkeypatch.setattr(sessions, "resolve_active_session", lambda wolt, registry=None: None)
    return sent


def test_agent_mail_resumes_a_resting_prompt_mode_wolt_with_no_input_and_a_pickup(lodge, resting, monkeypatch):
    picked = []
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt, once=False: picked.append((wolt, once)))
    monkeypatch.setattr(sessions, "start_session", lambda **kw: pytest.fail("a new session instead of a resume"))
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    (command,) = resting.spawned
    assert "--resume" in command and "pal-rest" in command and resting.input == []  # nothing typed or pasted
    assert command.endswith("pal-rest --resume")  # no prompt rides along either
    row = record(lodge, "pal-rest")
    assert row["status"] == "running" and row["execution_policy"]["mode"] == "prompt"  # its own mode
    assert wake_state(lodge)["reserved"]["session"] == "pal-rest"
    assert picked == [("pal", True)]  # the post-publication pickup hints once the backend is ready


@pytest.mark.parametrize("human", [True, False])
def test_mail_resumes_another_resting_session_never_the_stopped_one(lodge, resting, monkeypatch, human):
    monkeypatch.setattr(sessions, "start_session", lambda **kw: pytest.fail("a new session while one can resume"))
    lodge.registry.create("pal-old", wolt="pal")  # newer and resumable, but the owner stopped it
    lodge.registry.update("pal-old", status="resting", harness_session_id="conv-old", wake_inhibit=True,
                          last_activity=int(time.time()) + 100)
    mail(lodge, human=human)
    inbox_wake._wake(str(lodge.root), "pal")
    (command,) = resting.spawned
    assert command.endswith("pal-rest --resume") and resting.input == []
    assert record(lodge, "pal-old")["status"] == "resting"


def test_mail_with_only_stopped_sessions_starts_one_and_rearm_keeps_the_mark(lodge, resting, monkeypatch):
    started = []
    monkeypatch.setattr(sessions, "start_session", lambda **kw: started.append(kw) or {})
    sessions.stop_session("pal-rest")
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    assert resting.spawned == [] and len(started) == 1  # a new one, never the stopped conversation
    inbox_wake.rearm(lodge.root, "pal")
    assert record(lodge, "pal-rest")["wake_inhibit"]  # still never auto-resumed


def test_a_definite_resume_failure_moves_on_and_an_unknown_one_holds(lodge, resting, monkeypatch):
    started, calls = [], []
    monkeypatch.setattr(sessions, "start_session", lambda **kw: started.append(kw) or {})

    def refused(name, prompt="", **kw):  # e.g. an expired conversation: nothing launched
        calls.append(name)
        raise sessions.ResumeFailed("the engine refused the conversation")
    monkeypatch.setattr(sessions, "resume_session", refused)
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    assert calls == ["pal-rest"] and record(lodge, "pal-rest")["resume_failed"] and started == []
    mail(lodge, "k2", human=False)
    inbox_wake._wake(str(lodge.root), "pal")  # never that conversation again: a new session
    assert calls == ["pal-rest"] and len(started) == 1


def test_an_unknown_resume_outcome_never_starts_a_second_worker(lodge, resting, monkeypatch):
    monkeypatch.setattr(sessions, "start_session", lambda **kw: pytest.fail("a second worker after an unknown resume"))

    def lost(name, prompt="", **kw):  # the relaunch was marked, then its tmux call timed out
        lodge.registry.update(name, status="running", launching={"attempt": "r1"})
        raise subprocess.TimeoutExpired("tmux", 10)
    monkeypatch.setattr(sessions, "resume_session", lost)
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    assert "resume_failed" not in record(lodge, "pal-rest")
    mail(lodge, "k2", human=False)
    inbox_wake._wake(str(lodge.root), "pal")  # launch_unknown holds


@pytest.mark.parametrize("gap", ["stopped", "archived", "other_launching"])
def test_an_owners_stop_in_the_gap_before_the_relaunch_is_never_revived(lodge, resting, monkeypatch, gap):
    real = sessions.resume_session
    lodge.registry.create("pal-other", wolt="pal")

    def stop_first(name, prompt="", **kw):  # lands after the wake chose the session, before the relaunch
        if gap in ("stopped", "archived"):
            lodge.registry.update(name, status=gap, wake_inhibit=True)
        else:  # a manual launch of the agent elsewhere
            lodge.registry.update("pal-other", launching={"attempt": "m1"})
        return real(name, prompt, **kw)
    monkeypatch.setattr(sessions, "resume_session", stop_first)
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    assert resting.spawned == [] and "launching" not in record(lodge, "pal-rest")
    assert record(lodge, "pal-rest")["status"] == (gap if gap in ("stopped", "archived") else "resting")


def test_another_sessions_stop_in_the_gap_does_not_hold_the_resume(lodge, resting, monkeypatch):
    real = sessions.resume_session
    lodge.registry.create("pal-other", wolt="pal")

    def stop_other_first(name, prompt="", **kw):
        lodge.registry.update("pal-other", status="stopped", wake_inhibit=True)
        return real(name, prompt, **kw)
    monkeypatch.setattr(sessions, "resume_session", stop_other_first)
    mail(lodge, human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    assert [c for c in resting.spawned if c.endswith("pal-rest --resume")]


# -- one owner line per human message, and the preferred session (P2, 29 Oct) -----------------

def test_a_coalesced_burst_answers_each_human_message_once(lodge, wake, notices):
    mail(lodge, "k1")
    mail(lodge, "k2")
    mail(lodge, "a1", human=False)
    inbox_wake._wake(str(lodge.root), "pal")  # one wake for the whole burst
    inbox_wake._wake(str(lodge.root), "pal")  # a repeat answers nothing again
    (kw, _), = wake.starts
    assert notices.lines == [("started", seqs(lodge), kw["name"], None)] and len(seqs(lodge)) == 2
    mail(lodge, "k3")
    wake.live = kw["name"]
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines[1:] == [("hinted", seqs(lodge)[2:], kw["name"], None)]


def test_a_first_message_a_reader_took_before_the_wake_is_answered_as_hinted_to_it(lodge, wake, notices,
                                                                                    monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    mail(lodge)  # the mailbox's first message sets the baseline
    lodge.registry.create("pal-live", wolt="pal")
    lodge.store.fetch(lodge.store.grant("pal-live", "pal"))  # the live session fetched it first
    inbox_wake._wake(str(lodge.root), "pal")
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [("hinted", seqs(lodge), "pal-live", None)] and not wake.starts


def test_mail_handled_before_the_accounting_began_is_never_answered(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    lodge.registry.create("pal-live", wolt="pal")
    token = lodge.store.grant("pal-live", "pal")
    lodge.store.admit("telegram", "old", "pal", "hello", human=True)
    got = lodge.store.fetch(token)
    lodge.store.ack(token, got.seq, got.claim)  # handled by an earlier lodge
    queued = lodge.store.admit("telegram", "queued", "pal", "hello", human=True)
    import lodge_db
    with lodge_db.write(lodge.root) as conn:  # as an earlier lodge left it: no accounting yet
        conn.execute("UPDATE inbox_mailboxes SET wake=NULL")
    new = mail(lodge, "new")
    assert wake_state(lodge)["answered"] == queued.seq - 1  # below this message and any still queued
    mail(lodge, "next")
    assert wake_state(lodge)["answered"] == queued.seq - 1  # set once
    wake.live = "pal-live"
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [("hinted", [queued.seq, new.seq, new.seq + 1], "pal-live", None)]


def test_a_preferred_resting_session_is_resumed_before_another_live_one_is_hinted(lodge, resting, notices,
                                                                                    monkeypatch):
    lodge.registry.create("pal-live", wolt="pal")
    monkeypatch.setattr(sessions, "resolve_active_session", lambda wolt, registry=None: "pal-live")
    mail(lodge, route={"session": "pal-rest", "chat": None})
    inbox_wake._wake(str(lodge.root), "pal")
    (command,) = resting.spawned
    assert command.endswith("pal-rest --resume") and lodge.calls.delivered == []
    assert notices.lines == [("resumed", seqs(lodge), "pal-rest", None)]


def test_a_hint_not_placed_yet_is_answered_by_the_pickup_that_places_it(lodge, wake, notices):
    mail(lodge)
    lodge.registry.create("pal-live", wolt="pal")
    lodge.registry.update("pal-live", attempt="a1")
    wake.live = "pal-live"
    results = iter([("pending", "not_ready"), ("pending", "placed")])
    sys.modules["native_adapters"].deliver = lambda record_, hint: next(results)
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == []  # not "notified": nothing reached the session yet
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # the ready call's pickup
    assert notices.lines == [("hinted", seqs(lodge), "pal-live", None)]


def test_coalesced_mail_from_two_chats_is_answered_on_each_its_own(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    telegram, slack = {"adapter": "telegram", "chat": "42"}, {"adapter": "slack", "chat": "D1"}
    mail(lodge, "k1", route={"session": None, "chat": telegram})
    mail(lodge, "k2", route={"session": None, "chat": slack})
    mail(lodge, "k3", route={"session": None, "chat": slack})
    inbox_wake._wake(str(lodge.root), "pal")
    (kw, _), = wake.starts
    k1, k2, k3 = seqs(lodge)
    assert notices.lines == [("started", [k1], kw["name"], telegram), ("started", [k2, k3], kw["name"], slack)]
    assert wake_state(lodge).get("chats", {}) == {}


@pytest.mark.parametrize("result", [("failed", "OSError"), ("unavailable", "no_adapters")])
def test_a_hint_that_cannot_be_delivered_is_the_owners_blocked_line(lodge, wake, notices, result):
    mail(lodge)
    lodge.registry.create("pal-live", wolt="pal")
    wake.live = "pal-live"
    sys.modules["native_adapters"].deliver = lambda record_, hint: result
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [("blocked", seqs(lodge), f"hint_{result[0]}", None)]


@pytest.mark.parametrize("crash", [RuntimeError("boom"), lifecycle.LifecycleBusy("pal")])
def test_a_wake_that_raises_still_answers_the_owner_with_an_error(lodge, notices, monkeypatch, crash):
    monkeypatch.setattr(inbox_wake, "BUSY_RETRIES", 1)

    def broken(root, wolt, once=False):
        raise crash
    monkeypatch.setattr(inbox_wake, "_wake", broken)
    mail(lodge)
    inbox_wake._PENDING[(str(lodge.root), "pal")] = None  # as _schedule leaves it for its worker
    inbox_wake._worker(str(lodge.root), "pal")
    reason = "busy" if isinstance(crash, lifecycle.LifecycleBusy) else "error"
    assert notices.lines == [("error", seqs(lodge), reason, None)] and notices.senders == [("pal", reason, False)]
    inbox_wake._PENDING[(str(lodge.root), "pal")] = None
    inbox_wake._worker(str(lodge.root), "pal")
    assert len(notices.lines) == 1


def test_the_preferred_live_session_is_hinted_and_answered_on_its_chat(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    for name in ("pal-s1", "pal-s2"):
        lodge.registry.create(name, wolt="pal")
    wake.live = "pal-s2"  # the newest live session
    chat = {"adapter": "telegram", "chat_id": "42"}
    mail(lodge, route={"session": "pal-s1", "chat": chat})
    assert wake_state(lodge)["prefer"]["session"] == "pal-s1"
    inbox_wake._wake(str(lodge.root), "pal")
    assert [d[0] for d in lodge.calls.delivered] == ["pal-s1"]
    assert notices.lines == [("hinted", seqs(lodge), "pal-s1", chat)] and _stale_prefer(lodge)


def test_the_preferred_resting_session_is_resumed_over_a_newer_one(lodge, resting, notices, monkeypatch):
    lodge.registry.create("pal-new", wolt="pal")
    lodge.registry.update("pal-new", status="resting", harness_session_id="conv-new",
                          last_activity=int(time.time()) + 100)
    mail(lodge, route={"session": "pal-rest", "chat": None})
    inbox_wake._wake(str(lodge.root), "pal")
    (command,) = resting.spawned
    assert command.endswith("pal-rest --resume") and resting.input == []
    assert notices.lines == [("resumed", seqs(lodge), "pal-rest", None)]


@pytest.mark.parametrize("stale", ["gone", "stopped"])
def test_a_stale_preference_falls_back_keeps_its_chat_and_is_cleared(lodge, resting, notices, monkeypatch, stale):
    if stale == "stopped":
        sessions.stop_session("pal-rest")
    lodge.registry.create("pal-new", wolt="pal")
    lodge.registry.update("pal-new", status="resting", harness_session_id="conv-new")
    with lodge.registry.edit("pal-new", wolt="pal") as data:  # the newest (update stamps now)
        data["last_activity"] = int(time.time()) + 100
    chat = {"adapter": "slack", "chat_id": "D1"}
    mail(lodge, route={
        "session": "pal-gone" if stale == "gone" else "pal-rest", "chat": chat})
    inbox_wake._wake(str(lodge.root), "pal")
    (command,) = resting.spawned
    assert command.endswith("pal-new --resume")
    assert notices.lines == [("resumed", seqs(lodge), "pal-new", chat)] and _stale_prefer(lodge)


def test_a_resume_that_fails_is_the_owners_error_line(lodge, resting, notices, monkeypatch):
    def refused(name, prompt="", **kw):
        raise sessions.ResumeFailed("the engine refused the conversation")
    monkeypatch.setattr(sessions, "resume_session", refused)
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [("error", seqs(lodge), "resume_failed", None)]


def test_every_message_an_agents_too_gets_its_outcome_recorded_in_a_bounded_ring(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "OUTCOMES", 3)
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)  # rearm's wake: not raced here
    mail(lodge, "a1", human=False)
    mail(lodge, "k1")
    inbox_wake._wake(str(lodge.root), "pal")
    (kw, _), = wake.starts
    a1, k1 = [seq for seq, in rows(lodge.root, "SELECT seq FROM inbox_messages ORDER BY seq")]
    assert wake_state(lodge)["outcomes"] == [{"seq": a1, "how": "started", "session": kw["name"]},
                                             {"seq": k1, "how": "started", "session": kw["name"]}]
    assert notices.lines == [("started", [k1], kw["name"], None)]  # the owner's line: human mail only
    mail(lodge, "a2", human=False)
    mail(lodge, "a3", human=False)
    sys.modules["native_adapters"].deliver = lambda record_, hint: ("failed", "OSError")
    wake.live = kw["name"]
    inbox_wake._wake(str(lodge.root), "pal")
    ring = wake_state(lodge)["outcomes"]
    assert [e["seq"] for e in ring] == [k1, k1 + 1, k1 + 2]  # the oldest dropped
    assert ring[-1] == {"seq": k1 + 2, "how": "blocked", "reason": "hint_failed"} and len(notices.lines) == 1
    inbox_wake.rearm(lodge.root, "pal")
    assert wake_state(lodge)["outcomes"] == ring


def _stale_prefer(lodge):
    wake = wake_state(lodge)
    return wake["prefer"]["seq"] <= wake["answered"]  # its message answered: never a first choice again


def test_a_wake_right_after_the_commit_answers_on_the_route_admitted_with_it(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    telegram = {"adapter": "telegram", "chat": "42"}
    status = mail(lodge, route={"session": None, "chat": telegram})  # committed; its own notify paused
    inbox_wake._wake(str(lodge.root), "pal")  # another wake runs first
    (kw, _), = wake.starts
    assert notices.lines == [("started", [status.seq], kw["name"], telegram)]


def test_an_admission_between_a_wakes_read_and_its_write_keeps_its_route_and_preference(lodge, wake, notices,
                                                                                       monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    slack = {"adapter": "slack", "chat": "D1"}
    first = mail(lodge)
    late = []
    write = inbox.Store.set_wake

    def admission_lands_first(self, wolt, state):  # paused: the answer has read, not written yet
        if not late and "outcomes" in state:
            late.append(mail(lodge, "k2", route={"session": "pal-meant", "chat": slack}))
        write(self, wolt, state)
    monkeypatch.setattr(inbox.Store, "set_wake", admission_lands_first)
    inbox_wake._wake(str(lodge.root), "pal")
    monkeypatch.setattr(inbox.Store, "set_wake", write)
    (kw, _), = wake.starts
    stored = wake_state(lodge)
    assert stored["chats"] == {str(late[0].seq): slack}  # the wake's stale copy never dropped it
    assert stored["prefer"]["session"] == "pal-meant" and stored["prefer"]["seq"] == late[0].seq
    wake.live = kw["name"]
    inbox_wake._wake(str(lodge.root), "pal")
    assert notices.lines == [("started", [first.seq], kw["name"], None), ("hinted", [late[0].seq], kw["name"], slack)]


def test_a_true_duplicate_admission_keeps_the_route_and_accounting_it_was_accepted_with(lodge, wake, notices):
    telegram, slack = {"adapter": "telegram", "chat": "42"}, {"adapter": "slack", "chat": "D1"}
    status = mail(lodge, route={"session": "pal-meant", "chat": telegram})
    before = wake_state(lodge)
    again = mail(lodge, route={"session": "pal-other", "chat": slack})  # a resend of the same event
    assert again.seq == status.seq and wake_state(lodge) == before
    inbox_wake._wake(str(lodge.root), "pal")
    answered = wake_state(lodge)
    mail(lodge, route={"session": "pal-other", "chat": slack})  # and after its answer: nothing reopens
    inbox_wake._wake(str(lodge.root), "pal")
    (kw, _), = wake.starts
    assert wake_state(lodge) == answered and notices.lines == [("started", [status.seq], kw["name"], telegram)]


@pytest.mark.parametrize("adapter, chat", [("telegram", "4242"), ("slack", "D1:1712345678.000100")])
def test_a_session_the_wake_starts_for_owner_mail_is_routed_to_its_chat_as_chat_lodge_did(lodge, monkeypatch,
                                                                                           adapter, chat):
    from test_chat_lodge_inbox import ROOT  # noqa: F401 (puts container/ on the path for chat_lodge)
    import chat_lodge
    import connector_registry
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)  # nothing live: a fresh start
    started = []
    real = sessions.start_session
    monkeypatch.setattr(sessions, "start_session", lambda **kw: started.append(kw) or real(**kw))
    mail(lodge, route={"session": None, "chat": {"adapter": adapter, "chat": chat}})
    inbox_wake._wake(str(lodge.root), "pal")
    side = chat_lodge.ChatLodge(connector_registry.spec(adapter), [])
    assert started[0]["routing"] == side._routing(chat)  # the routing its own spawn passed before the inbox
    if adapter == "telegram":  # notify's exact binding keeps the reply footer
        codec = connector_registry.spec(adapter).codec
        assert codec.matches(codec.canonical(chat), record(lodge, started[0]["name"]))


def test_a_session_the_wake_starts_for_agent_mail_only_is_unrouted(lodge, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    mail(lodge, human=False, route={"session": None, "chat": {"adapter": "telegram", "chat": "4242"}})
    inbox_wake._wake(str(lodge.root), "pal")
    (name, _, _), = lodge.calls.spawned
    assert not record(lodge, name).get("routing")


@pytest.mark.parametrize("again", [True, False])  # a later pickup (another session's ready) or a coalesced pass
def test_while_the_preferred_live_session_has_not_fetched_only_it_is_hinted(lodge, wake, notices, monkeypatch,
                                                                             again):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    for name in ("pal-s1", "pal-s2"):
        lodge.registry.create(name, wolt="pal")
        lodge.registry.update(name, attempt=f"{name}-a1")
    wake.live = "pal-s2"  # the newest live session
    mail(lodge, route={"session": "pal-s1", "chat": None})
    inbox_wake._wake(str(lodge.root), "pal")
    inbox_wake._wake(str(lodge.root), "pal", once=again)
    assert [d[0] for d in lodge.calls.delivered] == ["pal-s1"] * (1 if again else 2)
    lodge.store.fetch(lodge.store.grant("pal-s1", "pal"))  # it took the message: the preference is spent
    mail(lodge, "k2")
    inbox_wake._wake(str(lodge.root), "pal")
    assert lodge.calls.delivered[-1][0] == "pal-s2"


@pytest.mark.parametrize("end", ["stopped", "died"])
def test_a_preferred_session_that_ends_with_its_message_queued_hands_it_on(lodge, wake, notices, monkeypatch, end):
    scheduled = []
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt, once=False: scheduled.append((wolt, once)))
    for name in ("pal-s1", "pal-s2"):
        lodge.registry.create(name, wolt="pal")
        lodge.registry.update(name, attempt=f"{name}-a1")
    wake.live = "pal-s2"
    mail(lodge, route={"session": "pal-s1", "chat": None})
    inbox_wake._wake(str(lodge.root), "pal")
    if end == "stopped":
        sessions.stop_session("pal-s1")  # the owner's stop schedules a pickup
        assert scheduled == [("pal", True)]
    else:  # its tmux is gone: not live, not resumable (the vulture's settle schedules a pickup)
        monkeypatch.setattr(sessions, "_tmux_alive", lambda data: data.get("name") != "pal-s1")
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert [d[0] for d in lodge.calls.delivered] == ["pal-s1", "pal-s2"]
    assert len(notices.lines) == 1  # the message's one line was already sent


@pytest.mark.parametrize("named", ["pal-s1", "nunu-s9"])  # its own session, or another wolt's: ignored
def test_an_enrolled_send_naming_a_session_nudges_only_that_one(lodge, wake, notices, monkeypatch, named):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    wolt(lodge.root, "nunu")
    for name, owner in (("pal-s1", "pal"), ("pal-s2", "pal"), ("nunu-s9", "nunu")):
        lodge.registry.create(name, wolt=owner)
    wake.live = "pal-s2"  # the newest live session
    lodge.store.send(lodge.store.grant("nunu-s9", "nunu"), "pal", "k1", "hello", session=named)
    inbox_wake._wake(str(lodge.root), "pal")
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert [d[0] for d in lodge.calls.delivered] == (["pal-s1"] if named == "pal-s1" else ["pal-s2"])


# -- a busy engine gets no hint; the end of its turn does (bug note 5) -------------------------

def test_mail_for_a_busy_session_is_told_to_the_owner_at_once_and_hinted_at_its_turn_end(lodge, wake, notices,
                                                                                        monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    lodge.registry.create("pal-s1", wolt="pal")
    lodge.registry.update("pal-s1", attempt="a1")
    wake.live = "pal-s1"
    engine = types.SimpleNamespace(busy=True, placed=[])
    sys.modules["native_adapters"].deliver = lambda record_, hint: (
        ("pending", "busy") if engine.busy else engine.placed.append(hint) or ("submitted", "queued"))
    for key in ("k1", "k2", "k3"):  # a burst while its turn runs
        mail(lodge, key)
        inbox_wake._wake(str(lodge.root), "pal")
    assert engine.placed == [] and "hinted" not in wake_state(lodge)  # nothing to go stale
    assert notices.lines == [("busy", [seq], "pal-s1", None) for seq in seqs(lodge)]  # each its one line
    engine.busy = False  # the turn ends: its wake hints once, and adds no second line
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(engine.placed) == 1 and len(notices.lines) == 3


def test_a_turn_end_after_the_session_took_its_mail_hints_nothing(lodge, wake, notices, monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    lodge.registry.create("pal-s1", wolt="pal")
    wake.live = "pal-s1"
    placed = []
    sys.modules["native_adapters"].deliver = lambda record_, hint: ("pending", "busy")
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    token = lodge.store.grant("pal-s1", "pal")
    got = lodge.store.fetch(token)  # during its turn, the agent takes the mail itself
    lodge.store.ack(token, got.seq, got.claim)
    sys.modules["native_adapters"].deliver = lambda record_, hint: placed.append(hint) or ("submitted", "queued")
    inbox_wake._wake(str(lodge.root), "pal")  # its turn end
    assert placed == [] and [line[0] for line in notices.lines] == ["busy"]


def test_a_pickup_that_finds_its_new_session_busy_sends_the_owner_no_second_line(lodge, wake, notices,
                                                                                  monkeypatch):
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")  # started: the message's one line
    (kw, _), = wake.starts
    wake.live = kw["name"]
    sys.modules["native_adapters"].deliver = lambda record_, hint: ("pending", "busy")  # its START turn runs
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # the post-publication pickup
    assert [line[0] for line in notices.lines] == ["started"]


def test_a_hint_names_who_the_queued_mail_is_from_never_what_it_says(lodge, wake):
    for key, sender, human in (("k1", "pal-a", False), ("k2", "telegram", True), ("k3", "pal-a", False),
                               ("k4", "pal-b", False), ("k5", "pal-c", False)):
        lodge.store.admit(sender, key, "pal", "secret body", human=human)
    assert lodge.store.wake_facts("pal")["senders"] == ["pal-a", "pal-b", "pal-c"]  # agents, oldest first, once each
    hints = []
    sys.modules["native_adapters"].deliver = lambda record, hint: hints.append(hint) or ("submitted", "queued")
    wake.live = "pal-live"
    lodge.registry.create("pal-live", wolt="pal")
    inbox_wake._wake(str(lodge.root), "pal")
    assert hints[0].startswith("New inbox mail from your human, pal-a, pal-b, pal-c. Fetch your inbox")
    assert "secret body" not in hints[0]


def test_your_human_comes_first_never_in_the_count_and_other_names_keep_the_wolt_charset(lodge, wake):
    for n, sender in enumerate(("pal-a", "pal b\n`x`", "pal-c", "pal-d", "telegram")):
        lodge.store.admit(sender, f"k{n}", "pal", "secret body", human=sender == "telegram")
    hints = []
    sys.modules["native_adapters"].deliver = lambda record, hint: hints.append(hint) or ("submitted", "queued")
    wake.live = "pal-live"
    lodge.registry.create("pal-live", wolt="pal")
    inbox_wake._wake(str(lodge.root), "pal")
    assert hints[0].startswith("New inbox mail from your human, pal-a, pal-b--x-, pal-c and 1 more. Fetch")


@pytest.mark.parametrize("change", ["attempt", "retired", "gone"])
def test_a_hint_is_not_placed_when_the_session_changed_after_the_wake_read_it(lodge, wake, notices,
                                                                             monkeypatch, change):
    mail(lodge)
    lodge.registry.create("pal-live", wolt="pal")
    lodge.registry.update("pal-live", attempt="a1", native={"kind": "claude", "addr": "/x", "attempt": "a1"})
    wake.live = "pal-live"
    real = inbox_wake._sender

    def meanwhile(facts):  # runs after the lock, before the hint is placed
        if change == "attempt":
            lodge.registry.update("pal-live", attempt="a2")
        elif change == "retired":
            sessions.invalidate_native(lodge.registry, "pal-live", "pal", record(lodge, "pal-live"))
        else:
            lodge.registry.delete("pal-live")
        return real(facts)
    monkeypatch.setattr(inbox_wake, "_sender", meanwhile)
    inbox_wake._wake(str(lodge.root), "pal")
    assert lodge.calls.delivered == [] and notices.lines == []  # nothing placed; the new attempt's pickup hints


# -- #643 review: with the inbox off a launch is as on main; on, a provable non-start clears ------

def test_with_the_inbox_off_a_lost_spawn_leaves_no_marker_and_ends_as_on_main(lodge, monkeypatch):
    (lodge.root / "woltspace.json").write_text("{}")

    def lost(name, cwd, command):
        raise subprocess.TimeoutExpired("tmux", 10)
    monkeypatch.setattr(sessions, "_tmux_spawn", lost)
    monkeypatch.setattr(sessions, "_tmux_exists", lambda where: None)
    with pytest.raises(subprocess.TimeoutExpired):
        sessions.start_session(wolt="pal", name="pal-a-b-c")
    assert "launching" not in record(lodge, "pal-a-b-c")
    with sessions._relaunch(lodge.registry, "pal-a-b-c", "pal", "claude") as attempt:
        assert attempt == ""
    sessions.stop_session("pal-a-b-c")
    assert record(lodge, "pal-a-b-c")["status"] == "stopped"


def test_a_marker_left_from_inbox_on_days_holds_nothing_once_the_inbox_is_off(lodge, unresolved, monkeypatch):
    (lodge.root / "woltspace.json").write_text("{}")
    monkeypatch.setattr(sessions, "_tmux_sessions", lambda: set())
    assert lodge.registry.reconcile() == [unresolved]
    lodge.registry.update(unresolved, status="running")
    sessions.stop_session(unresolved)
    assert record(lodge, unresolved)["status"] == "stopped"


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("tmux", 10), OSError("no tmux"), RuntimeError("x")])
@pytest.mark.parametrize("exists", [False, None])
def test_any_failed_spawn_clears_the_marker_only_when_tmux_provably_has_no_such_session(lodge, monkeypatch,
                                                                                       error, exists):
    def lost(name, cwd, command):
        raise error
    monkeypatch.setattr(sessions, "_tmux_spawn", lost)
    monkeypatch.setattr(sessions, "_tmux_exists", lambda where: exists)
    with pytest.raises(type(error)):
        sessions.start_session(wolt="pal", name="pal-a-b-c")
    row = record(lodge, "pal-a-b-c")
    if exists is False:
        assert "launching" not in row and (row["status"], row["stop_reason"]) == ("stopped", "launch_failed")
        assert lodge.calls.retired == lodge.calls.prepared
    else:
        assert row["launching"] and row["status"] == "running" and not lodge.calls.retired


def test_rearm_refuses_a_name_that_is_not_a_wolt_before_any_lock_file(lodge):
    for bad in ("../x", "nobody", ""):
        with pytest.raises(inbox.Refused):
            inbox_wake.rearm(lodge.root, bad)
    assert not (lodge.root / ".space" / "run" / "x.lock").exists()
    assert not (lodge.root / ".space" / "run" / "lifecycle" / "nobody.lock").exists()


def test_the_wake_texts_say_an_ack_confirms_receipt_not_completion():
    for text in (inbox_wake.START, inbox_wake.HINT):
        assert "ACK confirms receipt, not task completion" in text and "woltspace inbox release" in text
