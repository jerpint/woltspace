"""No paste until the agent's input is live (#555, part B).

Finding the agent process is not enough: Claude Code's process exists about a
second before its input handler mounts, and a paste in that gap is lost. Every
TUI we run takes its terminal raw when input goes live, so delivery waits for
the pane's tty to leave cooked mode — outside any session lock — and refuses,
rather than pastes, when it never does.

Fake runtimes and a fake clock throughout: no tmux, no real waiting.
"""

import json
import os
import pty
import subprocess
import sys
import time
import tty
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import sessions  # noqa: E402
from harnesses import LAUNCHING_NAMES  # noqa: E402
from runtime_context import RuntimeContext  # noqa: E402
from session_runtime import (  # noqa: E402
    RuntimeHandle,
    SessionRuntime,
    TmuxSessionRuntime,
    set_runtime,
)

from conftest import FakeSessionRuntime  # noqa: E402


NAME = "pal-boot-den-abc123"
PANE = "%5"


# ---------------------------------------------------------------------------
# The tty check, on a real pty
# ---------------------------------------------------------------------------

@pytest.fixture
def pty_pair():
    master, slave = pty.openpty()
    try:
        yield SimpleNamespace(slave=slave, path=os.ttyname(slave))
    finally:
        os.close(slave)
        os.close(master)


class TestTtyIsRaw:
    def test_a_cooked_terminal_is_not_raw(self, pty_pair):
        from session_runtime import tty_is_raw
        assert tty_is_raw(pty_pair.path) is False

    def test_a_terminal_set_raw_is_raw(self, pty_pair):
        from session_runtime import tty_is_raw
        tty.setraw(pty_pair.slave)
        assert tty_is_raw(pty_pair.path) is True

    def test_a_missing_tty_is_unknown(self, tmp_path):
        from session_runtime import tty_is_raw
        assert tty_is_raw(str(tmp_path / "no-such-tty")) is None

    def test_a_file_that_is_not_a_terminal_is_unknown(self, tmp_path):
        from session_runtime import tty_is_raw
        plain = tmp_path / "plain"
        plain.write_text("")
        assert tty_is_raw(str(plain)) is None


# ---------------------------------------------------------------------------
# The probe reads the exact persisted pane
# ---------------------------------------------------------------------------

class _Runner:
    """subprocess.run stand-in answering `display-message` with a tty path."""

    def __init__(self, tty_path="", error=None):
        self.tty_path = tty_path
        self.error = error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(stdout=f"{self.tty_path}\n", returncode=0)


class TestInputReadyProbe:
    def _runtime(self, runner):
        return TmuxSessionRuntime(RuntimeContext(tmux_bin="tmux-test"), runner=runner)

    def test_is_part_of_the_runtime_contract(self):
        assert "input_ready" in SessionRuntime.__dict__
        assert isinstance(FakeSessionRuntime(), SessionRuntime)

    def test_reads_the_tty_of_the_handles_own_pane(self, pty_pair):
        runner = _Runner(pty_pair.path)
        handle = RuntimeHandle(NAME, "persisted-tmux", "%42")

        assert self._runtime(runner).input_ready(handle) is False
        # The pane id, never the session: tmux resolves a session name to
        # whichever pane is active, which need not be the one we paste into.
        assert runner.calls == [
            ["tmux-test", "display-message", "-p", "-t", "%42", "#{pane_tty}"],
        ]

    def test_a_raw_pane_is_ready(self, pty_pair):
        tty.setraw(pty_pair.slave)
        runner = _Runner(pty_pair.path)
        handle = RuntimeHandle(NAME, NAME, "%42")
        assert self._runtime(runner).input_ready(handle) is True

    @pytest.mark.parametrize("pane", ["", NAME])
    def test_without_an_exact_pane_there_is_no_answer(self, pane):
        runner = _Runner("/dev/pts/0")
        handle = RuntimeHandle(NAME, NAME, pane)
        assert self._runtime(runner).input_ready(handle) is None
        assert runner.calls == []

    def test_a_pane_tmux_cannot_find_is_unknown(self):
        runner = _Runner(error=subprocess.CalledProcessError(1, "tmux"))
        assert self._runtime(runner).input_ready(RuntimeHandle(NAME, NAME, "%42")) is None

    def test_a_pane_without_a_tty_is_unknown(self):
        runner = _Runner("")
        assert self._runtime(runner).input_ready(RuntimeHandle(NAME, NAME, "%42")) is None


