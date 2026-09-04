"""Registry-led adoption across control-plane restarts."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from session_runtime import RuntimeHandle, set_runtime
from sessions import SessionRegistry, deliver_message
from woltspace.adoption import adopt_runtime_sessions, read_adoption_report
from woltspace.layout import RuntimeLayout, installation_root


class AdoptionRuntime:
    def __init__(self, alive, resolved):
        self.alive = set(alive)
        self.resolved = dict(resolved)
        self.checked = []
        self.pastes = []

    def is_alive(self, handle):
        self.checked.append(handle.tmux_session_name)
        return handle.tmux_session_name in self.alive

    def resolve_process_handle(self, handle, process_names):
        pane = self.resolved.get(handle.tmux_session_name)
        return handle.at_pane(pane) if pane else None

    def paste(self, handle, text, settle=0.0, **kwargs):
        self.pastes.append((handle, text, settle))


def _layout(tmp_path):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=installation_root(),
        host="127.0.0.1",
        port=18779,
        isolation="host",
    )


def test_adoption_starts_from_registry_and_preserves_session_identity(tmp_path):
    layout = _layout(tmp_path)
    reg = SessionRegistry(layout.wolts_dir)
    live = reg.create(
        "live-session", wolt="testwolt", harness="claude",
        adapter="telegram", chat_id="123",
    )
    reg.update(
        "live-session", wolt="testwolt",
        runtime=RuntimeHandle("live-session", "persisted-tmux", "%old").to_record(),
    )
    reg.create("revived-session", wolt="testwolt", harness="claude")
    reg.update(
        "revived-session", wolt="testwolt", status="orphaned",
        runtime=RuntimeHandle("revived-session", "revived-tmux", "%2").to_record(),
    )
    reg.create("dead-session", wolt="testwolt", harness="claude")
    reg.create("completed-session", wolt="testwolt", harness="claude")
    reg.update("completed-session", wolt="testwolt", status="completed")
    before_activity = reg.get("live-session", wolt="testwolt", check_alive=False)["last_activity"]

    runtime = AdoptionRuntime(
        alive={"persisted-tmux", "revived-tmux", "unmanaged-tmux"},
        resolved={"persisted-tmux": "%fresh", "revived-tmux": "%2"},
    )
    set_runtime(runtime)
    try:
        report = adopt_runtime_sessions(layout)
        stored_after_adoption = reg.get(
            "live-session", wolt="testwolt", check_alive=False
        )
        delivered = deliver_message(
            "live-session", "after restart", registry=reg,
        )
    finally:
        set_runtime(None)

    assert [item["session"] for item in report["adopted"]] == [
        "live-session", "revived-session",
    ]
    assert [item["session"] for item in report["orphaned"]] == ["dead-session"]
    assert [item["session"] for item in report["unchanged"]] == ["completed-session"]
    assert set(runtime.checked) == {"persisted-tmux", "revived-tmux", "dead-session"}
    assert "unmanaged-tmux" not in runtime.checked

    stored = stored_after_adoption
    assert stored["status"] == "running"
    assert stored["runtime"]["tmux_session_name"] == "persisted-tmux"
    assert stored["runtime"]["pane_id"] == "%fresh"
    assert stored["routing"] == live["routing"]
    assert stored["target"] == live["target"]
    assert stored["last_activity"] == before_activity
    assert reg.get("revived-session", wolt="testwolt", check_alive=False)["status"] == "running"
    assert reg.get("dead-session", wolt="testwolt", check_alive=False)["status"] == "orphaned"

    assert delivered["status"] == "delivered"
    pasted_handle, pasted_text, _settle = runtime.pastes[-1]
    assert pasted_handle.tmux_session_name == "persisted-tmux"
    assert pasted_handle.pane_id == "%fresh"
    assert pasted_text == "after restart"

    persisted = read_adoption_report(layout)
    assert persisted == report
    assert (layout.platform_state / "adoption.json").stat().st_mode & 0o777 == 0o600


def test_adoption_report_is_valid_json(tmp_path):
    layout = _layout(tmp_path)
    runtime = AdoptionRuntime(alive=set(), resolved={})
    set_runtime(runtime)
    try:
        report = adopt_runtime_sessions(layout)
    finally:
        set_runtime(None)
    assert json.loads((layout.platform_state / "adoption.json").read_text()) == report
