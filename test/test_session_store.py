"""Sessions in lodge.db: the races the old file locks were for, and the import."""

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import lodge_db
import sessions
from conftest import make_session
from session_runtime import RuntimeHandle
from sessions import SessionRegistry


def _race(*targets):
    threads = [threading.Thread(target=t) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def test_a_read_of_an_empty_lodge_finds_nothing_and_creates_nothing(tmp_path):
    reg = SessionRegistry(tmp_path)
    assert reg.list() == [] and reg.records() == []
    assert reg.get("s", check_alive=False) is None
    assert reg.taken_resume_ids() == set()
    assert not (tmp_path / ".space").exists()


def test_a_read_opens_the_file_read_only(tmp_path):
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal")
    with reg._reader() as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM sessions")
    assert reg.get("s", check_alive=False)["name"] == "s"


def test_two_writers_of_different_fields_keep_both(tmp_path):
    """799e4b0: the spawner's runtime handle and session-reg's resume id, written together."""
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal")
    _race(*(lambda field=field: [reg.update("s", **{field: i}) for i in range(40)]
            for field in ("runtime_n", "harness_n")))
    stored = reg._read("pal", "s")
    assert stored["runtime_n"] == 39 and stored["harness_n"] == 39


def test_a_touch_never_writes_running_over_a_rest(tmp_path):
    """#571 (a): the guard is in the UPDATE, so the loser changes nothing."""
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal", last_activity=1)
    touched, first = [], threading.Event()

    def touches():
        for _ in range(40):
            touched.append(reg.touch("s"))
            first.set()

    def rest():
        first.wait()
        reg._write("pal", "s", {**reg._read("pal", "s"), "status": "resting"})

    _race(touches, rest)
    stored = reg._read("pal", "s")
    assert stored["status"] == "resting"
    assert not reg.touch("s")
    assert True not in touched[touched.index(False):] if False in touched else True


def test_a_message_during_a_rest_waits_and_never_pastes(tmp_path, monkeypatch):
    """#571 (b): the record is `stopping` while tmux dies; a message waits, then wakes it."""
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal", harness_session_id="conv-1")
    pastes, claimed, threads = [], [], []
    monkeypatch.setattr(sessions, "_tmux_paste", lambda *a, **k: pastes.append(a))
    monkeypatch.setattr(sessions, "resolve_agent_handle",
                        lambda *a, **k: RuntimeHandle("s", "s", pane_id="%1"))

    def stop(seen):
        paste = threading.Thread(target=lambda: claimed.append(
            sessions._paste_into_live_agent(reg, "pal", "s", "running", "claude", "hi")))
        paste.start()
        threads.append(paste)
        time.sleep(0.3)  # the message has found `stopping` and waits
        return not claimed

    rested = sessions.stop_and_settle(reg, "s", reg._read("pal", "s"), stop,
                                      lambda record: record.update(status="resting"))
    threads[0].join()
    assert rested and claimed == [False] and pastes == []
    assert reg._read("pal", "s")["status"] == "resting"


@pytest.mark.parametrize("rest_first", [False, True])
def test_a_delivery_and_a_rest_claim_and_the_first_wins(tmp_path, monkeypatch, rest_first):
    """E6b: the message is pasted and the rest refused, or the message waits and wakes it."""
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal", harness_session_id="conv-1", last_activity=5)
    judged, pastes = reg._read("pal", "s"), []
    monkeypatch.setattr(sessions, "_tmux_paste", lambda *a, **k: pastes.append(a))
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: True)
    monkeypatch.setattr(sessions, "session_has_agent_process", lambda *a: True)
    monkeypatch.setattr(sessions, "resolve_agent_handle",
                        lambda *a, **k: RuntimeHandle("s", "s", pane_id="%1"))
    rest = []

    def ready(*a):
        if rest_first:  # the rest claims after delivery looked, and settles a moment later
            assert reg.claim("s", judged, sessions.STOPPING)
            settle = threading.Timer(0.3, lambda: reg._write(
                "pal", "s", {**reg._read("pal", "s"), "status": "resting"}))
            settle.start()
            rest.append(settle)
        return True

    monkeypatch.setattr(sessions, "_await_input_ready", ready)
    result = sessions.deliver_message("s", "hi", registry=reg)
    if rest_first:
        rest[0].join()
        assert result["status"] == "session-dead" and pastes == []
        assert reg._read("pal", "s")["status"] == "resting"
    else:
        assert result["status"] == "delivered" and len(pastes) == 1
        assert not reg.claim("s", judged, sessions.STOPPING)