# ---------------------------------------------------------------------------
# The agent's age, from ps etime
# ---------------------------------------------------------------------------

class TestEtime:
    @pytest.mark.parametrize("raw,seconds", [
        ("05", 5),
        ("00:07", 7),
        ("   00:07", 7),
        ("1:02:03", 3723),
        ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5),
    ])
    def test_every_shape_ps_prints(self, raw, seconds):
        from session_runtime import _etime_seconds
        assert _etime_seconds(raw) == seconds

    @pytest.mark.parametrize("raw", ["", "abc", "1:2:3:4", "12:", "-5", "1-2-3", "x-01:00", "0:0a"])
    def test_garbage_is_unknown(self, raw):
        from session_runtime import _etime_seconds
        assert _etime_seconds(raw) is None


class _PsRunner:
    """subprocess.run stand-in for one session's panes and a ps snapshot."""

    def __init__(self, panes, comm, args, etime):
        self.by_key = {
            "list-panes": panes,
            "pid=,ppid=,comm=": comm,
            "pid=,args=": args,
            "pid=,etime=": etime,
        }
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        key = command[1] if command[1] == "list-panes" else command[-1]
        return SimpleNamespace(stdout=self.by_key.get(key, ""), returncode=0)


# Two panes. %42 is the agent's: bash → run-session.sh → claude (an hour up)
# and a second, younger claude under it. %43 holds an even younger claude
# that must never be counted for %42.
_PANES = f"{NAME}\t%42\t100\t1\n{NAME}\t%43\t300\t0\n"
_COMM = (
    "100 1 bash\n101 100 bash\n102 101 claude\n103 102 claude\n"
    "300 1 bash\n301 300 claude\n"
)
_ARGS = "101 bash /opt/woltspace/container/bin/run-session.sh pal\n"
_ETIME = "100 01:00:10\n101 01:00:09\n102 01:00:08\n103 00:04\n300 05\n301 00:01\n"


class TestAgentAgeProbe:
    def _runtime(self, runner):
        return TmuxSessionRuntime(RuntimeContext(tmux_bin="tmux"), runner=runner)

    def test_is_part_of_the_runtime_contract(self):
        assert "agent_age" in SessionRuntime.__dict__

    def test_the_youngest_matching_process_in_the_pane_sets_the_age(self):
        runner = _PsRunner(_PANES, _COMM, _ARGS, _ETIME)
        handle = RuntimeHandle(NAME, NAME, "%42")
        assert self._runtime(runner).agent_age(handle, {"claude"}) == 4

    def test_another_panes_agent_never_counts(self):
        runner = _PsRunner(_PANES, _COMM, _ARGS, _ETIME)
        handle = RuntimeHandle(NAME, NAME, "%43")
        assert self._runtime(runner).agent_age(handle, {"claude"}) == 1

    def test_script_names_match_like_agent_detection(self):
        runner = _PsRunner(_PANES, _COMM, _ARGS, _ETIME)
        handle = RuntimeHandle(NAME, NAME, "%42")
        assert self._runtime(runner).agent_age(handle, {"run-session.sh"}) == 3609

    def test_no_wanted_process_is_unknown(self):
        runner = _PsRunner(_PANES, _COMM, _ARGS, _ETIME)
        handle = RuntimeHandle(NAME, NAME, "%42")
        assert self._runtime(runner).agent_age(handle, {"codex"}) is None

    def test_an_unreadable_etime_is_unknown(self):
        runner = _PsRunner(_PANES, _COMM, _ARGS, "102 soon\n103 later\n")
        handle = RuntimeHandle(NAME, NAME, "%42")
        assert self._runtime(runner).agent_age(handle, {"claude"}) is None

    @pytest.mark.parametrize("pane", ["", NAME, "%99"])
    def test_without_the_exact_pane_there_is_no_answer(self, pane):
        runner = _PsRunner(_PANES, _COMM, _ARGS, _ETIME)
        handle = RuntimeHandle(NAME, NAME, pane)
        assert self._runtime(runner).agent_age(handle, {"claude"}) is None
        assert not [c for c in runner.calls if c[0] == "ps"]


