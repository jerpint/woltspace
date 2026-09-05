"""Lock contention, sleep/wake, upgrade, and missing mounts — with a connector.

Phase 3 proved these for a bare control plane. A supervised connector is a
second moving part: it must not be stolen by a rejected second instance, must
survive a suspended machine, and must come back after an upgrade without
disturbing the tmux sessions the registry already owns.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from session_runtime import RuntimeHandle, set_runtime  # noqa: E402
from sessions import SessionRegistry  # noqa: E402
from woltspace.adoption import adopt_runtime_sessions  # noqa: E402
from woltspace.channel_supervisor import ChannelSupervisor, read_connector_report  # noqa: E402
from woltspace.channels import ConnectorPlan  # noqa: E402
from woltspace.doctor import (  # noqa: E402
    MountError,
    container_mount_check,
    doctor_ok,
    ensure_container_mounts,
    run_doctor,
)
from woltspace.instance import DataRootLock, InstanceConflict, read_owner  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace.supervisor import Supervisor  # noqa: E402


def _layout(tmp_path, isolation="host", port=18811):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=ROOT,
        host="127.0.0.1",
        port=port,
        isolation=isolation,
    )


def _sleeper(seconds: int = 60):
    return (sys.executable, "-c", f"import time; time.sleep({seconds})")


def _plan(command=None):
    return ConnectorPlan("telegram", True, "resilience child", command or _sleeper(), str(ROOT), {})


def _alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


class TestInstanceLockWithAConnectorRunning:
    def test_second_control_plane_is_rejected_with_owner_information(self, tmp_path):
        layout = _layout(tmp_path)
        first = ChannelSupervisor(layout, [_plan()])
        with DataRootLock(layout, "instance-one"):
            first.start()
            try:
                connector_pid = read_connector_report(layout)["connectors"][0]["pid"]
                with pytest.raises(InstanceConflict) as caught:
                    DataRootLock(layout, "instance-two").acquire()
                message = str(caught.value)
                assert "instance-one" in message
                assert str(tmp_path) in message
                assert "woltspace status" in message

                # The rejected instance must not have disturbed the running one.
                assert read_owner(layout).instance_id == "instance-one"
                assert _alive(connector_pid)
                assert read_connector_report(layout)["connectors"][0]["pid"] == connector_pid
            finally:
                first.stop()

    def test_a_rejected_start_leaves_the_owner_metadata_intact(self, tmp_path):
        layout = _layout(tmp_path)
        with DataRootLock(layout, "instance-one"):
            with pytest.raises(InstanceConflict):
                DataRootLock(layout, "instance-two").acquire()
            assert read_owner(layout).instance_id == "instance-one"
        assert read_owner(layout) is None


class TestSleepAndWake:
    def test_a_suspended_machine_does_not_exhaust_the_restart_budget(self, tmp_path):
        """Crashes age out of the window, so waking up is not a permanent failure."""
        layout = _layout(tmp_path)
        clock = {"now": 1000.0}
        crasher = (sys.executable, "-c", "import sys; sys.exit(1)")
        supervisor = ChannelSupervisor(
            layout,
            [_plan(crasher)],
            max_restarts=2,
            restart_window=300.0,
            sleep=lambda _seconds: None,
            clock=lambda: clock["now"],
        )
        supervisor.start(watch=False)
        try:
            for _ in range(4):
                # One crash, then the machine sleeps for an hour before the next.
                deadline = time.monotonic() + 20
                restarts_before = supervisor.states["telegram"].restarts
                while time.monotonic() < deadline:
                    supervisor.poll_once()
                    if supervisor.states["telegram"].restarts > restarts_before:
                        break
                    time.sleep(0.02)
                clock["now"] += 3600.0
            state = supervisor.states["telegram"]
            assert state.state != "failed", state.error
            assert state.restarts >= 4
        finally:
            supervisor.stop()

    def test_a_child_killed_while_suspended_is_reported_then_restarted(self, tmp_path):
        import os

        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()], poll_interval=0.05)
        supervisor.start()
        try:
            before = read_connector_report(layout)["connectors"][0]["pid"]
            os.kill(before, 9)  # what a suspend/OOM looks like from here
            deadline = time.monotonic() + 20
            record = {}
            while time.monotonic() < deadline:
                record = read_connector_report(layout)["connectors"][0]
                if record["state"] == "running" and record["pid"] != before:
                    break
                time.sleep(0.05)
            assert record["state"] == "running", record
            assert record["last_exit_code"] == -9
            assert record["restarts"] == 1
        finally:
            supervisor.stop()

    def test_an_exhausted_connector_reports_honestly_instead_of_looping(self, tmp_path):
        layout = _layout(tmp_path)
        crasher = (sys.executable, "-c", "import sys; sys.exit(7)")
        plan = ConnectorPlan(
            "telegram", True, "always crashes", crasher, str(ROOT), {},
            remedy="Check the bot token in config.json.",
        )
        supervisor = ChannelSupervisor(
            layout, [plan], max_restarts=1, sleep=lambda _seconds: None
        )
        supervisor.start(watch=False)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                supervisor.poll_once()
                if supervisor.states["telegram"].state == "failed":
                    break
                time.sleep(0.02)
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "failed"
            assert record["last_exit_code"] == 7
            assert "not restarting" in record["error"]
            assert record["remedy"] == "Check the bot token in config.json."

            from woltspace.cli import format_connector_lines

            lines = format_connector_lines({"health": {"connectors": [record]}})
            assert any("failed" in line for line in lines)
            assert any("Check the bot token" in line for line in lines)
        finally:
            supervisor.stop()


class AdoptionRuntime:
    """Minimal runtime seam: tmux is alive and untouched across the upgrade."""

    def __init__(self, alive):
        self.alive = set(alive)
        self.killed = []

    def is_alive(self, handle):
        return handle.tmux_session_name in self.alive

    def resolve_process_handle(self, handle, process_names):
        return handle.at_pane("%0")

    def paste(self, handle, text, settle=0.0, **kwargs):
        pass

    def kill(self, handle):
        self.killed.append(handle.tmux_session_name)


class TestUpgradePath:
    def test_stop_then_start_readopts_sessions_and_the_connector(self, tmp_path):
        layout = _layout(tmp_path)
        registry = SessionRegistry(layout.wolts_dir)
        registry.create("upgrade-session", wolt="testwolt", harness="claude")
        registry.update(
            "upgrade-session", wolt="testwolt",
            runtime=RuntimeHandle("upgrade-session", "upgrade-tmux", "%1").to_record(),
        )
        runtime = AdoptionRuntime(alive={"upgrade-tmux"})
        set_runtime(runtime)

        old = ChannelSupervisor(layout, [_plan()])
        with DataRootLock(layout, "old-version"):
            adopt_runtime_sessions(layout)
            old.start()
            old_pid = read_connector_report(layout)["connectors"][0]["pid"]
            old.stop()

        # The old control plane is gone; its connector is gone with it.
        assert not _alive(old_pid)
        assert read_owner(layout) is None
        assert read_connector_report(layout)["connectors"][0]["state"] == "stopped"

        new = ChannelSupervisor(layout, [_plan()])
        with DataRootLock(layout, "new-version"):
            report = adopt_runtime_sessions(layout)
            new.start()
            try:
                new_pid = read_connector_report(layout)["connectors"][0]["pid"]
                assert new_pid and new_pid != old_pid
                assert read_connector_report(layout)["connectors"][0]["state"] == "running"
            finally:
                new.stop()

        # Stopping a control plane never touches tmux — the session survives.
        assert runtime.killed == []
        assert "upgrade-session" in json.dumps(report)
        record = registry.get("upgrade-session", wolt="testwolt", check_alive=False)
        assert record["runtime"]["tmux_session_name"] == "upgrade-tmux"


class TestMissingContainerMounts:
    def test_an_absent_mount_is_named_not_a_traceback(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        check = container_mount_check(layout)
        assert check.status == "fail"
        assert "not mounted" in check.detail
        assert "docker run -v" in check.remedy
        with pytest.raises(MountError) as caught:
            ensure_container_mounts(layout)
        assert "docker run -v" in str(caught.value)

    def test_doctor_reports_the_missing_mount_in_container_mode(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        checks = run_doctor(layout, check_port=False)
        mounts = [check for check in checks if check.name == "mounts"]
        assert mounts and mounts[0].status == "fail"
        assert not doctor_ok(checks)

    def test_native_mode_has_no_mount_check_and_may_create_its_data_root(self, tmp_path):
        layout = _layout(tmp_path, isolation="host")
        checks = run_doctor(layout, check_port=False)
        assert [check for check in checks if check.name == "mounts"] == []
        ensure_container_mounts(layout)  # no-op natively

    def test_a_present_mount_passes(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        assert container_mount_check(layout).status == "pass"
        ensure_container_mounts(layout)

    def test_serve_refuses_to_boot_without_the_mount(self, tmp_path):
        """prepare() is the fail-fast point: no data root is invented."""
        layout = _layout(tmp_path, isolation="external")
        with pytest.raises(MountError) as caught:
            Supervisor(layout).prepare()
        assert "is not mounted" in str(caught.value)
        assert "docker run -v" in str(caught.value)
        assert not layout.wolts_dir.exists()

    def test_serve_surfaces_the_mount_error_as_a_message(self):
        """`woltspace serve` catches MountError rather than dumping a traceback."""
        source = (ROOT / "src" / "woltspace" / "cli.py").read_text()
        assert "except (InstanceConflict, MountError)" in source
        assert 'print(f"serve failed: {exc}")' in source