def test_only_what_decides_a_rest_can_cancel_it(tmp_path):
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="pal", last_activity=5)
    judged = reg._read("pal", "s")
    with reg.edit("s") as data:
        data["title"] = "a title, not a use"
    assert reg.claim("s", judged, sessions.STOPPING)
    with reg.edit("s") as data:
        data["status"] = "running"
    reg.touch("s")  # a use since the judgment
    assert not reg.claim("s", judged, sessions.STOPPING)


def test_two_deliveries_to_one_session_paste_one_after_the_other(tmp_path, monkeypatch):
    """Each delivery's paste and Enter land whole, never inside another's."""
    reg = SessionRegistry(tmp_path)
    make_session(reg, name="s", wolt="alpha", harness_session_id="conv-1")
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: True)
    monkeypatch.setattr(sessions, "session_has_agent_process", lambda *a: True)
    monkeypatch.setattr(sessions, "_await_input_ready", lambda *a: True)
    monkeypatch.setattr(sessions, "resolve_agent_handle",
                        lambda *a, **k: RuntimeHandle("s", "s", pane_id="%1"))
    from conftest import FakeSessionRuntime
    buffer, pane = {}, []

    class Tmux(FakeSessionRuntime):
        def paste(self, handle, text, settle=0.0):
            # As TmuxSessionRuntime.paste: one named buffer per session, set,
            # pasted with -d, then the Enter after the settle.
            buffer[handle.woltspace_session_id] = text
            time.sleep(0.2)
            if handle.woltspace_session_id not in buffer:
                raise RuntimeError("paste-buffer: no buffer paste-s")
            pane.append(buffer.pop(handle.woltspace_session_id))
            time.sleep(0.05)
            pane.append("<Enter>")

    monkeypatch.setattr(sessions, "get_runtime", lambda: Tmux())
    start, results, errors = threading.Barrier(2, timeout=10), {}, []

    def deliver(text):
        start.wait()
        try:
            results[text] = sessions.deliver_message("s", text, registry=reg)
        except Exception as exc:
            errors.append(f"{text}: {exc}")

    _race(lambda: deliver("first message"), lambda: deliver("second message"))
    assert errors == []
    assert {t: r["status"] for t, r in results.items()} == {
        "first message": "delivered", "second message": "delivered"}
    assert pane[1::2] == ["<Enter>", "<Enter>"]
    assert sorted(pane[0::2]) == ["first message", "second message"]


# One delivering process: a fake runtime whose paste writes "<text> start",
# sleeps, then "<text> end" to a shared file. It waits for the go file first.
_DELIVER_IN_A_PROCESS = """
import sys, time
from pathlib import Path
lib, data, text, names = sys.argv[1:5]
sys.path.insert(0, lib)
import sessions
from session_runtime import RuntimeHandle, SessionRuntime, set_runtime
log = Path(data) / "pane.log"

class Pane(SessionRuntime):
    def __getattr__(self, name):
        raise AssertionError(name)
    def paste(self, handle, text, settle=0.0, **_):
        with log.open("a") as f:
            f.write(text + " start\\n")
        time.sleep(0.3 if handle.woltspace_session_id == "s" else 0)
        with log.open("a") as f:
            f.write(text + " end\\n")

set_runtime(Pane.__new__(Pane))
sessions._tmux_alive = lambda data: True
sessions.session_has_agent_process = lambda *a: True
sessions._await_input_ready = lambda *a: True
sessions.resolve_agent_handle = lambda d, *a, **k: RuntimeHandle(d["name"], d["name"], pane_id="%1")
while not (Path(data) / "go").exists():
    time.sleep(0.01)
for name in names.split(","):
    if text == "first":  # a chat message: the bot's process resumes into the live agent
        print(sessions.resume_session(name, text)["status"])
        continue
    time.sleep(0.1 if text == "second" else 0)  # the server's delivery, a moment later
    print(sessions.deliver_message(name, text, registry=sessions.SessionRegistry(data))["status"])
"""


