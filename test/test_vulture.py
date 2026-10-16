"""Vulture session reaper tests — pure Python, no server or tmux required.

Tests the reaping logic in container/creatures/vulture.py with mocked
tmux and filesystem operations.

Now uses the per-wolt session model: sessions live at
wolts/{wolt}/.state/sessions/{name}.json, and the vulture iterates all
wolt directories instead of using reg.dir.

Usage: uv run pytest test/test_vulture.py -v
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


@pytest.fixture(autouse=True)
def _idle_closing_off():
    """These tests cover reaping; idle closing (on by default, 24h) has its own tests."""
    with patch("creatures.vulture.get_idle_timeout", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _vulture_state_in_tmp(tmp_path, monkeypatch):
    """Every vulture write lands in tmp_path, never the developer's live lodge.

    The module computes its log and state paths from WOLTSPACE_WOLTS_DIR at
    import, so a test run from a wolt session used to append to the real log.
    """
    from creatures import vulture
    state = tmp_path / ".vulture-state"
    monkeypatch.setattr(vulture, "STATE_DIR", state)
    monkeypatch.setattr(vulture, "LOG_FILE", state / "vulture.log")
    monkeypatch.setattr(vulture, "LAST_RUN_FILE", state / "last-run")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wolt_session(wolts_dir: Path, wolt: str, name: str,
                       status: str = "running", created_at: int = None,
                       finished_at: int = None) -> dict:
    """Write a session JSON file to the wolt's .state/sessions/ dir."""
    now = int(time.time())
    sessions_dir = wolts_dir / wolt / ".state" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "name": name,
        "wolt": wolt,
        "creature": "beaver",
        "status": status,
        "created_at": created_at or (now - 600),  # 10 min ago by default
        "finished_at": finished_at,
        "exit_code": None,
        "last_activity": now - 300,
    }
    (sessions_dir / f"{name}.json").write_text(json.dumps(data))
    return data


def _read_wolt_session(wolts_dir: Path, wolt: str, name: str) -> dict:
    return json.loads(
        (wolts_dir / wolt / ".state" / "sessions" / f"{name}.json").read_text()
    )


# ---------------------------------------------------------------------------
# Registry reaping (Pass 1)
# ---------------------------------------------------------------------------