# ---------------------------------------------------------------------------
# A session that boots on a script
# ---------------------------------------------------------------------------

POLL = 0.25


class Clock:
    """Stands in for the sessions module's `time`: sleeping advances it."""

    def __init__(self):
        self.now = 0.0
        self.events = []

    def monotonic(self):
        return self.now

    def time(self):
        return time.time()

    def sleep(self, seconds):
        self.events.append(("sleep", seconds))
        self.now += seconds


class BootingRuntime(FakeSessionRuntime):
    """One pane whose processes, terminal mode and agent age follow a script.

    Step i holds from clock time i × 0.25 s, one poll each, and the last step
    holds for good. Each step is (process, raw): process is "agent" (the
    harness is up, under run-session.sh), "shim" (only run-session.sh, still
    launching) or "none" (a husk); raw is what the pane's terminal reports.
    `born` is the clock time the agent process started — by default a minute
    before the test, an agent long up — or None when its age can't be read.
    """

    def __init__(self, clock, steps, *, alive=True, born=-60.0):
        super().__init__(alive=alive, panes=[(PANE, "100", True)])
        self.clock = clock
        self.steps = steps
        self.born = born

    @property
    def step(self):
        return self.steps[min(int(self.clock.now / POLL), len(self.steps) - 1)]

    def agent_age(self, handle, process_names):
        age = None
        if self.step[0] == "agent" and self.born is not None:
            age = int(self.clock.now - self.born)
        self.clock.events.append(("age", handle.pane_id, age))
        return age

    def _matching_panes_for_handle(self, handle, process_names):
        panes = self.panes_for_session(handle.tmux_session_name)
        if not panes:
            return None
        process, _raw = self.step
        if process == "agent":
            return panes
        if process == "shim" and set(process_names) & LAUNCHING_NAMES:
            return panes
        return []

    def input_ready(self, handle):
        raw = self.step[1]
        self.clock.events.append(("probe", handle.pane_id, raw))
        return raw

    def paste(self, handle, text, settle=0.0, *, process_names=None):
        super().paste(handle, text, settle, process_names=process_names)
        self.clock.events.append(("paste", self.pastes[-1][0]))


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    """One claude session, stamped and running, with a recording registry write."""
    import paths

    monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)
    (tmp_path / "pal" / "wolt").mkdir(parents=True)
    reg = sessions.SessionRegistry(tmp_path)
    reg.create(NAME, wolt="pal", harness="claude", dir=str(tmp_path / "pal"))
    reg.update(
        NAME, wolt="pal",
        harness_session_id=str(uuid.uuid4()),
        runtime=RuntimeHandle(NAME, NAME, PANE).to_record(),
    )

    clock = Clock()
    monkeypatch.setattr(sessions, "time", clock)

    real_edit = sessions.SessionRegistry.edit

    @contextmanager
    def recording_edit(self, name, **kwargs):
        clock.events.append(("lock",))
        with real_edit(self, name, **kwargs) as data:
            yield data

    monkeypatch.setattr(sessions.SessionRegistry, "edit", recording_edit)
    real_touch = sessions.SessionRegistry.touch

    def recording_touch(self, name, **kwargs):  # delivery's claim
        clock.events.append(("lock",))
        return real_touch(self, name, **kwargs)

    monkeypatch.setattr(sessions.SessionRegistry, "touch", recording_touch)

    installed = []

    def boot(*steps, alive=True, born=-60.0):
        runtime = BootingRuntime(clock, list(steps), alive=alive, born=born)
        set_runtime(runtime)
        installed.append(runtime)
        return runtime

    yield SimpleNamespace(
        reg=reg, clock=clock, events=clock.events, boot=boot,
        record_text=lambda: json.dumps(reg._read("pal", NAME), sort_keys=True),
    )
    if installed:
        set_runtime(None)


def _pastes(events):
    return [e for e in events if e[0] == "paste"]


# ---------------------------------------------------------------------------
# deliver_message — inter-wolt messages and the lodge's /message
# ---------------------------------------------------------------------------