def test_two_processes_paste_into_one_session_one_after_the_other(tmp_path):
    """The lock holds across processes (the server delivers, a chat bot resumes), not only threads."""
    import os
    import subprocess
    reg = SessionRegistry(tmp_path)
    many = [f"s{i}" for i in range(40)]
    for name in ["s", *many]:
        make_session(reg, name=name, wolt="alpha", harness_session_id=f"conv-{name}")
    lib = str(Path(sessions.__file__).parent)
    env = {**os.environ, "WOLTSPACE_WOLTS_DIR": str(tmp_path), "WOLTS_DIR": str(tmp_path)}

    def deliver(text, names="s"):
        return subprocess.Popen([sys.executable, "-c", _DELIVER_IN_A_PROCESS, lib, str(tmp_path), text, names],
                                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    procs = [deliver("first"), deliver("second")]
    time.sleep(1.0)  # both processes imported and waiting at the gate
    (tmp_path / "go").touch()
    results = [p.communicate(timeout=30) for p in procs]
    assert [out.strip() for out, _err in results] == ["delivered", "delivered"], results
    lines = (tmp_path / "pane.log").read_text().splitlines()
    assert sorted(lines[0::2]) == ["first start", "second start"]
    assert [line.replace("start", "end") for line in lines[0::2]] == lines[1::2]
    # A fixed set of lock files, however many sessions paste.
    out, err = deliver("hi", ",".join(many)).communicate(timeout=60)
    assert out.split() == ["delivered"] * 40, err
    locks = sorted(p.name for p in (tmp_path / ".space" / "run" / "paste").iterdir())
    assert 0 < len(locks) <= 32 and {f"{k}.lock" for k in range(32)} >= set(locks)


def test_a_failed_stop_gives_back_the_status_and_keeps_what_came_meanwhile(tmp_path):
    reg = SessionRegistry(tmp_path)
    hold = {"until": None, "reason": "a build", "since": 1}
    make_session(reg, name="s", wolt="pal", hold=hold)

    def stop(seen):
        with reg.edit("s") as data:
            assert data["status"] == sessions.STOPPING
            data["title"] = "written while stopping"
        return False

    assert sessions.stop_and_settle(reg, "s", reg._read("pal", "s"), stop, None) is False
    stored = reg._read("pal", "s")
    assert (stored["status"], stored["title"], stored["hold"]) == ("running", "written while stopping", hold)


@pytest.mark.parametrize("resume_id, tmux_alive, settled", [
    ("conv-1", False, "resting"), ("", False, "stopped"), ("conv-1", True, "resting")])
def test_boot_finishes_a_stop_a_dead_control_plane_left(tmp_path, resume_id, tmux_alive, settled):
    from conftest import FakeSessionRuntime
    reg = SessionRegistry(tmp_path)
    (tmp_path / "pal").mkdir()
    make_session(reg, name="s", wolt="pal", status=sessions.STOPPING, harness_session_id=resume_id)
    runtime = FakeSessionRuntime(alive=tmux_alive)
    with mock.patch.object(sessions, "get_runtime", return_value=runtime):
        report = reg.adopt_runtime_sessions()
    assert report["settled"] == [{"session": "s", "wolt": "pal", "status": settled}]
    assert reg._read("pal", "s")["status"] == settled
    assert runtime.stops == (["s"] if tmux_alive else [])


def test_two_claimers_of_one_conversation_get_one_each(tmp_path, monkeypatch):
    """3cb6030: harness_session_id is UNIQUE; the second claimer looks again."""
    reg = SessionRegistry(tmp_path)
    for name in ("a", "b"):
        make_session(reg, name=name, wolt="pal", harness="codex")
    gate = threading.Barrier(2)

    def recover(data, taken):
        if not taken:
            gate.wait(timeout=5)  # both see the newest rollout before either writes
        return next(c for c in ("conv-1", "conv-2") if c not in taken)

    monkeypatch.setattr(sessions, "recover_resume_id", recover)
    got = {}

    def claim(name):
        data = reg._read("pal", name)
        got[name] = sessions.claim_resume_id(reg, data, lambda d: sessions.stamp_resume_id(
            reg, "pal", name, d["harness_session_id"]))

    _race(lambda: claim("a"), lambda: claim("b"))
    assert sorted(got.values()) == ["conv-1", "conv-2"]
    assert {reg._read("pal", n)["harness_session_id"] for n in "ab"} == {"conv-1", "conv-2"}
    with pytest.raises(sqlite3.IntegrityError):
        reg.update("a", harness_session_id=got["b"])


def test_an_archived_wolt_drops_out_until_it_comes_back(tmp_path):
    reg = SessionRegistry(tmp_path)
    (tmp_path / "pal").mkdir()
    make_session(reg, name="s", wolt="pal")
    (tmp_path / ".archive").mkdir()
    (tmp_path / "pal").rename(tmp_path / ".archive" / "pal")
    assert reg.get("s", check_alive=False) is None and reg.records() == []
    archive = SessionRegistry(tmp_path / ".archive", db_dir=tmp_path)
    assert [r["name"] for r in archive.records(wolt="pal")] == ["s"]
    (tmp_path / ".archive" / "pal").rename(tmp_path / "pal")
    assert reg.get("s", check_alive=False)["name"] == "s"
    assert reg.forget_wolt("pal") == 1 and reg.records() == []


def _old_session(root, wolt, name, **fields):
    folder = root / wolt / ".state" / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"name": name, "wolt": wolt, **fields}))
    (folder / f"{name}.lock").write_text("")
    return folder