class TestRegistryReaping:
    """Pass 1: marking dead registry entries as reaped."""

    def test_reaps_running_session_with_dead_tmux(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "dead-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "dead-session" in stats["registry_reaped"]
        data = _read_wolt_session(tmp_path, "testwolt", "dead-session")
        assert data["status"] == "reaped"

    def test_a_dead_session_that_can_resume_rests_instead(self, tmp_path):
        """A reaped session refuses messages; a resting one wakes on them."""
        from creatures.vulture import reap

        data = _make_wolt_session(tmp_path, "testwolt", "can-resume", status="running")
        data["harness_session_id"] = "abc"
        (tmp_path / "testwolt" / ".state" / "sessions" / "can-resume.json").write_text(json.dumps(data))

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            stats = reap()

        assert "can-resume" in stats["registry_reaped"]
        after = _read_wolt_session(tmp_path, "testwolt", "can-resume")
        assert after["status"] == "resting"
        assert after["rested_at"]

    def test_skips_already_completed_sessions(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "done-session", status="completed")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []

    def test_skips_sessions_still_alive_in_tmux(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "alive-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"alive-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=True), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []
        assert stats["tmux_killed"] == []

    def test_reaps_across_multiple_wolts(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "woltA", "sess-a", status="running")
        _make_wolt_session(tmp_path, "woltB", "sess-b", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "sess-a" in stats["registry_reaped"]
        assert "sess-b" in stats["registry_reaped"]


# ---------------------------------------------------------------------------
# Grace period
# ---------------------------------------------------------------------------

class TestGracePeriod:
    """Sessions younger than GRACE_PERIOD_SECONDS are never reaped."""

    def test_skips_fresh_sessions(self, tmp_path):
        from creatures.vulture import reap

        # Created 30 seconds ago — within grace period
        _make_wolt_session(tmp_path, "testwolt", "fresh-session",
                          status="running", created_at=int(time.time()) - 30)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["registry_reaped"] == []


# ---------------------------------------------------------------------------
# Protected sessions
# ---------------------------------------------------------------------------

class TestProtectedSessions:
    """The 'main' tmux session is never killed."""

    def test_main_session_never_killed(self, tmp_path):
        from creatures.vulture import reap

        # Create at least one wolt dir so iteration works
        (tmp_path / "testwolt" / ".state" / "sessions").mkdir(parents=True)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"main"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        mock_kill.assert_not_called()
        assert stats["tmux_killed"] == []


# ---------------------------------------------------------------------------
# Zombie tmux cleanup (Pass 2)
# ---------------------------------------------------------------------------

class TestZombieTmuxCleanup:
    """Pass 2: killing tmux sessions with no claude process inside."""

    def test_kills_zombie_tmux_with_running_registry(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "zombie-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"zombie-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "zombie-session" in stats["tmux_killed"]
        mock_kill.assert_called_once_with("zombie-session")
        data = _read_wolt_session(tmp_path, "testwolt", "zombie-session")
        assert data["status"] == "reaped"

    def test_kills_tmux_for_already_reaped_registry(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "leftover-session", status="reaped")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"leftover-session"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert "leftover-session" in stats["tmux_killed"]

    def test_never_touches_a_tmux_session_the_lodge_did_not_register(self, tmp_path):
        """Natively the lodge shares the owner's tmux server: these are their terminals."""
        from creatures.vulture import reap

        (tmp_path / "testwolt" / ".state" / "sessions").mkdir(parents=True)

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"145", "my-shell"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap()

        assert stats["tmux_killed"] == []
        mock_kill.assert_not_called()


# ---------------------------------------------------------------------------
# Deaths settle under the session lock
# ---------------------------------------------------------------------------

class TestDeathsAreReCheckedUnderTheLock:
    """The passes work from a snapshot taken at their start. A message can wake
    a session in between (resume_session spawns a new tmux and writes running),
    so each settle reads the record again under the session lock and asks the
    runtime again: a woken session is never written resting while it runs."""

    def test_a_session_woken_since_the_snapshot_is_not_settled(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "woken", status="running")
        runtime = MagicMock(); runtime.is_alive.return_value = True
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture.get_runtime", return_value=runtime):
            stats = reap()

        assert stats["registry_reaped"] == []
        assert _read_wolt_session(tmp_path, "testwolt", "woken")["status"] == "running"

    def test_an_agent_revived_since_the_look_is_not_killed(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "revived", status="running")
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"revived"}), \
             patch("creatures.vulture.session_has_agent_process", side_effect=[False, True]), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as kill:
            stats = reap()

        kill.assert_not_called()
        assert stats["tmux_killed"] == []
        assert _read_wolt_session(tmp_path, "testwolt", "revived")["status"] == "running"

    def test_a_leftover_resumed_since_the_look_is_not_killed(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "back", status="reaped")

        def resumed_meanwhile(_record):
            data = _read_wolt_session(tmp_path, "testwolt", "back")
            data["status"] = "running"
            (tmp_path / "testwolt" / ".state" / "sessions" / "back.json").write_text(json.dumps(data))
            return False

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"back"}), \
             patch("creatures.vulture.session_has_agent_process", side_effect=resumed_meanwhile), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as kill:
            reap()

        kill.assert_not_called()

    def test_a_message_right_after_the_settle_keeps_its_agent(self, tmp_path, monkeypatch):
        """Pass 2 used to write resting, release the lock, then kill tmux. A
        message in that gap read resting, revived the agent in the lingering
        pane, and the kill took it down. The kill happens under the lock now,
        before the write: a message that reads resting finds tmux gone and
        wakes the session in a fresh one."""
        import sessions
        from contextlib import contextmanager
        from creatures.vulture import reap

        data = _make_wolt_session(tmp_path, "testwolt", "gap", status="running")
        data["harness_session_id"] = "abc"
        path = tmp_path / "testwolt" / ".state" / "sessions" / "gap.json"
        path.write_text(json.dumps(data))
        world = {"tmux": "old", "agent": None, "killed": [], "messaged": False}

        def message_arrives():
            # resume_session wakes a resting record in its lingering tmux if
            # there is one, else in a new tmux.
            current = json.loads(path.read_text())
            if current["status"] != "resting":
                return
            world["messaged"] = True
            if world["tmux"] == "old":
                world["agent"] = "revived in the old tmux"
            else:
                world["tmux"], world["agent"] = "new", "fresh in a new tmux"
            current["status"] = "running"
            path.write_text(json.dumps(current))

        real_lock = sessions.SessionRegistry._lock

        @contextmanager
        def lock_then_message(self, wolt, name):
            with real_lock(self, wolt, name):
                yield
            if name == "gap" and not world["messaged"]:
                message_arrives()

        def kill(name):
            world["killed"].append(world["agent"])
            if world["tmux"] == "old":
                world["tmux"] = None
            return True

        monkeypatch.setattr(sessions.SessionRegistry, "_lock", lock_then_message)
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"gap"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", side_effect=kill):
            reap()

        assert world["messaged"]
        assert world["killed"] == [None], f"the kill took down {world['killed']}"
        assert world["agent"] == "fresh in a new tmux"
        assert _read_wolt_session(tmp_path, "testwolt", "gap")["status"] == "running"

    def test_a_zombie_whose_tmux_will_not_die_stays_running(self, tmp_path):
        """As /rest: a failed stop is not a settle; the next pass tries again."""
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "stubborn", status="running")
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"stubborn"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=False) as kill:
            stats = reap()

        kill.assert_called_once_with("stubborn")
        assert stats["tmux_killed"] == []
        assert _read_wolt_session(tmp_path, "testwolt", "stubborn")["status"] == "running"

    def test_a_dead_held_session_settles_and_loses_its_hold(self, tmp_path):
        """The hold gates idle rests only: the task it covered died with tmux."""
        from creatures.vulture import reap

        data = _make_wolt_session(tmp_path, "testwolt", "held-dead", status="running")
        data.update(harness_session_id="abc", hold={"until": None, "reason": "dev server", "since": 1})
        (tmp_path / "testwolt" / ".state" / "sessions" / "held-dead.json").write_text(json.dumps(data))
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            stats = reap()

        assert "held-dead" in stats["registry_reaped"]
        after = _read_wolt_session(tmp_path, "testwolt", "held-dead")
        assert after["status"] == "resting"
        assert "hold" not in after
        assert after["rest_reason"] == "tmux-session-missing"


