"""The inbox's pasted hint (the default): the mode a launch records, the waits outside every lock,
the guarded paste and its outcomes. Real lodge.db and flock, test_inbox_wake's fake tmux and transport."""

import json
import re
import subprocess
import sys
import types
from contextlib import contextmanager

import pytest

from inbox_support import rows
from test_inbox_wake import held, lodge, mail, record, wake_state  # noqa: F401  (lodge is a fixture)

import inbox
import inbox_wake
import sessions
from session_runtime import RuntimeHandle


def config(lodge, **inbox_section):
    (lodge.root / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True, **inbox_section}}))


@pytest.fixture
def paste(lodge, monkeypatch):
    """Paste mode, and the terminal controlled: readiness, quiet, the agent's pane, the paste."""
    config(lodge)
    state = types.SimpleNamespace(ready=True, agent="%1", error=None, during_quiet=None, pastes=[], waits=[],
                                  schedules=[], in_paste_lock=False, live=None)
    real_lock = sessions._paste_lock

    @contextmanager
    def paste_lock(session, root):
        with real_lock(session, root):
            state.in_paste_lock = True
            try:
                yield
            finally:
                state.in_paste_lock = False

    def ready(target, harness, timeout=None):
        state.waits.append(("ready", held(lodge.root)))
        return RuntimeHandle.from_record(target) if state.ready else None

    def quiet(name):
        state.waits.append(("quiet", held(lodge.root)))
        (state.during_quiet or (lambda: None))()

    def agent(target, harness=None, include_launching=True):
        return RuntimeHandle.from_record(target).at_pane(state.agent) if state.agent else None

    def typed(target, text, settle=0.0):
        if state.error:
            raise state.error
        state.pastes.append((target.pane_id, text, settle, held(lodge.root), state.in_paste_lock))

    monkeypatch.setattr(sessions, "_paste_lock", paste_lock)
    monkeypatch.setattr(sessions, "_await_input_ready", ready)
    monkeypatch.setattr(inbox_wake, "_quiet", quiet)
    monkeypatch.setattr(sessions, "resolve_agent_handle", agent)
    monkeypatch.setattr(sessions, "_tmux_paste", typed)
    monkeypatch.setattr(inbox_wake, "_schedule", lambda root, wolt, once=False: state.schedules.append(once))
    monkeypatch.setattr(sessions, "resolve_active_session", lambda wolt, registry=None: state.live)
    return state


def live(lodge, paste, name="pal-live"):
    sessions.start_session(wolt="pal", name=name)
    paste.live = name
    return record(lodge, name)


def outcomes(lodge):
    return [(o["how"], o.get("reason")) for o in wake_state(lodge).get("outcomes", [])]


# -- the mode, chosen at each real launch ---------------------------------------------

@pytest.mark.parametrize("flag, mode", [({}, "paste"), ({"native_hints": False}, "paste"),
                                        ({"native_hints": True}, "native")])
def test_a_launch_records_its_mode_and_paste_mode_prepares_nothing(lodge, flag, mode):
    config(lodge, **flag)
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    (_, marker, _), = lodge.calls.spawned
    row = record(lodge, "pal-a-b-c")
    assert marker["inbox_adapter"] == row["inbox_adapter"] == mode and row["attempt"] == marker["attempt"]
    assert bool(marker["native"]) == bool(lodge.calls.prepared) == ("native" in row) == (mode == "native")


def test_a_paste_mode_engine_command_is_the_stock_one(lodge, monkeypatch):
    monkeypatch.delitem(sys.modules, "native_adapters")
    config(lodge)
    sessions.start_session(wolt="pal", name="pal-a-b-c")
    def command():  # a fresh session id each call
        return re.sub(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", "ID",
                      sessions.prepare_session_command("pal-a-b-c", "spawn", "hi"))
    pasted = command()
    (lodge.root / "woltspace.json").write_text("{}")
    assert pasted == command()
    assert "WOLTSPACE_NATIVE" not in pasted and "--settings" not in pasted


@pytest.mark.parametrize("bad", ["true", 1, None, {}])
def test_a_malformed_switch_refuses_before_any_launch_effect(lodge, monkeypatch, bad):
    config(lodge, native_hints=True)
    sessions.start_session(wolt="pal", name="pal-old")
    lodge.registry.update("pal-old", status="resting", harness_session_id="conv-1")
    before = record(lodge, "pal-old")
    config(lodge, native_hints=bad)
    with pytest.raises(inbox.Invalid):
        sessions.start_session(wolt="pal", name="pal-new")
    assert record(lodge, "pal-new") is None and len(lodge.calls.spawned) == 1
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    with pytest.raises(inbox.Invalid):
        sessions.resume_session("pal-old")
    assert record(lodge, "pal-old")["native"] == before["native"] and not lodge.calls.retired
    lodge.registry.update("pal-old", wake_inhibit=True)  # not resumable: the wake starts one, which refuses
    monkeypatch.setattr(inbox_wake, "_schedule", lambda *a, **k: None)
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    assert outcomes(lodge) == [("blocked", "not_startable")] and len(lodge.calls.spawned) == 1