def test_the_import_takes_every_file_once_and_rows_win_after(tmp_path, capsys):
    folder = _old_session(tmp_path, "pal", "new", status="resting", created_at=20,
                          harness_session_id="conv-1")
    _old_session(tmp_path, "pal", "old", status="stopped", created_at=10,
                 harness_session_id="conv-1")
    (folder / "torn.json").write_text("{")
    (folder / "half.tmp").write_text("{}")
    _old_session(tmp_path / ".archive", "nunu", "kept", status="resting", created_at=5)
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "resume-id-recovery.lock").write_text("")

    ran = lodge_db.run_imports(tmp_path, {"sessions": sessions.import_sessions})
    assert ran == {"sessions": {"imported": 2, "skipped": 2}}
    err = capsys.readouterr().err
    assert "old.json" in err and "torn.json" in err
    reg = SessionRegistry(tmp_path)
    assert reg._read("pal", "new")["harness_session_id"] == "conv-1"
    assert reg._read("pal", "old") is None
    assert not folder.exists() and not (tmp_path / ".state" / "resume-id-recovery.lock").exists()
    assert sorted(p.name for p in (tmp_path / "pal" / ".state" / "sessions.imported").iterdir()) == [
        "half.tmp", "new.json", "old.json", "torn.json"]
    assert (tmp_path / ".archive" / "nunu" / ".state" / "sessions.imported" / "kept.json").exists()

    assert lodge_db.run_imports(tmp_path, {"sessions": sessions.import_sessions}) == {}
    # A downgraded lodge wrote files again: the new session comes in, the row already here wins.
    reg.update("new", title="since the upgrade")
    _old_session(tmp_path, "pal", "new", status="running", created_at=20)
    _old_session(tmp_path, "pal", "made-meanwhile", status="resting", created_at=30)
    ran = lodge_db.run_imports(tmp_path, {"sessions": sessions.import_sessions})
    assert ran == {"sessions": {"imported": 1, "skipped": 1}}
    assert reg._read("pal", "new")["title"] == "since the upgrade"
    assert reg._read("pal", "made-meanwhile")["status"] == "resting"


def test_a_failed_import_never_stops_the_boot(tmp_path, monkeypatch):
    from woltspace import adoption

    def broken(conn, wolts_dir):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(sessions, "import_sessions", broken)
    layout = type("Layout", (), {"wolts_dir": tmp_path})()
    assert adoption.import_lodge_state(layout) == {
        "sessions": "OperationalError: disk I/O error"}