# ---------------------------------------------------------------------------
# Conversation ids never stamped (codex, opencode, hermes pick their own)
# ---------------------------------------------------------------------------

CODEX_ID = "01a0e464-dc51-7053-a4b5-0afe1fed6bb9"
CODEX_START = 1790523810  # 2026-09-27T15:43:30Z
CODEX_START_ISO = "2026-09-27T15:43:30Z"


def _codex_session_without_id(wolts_dir: Path, monkeypatch, name: str, *,
                              rollout: bool = True) -> None:
    """A running codex session whose spawn-time discovery missed its id and,
    unless told otherwise, the rollout codex wrote for it in the wolt's own
    CODEX_HOME. No codex binary: recovery only reads these files."""
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts_dir))
    monkeypatch.setenv("CODEX_HOME", str(wolts_dir / "no-host-codex"))
    data = _make_wolt_session(wolts_dir, "testwolt", name, status="running",
                              created_at=CODEX_START)
    data.update(harness="codex", dir=str(wolts_dir / "testwolt"))
    (wolts_dir / "testwolt" / ".state" / "sessions" / f"{name}.json").write_text(json.dumps(data))
    if rollout:
        day = wolts_dir / "testwolt" / ".codex" / "sessions" / "2026" / "09" / "27"
        day.mkdir(parents=True)
        meta = {"type": "session_meta",
                "payload": {"id": CODEX_ID, "timestamp": CODEX_START_ISO, "cwd": data["dir"]}}
        (day / f"rollout-2026-09-27T15-43-30-{CODEX_ID}.jsonl").write_text(json.dumps(meta) + "\n")