class TestDeliverWaitsForInput:
    def test_a_young_agent_waits_out_its_age_cooked_or_raw(self, lodge):
        """The agent execs at 0 and goes raw at 0.5 s. Neither tells: Claude
        drops input until its composer mounts, so the paste waits for the
        agent to be AGENT_INPUT_READY_AGE old."""
        runtime = lodge.boot(
            ("agent", False), ("agent", False), ("agent", True), born=0.0,
        )

        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 0), ("sleep", 3),
            ("age", PANE, 3), ("probe", PANE, True),
            # The lock is taken only once the wait is over.
            ("lock",),
            ("paste", PANE),
        ]
        assert [text for _pane, text, _settle in runtime.pastes] == ["hello"]

    def test_raw_at_the_first_look_but_young_waits_out_the_age(self, lodge):
        runtime = lodge.boot(("agent", True), born=-1.0)

        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 1), ("sleep", 2),
            ("age", PANE, 3), ("probe", PANE, True),
            ("lock",), ("paste", PANE),
        ]
        assert len(runtime.pastes) == 1

    def test_an_agent_raw_and_old_enough_pays_no_wait(self, lodge, capsys):
        lodge.boot(("agent", True))
        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 60), ("probe", PANE, True), ("lock",), ("paste", PANE),
        ]
        assert capsys.readouterr().err == ""

    def test_an_old_cooked_agent_is_pasted_into_and_says_so(self, lodge, capsys):
        """Raw adds nothing once the agent is old, a cooked reader buffers a
        paste and its Enter fine, and a harness that never goes raw must not
        have every message refused."""
        runtime = lodge.boot(("agent", False))

        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 60), ("probe", PANE, False), ("lock",), ("paste", PANE),
        ]
        assert [text for _pane, text, _settle in runtime.pastes] == ["hello"]
        log = capsys.readouterr().err.strip().splitlines()
        assert len(log) == 1 and "old enough but its terminal is not raw" in log[0]

    def test_an_unreadable_terminal_on_an_old_agent_pastes_and_says_so(self, lodge, capsys):
        lodge.boot(("agent", None))
        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 60), ("probe", PANE, None), ("lock",), ("paste", PANE),
        ]
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_an_unreadable_age_pastes_at_once_and_says_so(self, lodge, capsys):
        lodge.boot(("agent", True), born=None)
        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        assert res["status"] == "delivered"
        assert lodge.events == [("age", PANE, None), ("lock",), ("paste", PANE)]
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_a_session_still_launching_for_the_whole_wait_gets_nothing(self, lodge):
        runtime = lodge.boot(("shim", None))

        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)

        assert res == {
            "status": "agent-booting",
            "session": NAME,
            "detail": "the agent is still booting",
        }
        assert runtime.pastes == []
        # The whole timeout, polled every quarter second...
        assert lodge.clock.now == 15.0
        assert lodge.events.count(("sleep", 0.25)) == 60
        # ...and not one moment of it under the session lock.
        last_sleep = max(i for i, e in enumerate(lodge.events) if e[0] == "sleep")
        assert all(i > last_sleep for i, e in enumerate(lodge.events) if e == ("lock",))

    def test_launching_then_the_agent_is_one_paste_once_it_is_old(self, lodge):
        runtime = lodge.boot(
            ("shim", None), ("shim", None), ("agent", False), ("agent", True),
            born=0.5,
        )

        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("sleep", 0.25), ("sleep", 0.25),
            ("age", PANE, 0), ("sleep", 3),
            ("age", PANE, 3), ("probe", PANE, True),
            ("lock",), ("paste", PANE),
        ]
        assert [text for _pane, text, _settle in runtime.pastes] == ["hello"]

    def test_the_lock_waits_for_readiness(self, lodge):
        lodge.boot(("agent", False), ("agent", False), ("agent", True), born=0.0)
        sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        first_lock = lodge.events.index(("lock",))
        ready = lodge.events.index(("age", PANE, 3))
        assert ready < first_lock

    def test_a_husk_is_answered_without_waiting(self, lodge):
        """Nothing is booting, so there is nothing to wait for: the caller
        hears agent-gone at once and can resume."""
        lodge.boot(("none", None))
        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        assert res["status"] == "agent-gone"
        assert lodge.clock.now == 0.0

    def test_a_dead_session_is_answered_without_waiting(self, lodge):
        lodge.boot(("none", None), alive=False)
        res = sessions.deliver_message(NAME, "hello", registry=lodge.reg)
        assert res["status"] == "session-dead"
        assert lodge.clock.now == 0.0