def test_a_live_reuse_keeps_its_mode_and_a_relaunch_chooses_again(lodge, paste, monkeypatch):
    first = live(lodge, paste)
    config(lodge, native_hints=True)
    sessions.resume_session("pal-live")  # the agent is running: reused, nothing pasted, no new attempt
    row = record(lodge, "pal-live")
    assert (row["attempt"], row["inbox_adapter"], "native" in row) == (first["attempt"], "paste", False)
    lodge.registry.update("pal-live", harness_session_id="conv-1")
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    monkeypatch.setattr(sessions, "_await_agent", lambda *a, **k: True)
    sessions.resume_session("pal-live")
    row = record(lodge, "pal-live")
    assert row["attempt"] != first["attempt"] and row["inbox_adapter"] == "native"
    assert row["native"]["attempt"] == row["attempt"] and not paste.pastes


def test_the_transport_comes_from_the_record():
    # explicit native never falls back to a paste; a record from before the mode with a locator is native
    assert [inbox_wake._mode(r) for r in ({"inbox_adapter": "paste", "native": 1}, {"inbox_adapter": "native"},
                                          {"native": 1}, {}, {"inbox_adapter": "x"})] == [
        "paste", "native", "native", None, None]


def quiet_run(lodge, monkeypatch, keys):
    """Run _quiet on a fake clock; keys(t, timeout) is what tmux answers at t."""
    t, probes = [0.0], []

    def last_keys(timeout=None):
        probes.append((t[0], timeout, held(lodge.root)))
        return keys(t, timeout)
    monkeypatch.setattr(sessions, "_runtime", lambda: types.SimpleNamespace(last_keys=last_keys))
    inbox_wake._quiet("pal-live", clock=lambda: t[0], now=lambda: 1000 + t[0],
                      sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert not any(locked for _, _, locked in probes)
    return t[0], probes


def test_quiet_returns_once_nobody_typed_for_two_seconds(lodge, monkeypatch):
    end, probes = quiet_run(lodge, monkeypatch, lambda t, _: {"pal-live": 999})  # typed 1 s before the pass
    assert end == pytest.approx(1.0) and len(probes) == 3


@pytest.mark.parametrize("keys", [
    lambda t, _: {"pal-live": 1000 + int(t[0])},  # typing all along: a key at 4 s does not extend the pass
    lambda t, _: {},  # no such session: unknown
    lambda t, _: 1 / 0,  # tmux did not answer: unknown
])
def test_typing_or_unknown_activity_waits_exactly_the_cap(lodge, monkeypatch, keys):
    end, probes = quiet_run(lodge, monkeypatch, keys)
    assert end == pytest.approx(inbox_wake.QUIET_CAP)
    assert all(timeout == pytest.approx(max(0.1, inbox_wake.QUIET_CAP - at)) for at, timeout, _ in probes)


def test_a_probe_that_hangs_spends_the_rest_of_the_cap_and_no_more(lodge, monkeypatch):
    def hang(t, timeout):
        t[0] += timeout
        raise subprocess.TimeoutExpired("tmux", timeout)
    end, probes = quiet_run(lodge, monkeypatch, hang)
    assert end == pytest.approx(inbox_wake.QUIET_CAP) and len(probes) == 1


# -- the guarded paste ---------------------------------------------------------------

def test_a_live_session_gets_one_pasted_hint_in_its_own_pane_under_both_locks(lodge, paste):
    row = live(lodge, paste)
    mail(lodge)
    mail(lodge, "k2", human=False)
    inbox_wake._wake(str(lodge.root), "pal")
    (pane, text, settle, locked, paste_locked), = paste.pastes
    assert (pane, settle, locked, paste_locked) == ("%1", 0.5, True, True)
    assert text.startswith("New inbox mail from your human, telegram.") and "hello" not in text
    assert paste.waits == [("ready", False), ("quiet", False)] and not lodge.calls.delivered
    assert outcomes(lodge) == [("hinted", None), ("hinted", None)]
    assert wake_state(lodge)["hinted"] == {"session": "pal-live", "attempt": row["attempt"]}
    assert rows(lodge.root, "SELECT state FROM inbox_messages") == [("queued",), ("queued",)]
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # a later pickup of the same attempt
    assert len(paste.pastes) == 1


@pytest.mark.parametrize("change", ["stop", "archive", "relaunch", "rest", "pane", "mode", "gone"])
def test_a_change_during_the_wait_is_never_pasted_into(lodge, paste, change):
    row = live(lodge, paste)
    mail(lodge)
    paste.during_quiet = {
        "stop": lambda: sessions.stop_session("pal-live"),
        "archive": lambda: sessions.archive_session("pal-live"),
        "relaunch": lambda: lodge.registry.update("pal-live", attempt="a-new"),
        "rest": lambda: lodge.registry.update("pal-live", status=sessions.STOPPING),
        "pane": lambda: lodge.registry.update("pal-live", runtime={**row["runtime"], "pane_id": "%9"}),
        "mode": lambda: lodge.registry.update("pal-live", inbox_adapter="native"),
        "gone": lambda: setattr(paste, "agent", "%7"),  # an agent, but not in the recorded pane
    }[change]
    inbox_wake._wake(str(lodge.root), "pal")
    assert not paste.pastes and not lodge.calls.delivered
    assert outcomes(lodge) == ([("blocked", "hint_unavailable")] if change == "gone" else [])
    assert rows(lodge.root, "SELECT state FROM inbox_messages") == [("queued",)]


def test_a_paste_that_fails_is_unknown_never_retried_and_custody_is_unchanged(lodge, paste):
    live(lodge, paste)
    mail(lodge)
    paste.error = subprocess.TimeoutExpired("tmux", 10)
    inbox_wake._wake(str(lodge.root), "pal")
    assert outcomes(lodge) == [("blocked", "hint_unknown")] and "hinted" not in wake_state(lodge)
    assert not lodge.calls.delivered and paste.schedules == [True]  # only the start's own pickup
    assert rows(lodge.root, "SELECT state FROM inbox_messages") == [("queued",)]


def test_not_ready_pastes_nothing_answers_nothing_and_a_later_wake_tries_again(lodge, paste):
    live(lodge, paste)
    mail(lodge)
    paste.ready = False
    inbox_wake._wake(str(lodge.root), "pal")
    assert not paste.pastes and outcomes(lodge) == [] and paste.waits == [("ready", False)]
    paste.ready = True
    inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert len(paste.pastes) == 1 and outcomes(lodge) == [("hinted", None)]


def test_mail_a_reader_took_during_the_wait_is_answered_and_nothing_is_pasted(lodge, paste):
    live(lodge, paste)
    mail(lodge)
    paste.during_quiet = lambda: lodge.store.fetch(lodge.store.grant("pal-live", "pal"))
    inbox_wake._wake(str(lodge.root), "pal")
    assert not paste.pastes and outcomes(lodge) == [("hinted", None)]


def test_a_native_or_unknown_mode_session_is_never_pasted_into(lodge, paste):
    config(lodge, native_hints=True)
    live(lodge, paste)
    mail(lodge)
    for result in (("submitted", "x"), ("pending", "not_ready"), ("unknown", "timeout"),
                   ("unavailable", "no_native"), ("failed", "OSError")):
        sys.modules["native_adapters"].deliver = lambda record, hint, r=result: lodge.calls.delivered.append(r) or r
        inbox_wake._wake(str(lodge.root), "pal")
    with lodge.registry.edit("pal-live", wolt="pal") as row:  # neither mode nor locator: unknown, never a paste
        row.pop("native"), row.pop("inbox_adapter")
    inbox_wake._wake(str(lodge.root), "pal")
    assert len(lodge.calls.delivered) == 6 and not paste.pastes and not paste.waits


def test_a_wake_that_starts_a_session_counts_its_fetch_instruction_as_the_hint(lodge, paste):
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    (started, _, _), = lodge.calls.spawned
    row = record(lodge, started)
    assert outcomes(lodge) == [("started", None)] and paste.schedules == [True]
    assert wake_state(lodge)["hinted"] == {"session": started, "attempt": row["attempt"]}
    paste.live = started
    inbox_wake._wake(str(lodge.root), "pal", once=True)  # the start's own pickup
    assert not paste.pastes
    mail(lodge, "k2")
    inbox_wake._wake(str(lodge.root), "pal")  # new mail still nudges
    assert len(paste.pastes) == 1


def test_a_resume_without_a_prompt_is_hinted_by_its_pickup_once(lodge, paste, monkeypatch):
    live(lodge, paste, "pal-rest")
    lodge.registry.update("pal-rest", status="resting", harness_session_id="conv-1")
    paste.live = None
    monkeypatch.setattr(sessions, "_tmux_alive", lambda data: False)
    monkeypatch.setattr(sessions, "_await_agent", lambda *a, **k: True)
    mail(lodge)
    inbox_wake._wake(str(lodge.root), "pal")
    assert lodge.calls.spawned[-1][0] == "pal-rest" and not paste.pastes
    row = record(lodge, "pal-rest")
    assert (row["status"], row["inbox_adapter"]) == ("running", "paste")
    paste.live = "pal-rest"
    for _ in range(2):
        inbox_wake._wake(str(lodge.root), "pal", once=True)
    assert len(paste.pastes) == 1 and wake_state(lodge)["hinted"]["attempt"] == row["attempt"]