class TestRecoveredConversationIds:
    """A dead session whose id was never stamped gets it recovered from the
    engine's files first, as an idle rest does, so it rests instead of being
    reaped: a message can still wake it."""

    def test_tmux_gone_rests_with_the_recovered_id(self, tmp_path, monkeypatch):
        from creatures.vulture import reap

        _codex_session_without_id(tmp_path, monkeypatch, "codex-dead")
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            stats = reap()

        assert "codex-dead" in stats["registry_reaped"]
        after = _read_wolt_session(tmp_path, "testwolt", "codex-dead")
        assert after["status"] == "resting"
        assert after["harness_session_id"] == CODEX_ID
        assert after["rest_reason"] == "tmux-session-missing"

    def test_agent_gone_rests_with_the_recovered_id(self, tmp_path, monkeypatch):
        from creatures.vulture import reap

        _codex_session_without_id(tmp_path, monkeypatch, "codex-zombie")
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value={"codex-zombie"}), \
             patch("creatures.vulture.session_has_agent_process", return_value=False), \
             patch("creatures.vulture._kill_tmux_session", return_value=True) as kill:
            reap()

        kill.assert_called_once_with("codex-zombie")
        after = _read_wolt_session(tmp_path, "testwolt", "codex-zombie")
        assert after["status"] == "resting"
        assert after["harness_session_id"] == CODEX_ID
        assert after["rest_reason"] == "agent-process-missing"

    def test_nothing_to_recover_is_still_reaped(self, tmp_path, monkeypatch):
        from creatures.vulture import reap

        _codex_session_without_id(tmp_path, monkeypatch, "codex-empty", rollout=False)
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            reap()

        after = _read_wolt_session(tmp_path, "testwolt", "codex-empty")
        assert after["status"] == "reaped"
        assert not after.get("harness_session_id")

    def test_recovery_runs_before_the_session_lock(self, tmp_path, monkeypatch):
        """claim_resume_id holds the lodge-wide recovery lock, which resume_session
        and the id poller take before the session lock: never inside it."""
        import sessions
        from creatures import vulture
        from contextlib import contextmanager

        _codex_session_without_id(tmp_path, monkeypatch, "codex-order")
        held = []
        real_lock = sessions.SessionRegistry._lock

        @contextmanager
        def tracking_lock(self, *args):
            with real_lock(self, *args):
                held.append(True)
                try:
                    yield
                finally:
                    held.pop()

        seen = []
        real_recover = sessions.recover_resume_id

        def recover(data, taken):
            seen.append(bool(held))
            return real_recover(data, taken)

        monkeypatch.setattr(sessions.SessionRegistry, "_lock", tracking_lock)
        monkeypatch.setattr(sessions, "recover_resume_id", recover)
        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()):
            vulture.reap()

        assert seen and not any(seen), "recovery ran inside the session lock"
        assert _read_wolt_session(tmp_path, "testwolt", "codex-order")["status"] == "resting"


# ---------------------------------------------------------------------------
# The first pass waits for the lodge API
# ---------------------------------------------------------------------------

class _Stop(BaseException):
    """Ends run_loop's endless loop in a test (it catches Exception only)."""