# ---------------------------------------------------------------------------
# resume_session — Telegram and Slack messages to a session that exists
# ---------------------------------------------------------------------------

class TestResumeWaitsForInput:
    def test_a_young_agent_waits_out_its_age_cooked_or_raw(self, lodge):
        runtime = lodge.boot(
            ("agent", False), ("agent", False), ("agent", True), born=0.0,
        )

        res = sessions.resume_session(NAME, "fix the bug")

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 0), ("sleep", 3),
            ("age", PANE, 3), ("probe", PANE, True),
            # The paste happens under the session lock, so a rest can't stop
            # the pane under it; after the wait, which never runs under it.
            ("lock",), ("paste", PANE),
        ]
        assert [text for _pane, text, _settle in runtime.pastes] == ["fix the bug"]

    def test_raw_at_the_first_look_but_young_waits_out_the_age(self, lodge):
        lodge.boot(("agent", True), born=-1.0)

        res = sessions.resume_session(NAME, "fix the bug")

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 1), ("sleep", 2),
            ("age", PANE, 3), ("probe", PANE, True),
            ("lock",), ("paste", PANE),
        ]

    def test_a_revived_agent_under_an_old_record_waits_on_its_own_age(self, lodge):
        """A revive puts a new pid under a record that is a day old. The
        record's timestamps say nothing about the agent; its process does."""
        record = lodge.reg.get(NAME, wolt="pal", check_alive=False)
        record["created_at"] = record["last_activity"] = int(time.time()) - 86400
        lodge.reg._write("pal", NAME, record)
        lodge.boot(("agent", True), born=-0.5)

        res = sessions.resume_session(NAME, "fix the bug")

        assert res["status"] == "delivered"
        assert lodge.events[:3] == [("age", PANE, 0), ("sleep", 3), ("age", PANE, 3)]
        assert len(_pastes(lodge.events)) == 1

    def test_an_agent_raw_and_old_enough_pays_no_wait(self, lodge):
        lodge.boot(("agent", True))
        res = sessions.resume_session(NAME, "fix the bug")
        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 60), ("probe", PANE, True), ("lock",), ("paste", PANE),
        ]

    def test_an_old_cooked_agent_is_pasted_into_and_says_so(self, lodge, capsys):
        runtime = lodge.boot(("agent", False))

        res = sessions.resume_session(NAME, "fix the bug")

        assert res["status"] == "delivered"
        assert lodge.events == [
            ("age", PANE, 60), ("probe", PANE, False), ("lock",), ("paste", PANE),
        ]
        assert [text for _pane, text, _settle in runtime.pastes] == ["fix the bug"]
        log = capsys.readouterr().err.strip().splitlines()
        assert len(log) == 1 and "old enough but its terminal is not raw" in log[0]

    def test_an_unreadable_terminal_on_an_old_agent_pastes_and_says_so(self, lodge, capsys):
        lodge.boot(("agent", None))
        res = sessions.resume_session(NAME, "fix the bug")
        assert res["status"] == "delivered"
        assert lodge.events[:4] == [("age", PANE, 60), ("probe", PANE, None), ("lock",), ("paste", PANE)]
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_an_unreadable_age_pastes_at_once_and_says_so(self, lodge, capsys):
        lodge.boot(("agent", True), born=None)
        res = sessions.resume_session(NAME, "fix the bug")
        assert res["status"] == "delivered"
        assert lodge.events[:3] == [("age", PANE, None), ("lock",), ("paste", PANE)]
        assert len(capsys.readouterr().err.strip().splitlines()) == 1

    def test_a_booting_session_is_waited_for_not_relaunched(self, lodge):
        """Only run-session.sh is up and the conversation id is already
        stamped. Relaunching here pasted `run-session.sh --resume` into the
        pane the agent was still booting in."""
        runtime = lodge.boot(
            ("shim", None), ("agent", False), ("agent", True), born=0.25,
        )

        res = sessions.resume_session(NAME, "fix the bug")

        assert res["status"] == "delivered"
        assert [text for _pane, text, _settle in runtime.pastes] == ["fix the bug"]
        assert runtime.spawns == [] and runtime.in_session_spawns == []

    def test_a_booting_session_with_nothing_to_send_says_so(self, lodge):
        runtime = lodge.boot(("shim", None))

        res = sessions.resume_session(NAME, "")

        assert res["status"] == "delivered"
        assert res["detail"] == "agent booting, nothing to send"
        assert runtime.pastes == []
        assert runtime.spawns == [] and runtime.in_session_spawns == []
        assert lodge.clock.now == 0.0

    def test_a_session_still_launching_for_the_whole_wait_gets_nothing(self, lodge):
        runtime = lodge.boot(("shim", None))
        before = lodge.record_text()

        with pytest.raises(sessions.ResumeFailed, match="still booting"):
            sessions.resume_session(NAME, "fix the bug")

        assert runtime.pastes == []
        assert runtime.spawns == [] and runtime.in_session_spawns == []
        assert lodge.record_text() == before
        assert ("lock",) not in lodge.events
        assert lodge.clock.now == 15.0


class TestTelegramToABootingSession:
    def test_two_messages_back_to_back_are_two_pastes_and_no_relaunch(
        self, lodge, tmp_path, monkeypatch
    ):
        from bot import core

        monkeypatch.setattr(core, "BOT_LOG_DIR", tmp_path)
        runtime = lodge.boot(
            ("shim", None), ("shim", None), ("agent", False), ("agent", True),
            born=0.5,
        )

        first = core.message_session(NAME, "are you there?")
        second = core.message_session(NAME, "and one more thing")

        assert first["ok"] is True and second["ok"] is True
        assert [text for _pane, text, _settle in runtime.pastes] == [
            "are you there?", "and one more thing",
        ]
        assert runtime.spawns == [] and runtime.in_session_spawns == []
        # The second message found the agent raw and already old: no wait.
        second_paste = [i for i, e in enumerate(lodge.events) if e[0] == "paste"][1]
        first_paste = [i for i, e in enumerate(lodge.events) if e[0] == "paste"][0]
        assert not [e for e in lodge.events[first_paste:second_paste] if e[0] == "sleep"]


# ---------------------------------------------------------------------------
# The lodge's /message route: one wait per message
# ---------------------------------------------------------------------------

@contextmanager
def _unlocked(*_args):
    yield


class TestMessageRouteOnABootingAgent:
    def _client(self):
        import server.app as server_app
        from starlette.testclient import TestClient
        return server_app, TestClient(server_app.app, base_url="http://localhost:7777")

    def test_agent_booting_is_409_and_never_resumes(self, monkeypatch):
        server_app, client = self._client()
        booting = {"status": "agent-booting", "session": NAME,
                   "detail": "the agent is still booting"}
        monkeypatch.setattr(server_app, "deliver_message", Mock(return_value=booting))
        resumed = Mock()
        monkeypatch.setattr(server_app, "resume_session", resumed)
        registry = Mock()
        registry.edit = _unlocked
        registry.get.return_value = {"name": NAME, "wolt": "pal", "status": "running"}
        monkeypatch.setattr("sessions.SessionRegistry", Mock(return_value=registry))

        response = client.post(f"/sessions/{NAME}/message", json={"text": "hello"})

        assert response.status_code == 409
        assert response.json() == {"ok": False, **booting}
        resumed.assert_not_called()

    def test_a_session_still_launching_waits_once_and_answers_409(self, lodge, monkeypatch):
        server_app, client = self._client()
        monkeypatch.setattr(server_app, "WOLTS_DIR", lodge.reg.wolts_dir)
        resumed = Mock(return_value={"name": NAME, "status": "revived"})
        monkeypatch.setattr(server_app, "resume_session", resumed)
        runtime = lodge.boot(("shim", None))

        response = client.post(f"/sessions/{NAME}/message", json={"text": "hello"})

        assert response.status_code == 409
        assert response.json()["status"] == "agent-booting"
        resumed.assert_not_called()
        assert runtime.pastes == []