class TestWaitsForTheApi:
    """Idle rests go through the lodge's /rest. At a lodge restart the vulture
    starts before the API answers, and every rest of its first pass failed."""

    def test_the_first_pass_waits_for_health(self, monkeypatch):
        from creatures import vulture
        order = []
        monkeypatch.setattr(vulture, "_wait_for_api", lambda: order.append("wait") or True)
        monkeypatch.setattr(vulture, "reap", lambda dry_run=False: order.append("reap") or
                            {"registry_reaped": [], "tmux_killed": [], "sessions_rested": []})

        def stop(_seconds):
            raise _Stop

        monkeypatch.setattr(vulture.time, "sleep", stop)
        with pytest.raises(_Stop):
            vulture.run_loop(interval=600)
        assert order == ["wait", "reap"]

    def test_wait_gives_up_after_a_minute_and_says_so(self, monkeypatch):
        from creatures import vulture
        now = [0.0]
        monkeypatch.setattr(vulture, "_api_ready", lambda: False)
        assert vulture._wait_for_api(clock=lambda: now[0],
                                     sleep=lambda s: now.__setitem__(0, now[0] + s)) is False
        assert 60 <= now[0] <= 62
        assert "not answering" in vulture.LOG_FILE.read_text()

    def test_health_is_asked_of_the_configured_api(self, monkeypatch):
        from creatures import vulture
        from urllib import error as urlerror
        asked = []

        class Answer:
            status = 200
            def __enter__(self):
                return self
            def __exit__(self, *exc):
                return False

        def urlopen(url, timeout=None):
            asked.append(url)
            return Answer()

        monkeypatch.setenv("WOLTSPACE_API", "http://127.0.0.1:7794/")
        monkeypatch.setattr(vulture.urlrequest, "urlopen", urlopen)
        assert vulture._api_ready() is True
        assert asked == ["http://127.0.0.1:7794/health"]

        def refused(url, timeout=None):
            raise urlerror.URLError("connection refused")

        monkeypatch.setattr(vulture.urlrequest, "urlopen", refused)
        assert vulture._api_ready() is False


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------

class TestDryRun:
    """Dry run mode identifies but doesn't mutate anything."""

    def test_dry_run_does_not_write(self, tmp_path):
        from creatures.vulture import reap

        _make_wolt_session(tmp_path, "testwolt", "victim-session", status="running")

        with patch("creatures.vulture.WOLTS_DIR", tmp_path), \
             patch("creatures.vulture._tmux_sessions", return_value=set()), \
             patch("creatures.vulture._kill_tmux_session") as mock_kill, \
             patch("creatures.vulture.STATE_DIR", tmp_path / ".space" / "vulture"), \
             patch("creatures.vulture.LAST_RUN_FILE", tmp_path / "last-run"), \
             patch("creatures.vulture.LOG_FILE", tmp_path / "vulture.log"):
            stats = reap(dry_run=True)

        assert "victim-session" in stats["registry_reaped"]
        mock_kill.assert_not_called()
        # Original file should still say "running"
        data = _read_wolt_session(tmp_path, "testwolt", "victim-session")
        assert data["status"] == "running"


def test_idle_closing_skips_sessions_that_cannot_resume(tmp_path):
    """A codex session with no stamped conversation id must never be rested: it could not come back."""
    import creatures.vulture as vulture
    reg = MagicMock()
    old = int(time.time()) - 10 * 86400
    reg.list.return_value = [
        {"name": "no-id", "wolt": "w", "status": "running", "created_at": old},
        {"name": "has-id", "wolt": "w", "status": "running", "created_at": old, "harness_session_id": "abc"},
    ]
    runtime = MagicMock()
    runtime.last_keys.return_value = {"no-id": old, "has-id": old}
    with patch.object(vulture, "get_runtime", return_value=runtime), \
         patch.object(vulture, "_rest_via_api", return_value=True) as rest:
        rested = vulture._rest_idle_sessions(reg, {"no-id", "has-id"}, int(time.time()), 86400, dry_run=False)
    assert rested == ["has-id"]
    rest.assert_called_once()
