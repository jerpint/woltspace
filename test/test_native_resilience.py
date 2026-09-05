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


class TestTelegramTokenClash:
    """One bot token, two pollers: Telegram answers 409 and our bot goes deaf."""

    def _degraded(self, tmp_path, log_line):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor.start(watch=False)
        try:
            log = Path(supervisor.states["telegram"].log)
            log.write_text(log_line)
            supervisor.states["telegram"].log_offset = 0
            supervisor.poll_once()
            return supervisor, read_connector_report(layout)["connectors"][0]
        finally:
            supervisor.stop()

    def test_a_clash_is_reported_in_telegram_terms_not_raw_http(self, tmp_path):
        _supervisor, record = self._degraded(
            tmp_path,
            "telegram.error.Conflict: Conflict: terminated by other getUpdates request\n",
        )
        assert record["state"] == "degraded"
        assert "already polling this bot token" in record["error"]
        assert "409" in record["error"]
        assert "own test bot token" in record["remedy"]

    def test_a_live_but_deaf_connector_is_not_reported_healthy(self, tmp_path):
        supervisor, record = self._degraded(
            tmp_path, '{"ok":false,"error_code":409,"description":"Conflict"}\n'
        )
        assert record["state"] == "degraded"
        assert record["pid"], "the child is still alive — liveness alone would pass"

    def test_a_clean_log_leaves_the_connector_running(self, tmp_path):
        _supervisor, record = self._degraded(
            tmp_path, "telegram v2 bot starting (chat-per-wolt model)...\n"
        )
        assert record["state"] == "running"
        assert record["error"] is None

    def test_a_connector_that_died_of_a_clash_is_not_restarted(self, tmp_path):
        layout = _layout(tmp_path)
        crasher = (sys.executable, "-c", "import sys; sys.exit(1)")
        supervisor = ChannelSupervisor(
            layout, [_plan(crasher)], max_restarts=5, sleep=lambda _s: None
        )
        supervisor.start(watch=False)
        try:
            Path(supervisor.states["telegram"].log).write_text(
                "Conflict: terminated by other getUpdates request\n"
            )
            supervisor.states["telegram"].log_offset = 0
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                supervisor.poll_once()
                if supervisor.states["telegram"].state == "failed":
                    break
                time.sleep(0.02)
            state = supervisor.states["telegram"]
            assert state.state == "failed"
            assert state.restarts == 0, "restarting cannot win the token race"
            assert "already polling this bot token" in state.error
        finally:
            supervisor.stop()


class TestSharedDataRootWarning:
    def test_a_container_owned_data_root_warns_a_native_run(self, tmp_path):
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import InstanceOwner, write_owner

        layout = _layout(tmp_path)
        write_owner(layout, InstanceOwner(
            instance_id="container-instance", pid=1, started_at=0,
            endpoint="http://127.0.0.1:7777", isolation="external",
            hostname="woltspace-container",
        ))
        check = shared_data_root_check(layout)
        assert check is not None
        assert check.status == "warn"
        assert "cannot be trusted across a Docker bind mount" in check.detail
        assert "fresh data root" in check.remedy
        # A warning, never a hard failure — the operator decides.
        assert check.ok

    def test_an_unclaimed_data_root_says_nothing(self, tmp_path):
        from woltspace.doctor import shared_data_root_check

        assert shared_data_root_check(_layout(tmp_path)) is None

    def test_container_mode_does_not_warn_about_itself(self, tmp_path):
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import InstanceOwner, write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        write_owner(layout, InstanceOwner(
            instance_id="container-instance", pid=1, started_at=0,
            endpoint="http://127.0.0.1:7777", isolation="external", hostname="elsewhere",
        ))
        assert shared_data_root_check(layout) is None


class TestPrePublishTuiRemedy:
    """@woltspace/tui is not on the registry yet, so npx cannot resolve it."""

    def test_the_npx_fallback_names_the_from_checkout_recipe(self):
        from woltspace.tui import TuiResolution, fallback_notices, local_tarball_recipe

        resolution = TuiResolution(
            "npx", ("npx", "--yes", "--package=@woltspace/tui@0.2.2", "woltspace-tui"),
            {"path": "/usr/local/bin/woltspace-tui", "valid": False,
             "error": "expected @woltspace/tui@0.2.2, got @woltspace/tui@0.1.0"},
        )
        notices = fallback_notices(resolution)
        assert any("ignoring /usr/local/bin/woltspace-tui" in line for line in notices)
        assert any("not published yet" in line for line in notices)
        assert any(local_tarball_recipe() in line for line in notices)
        assert "npm pack" in local_tarball_recipe()
        assert "woltspace-tui-0.2.2.tgz" in local_tarball_recipe()

    def test_an_exact_local_binary_prints_nothing(self):
        from woltspace.tui import TuiResolution, fallback_notices

        assert fallback_notices(TuiResolution("local", ("/usr/local/bin/woltspace-tui",))) == []

    def test_missing_npx_also_names_the_recipe(self):
        from woltspace.tui import TuiResolutionError, resolve_tui

        def which(name):
            return "/usr/bin/node" if name == "node" else None

        def runner(command, **kwargs):
            return subprocess.CompletedProcess(command, 0, "v20.11.0", "")

        with pytest.raises(TuiResolutionError) as caught:
            resolve_tui({}, which=which, runner=runner)
        assert "npm pack" in str(caught.value)


class TestDocsStayTrue:
    """The doc makes promises the code has to keep."""

    DOC = ROOT / "docs" / "native-and-container.md"

    def test_the_release_checklist_names_every_pinned_file(self):
        text = self.DOC.read_text()
        for path in ("tui/package.json", "tui/src/version.js",
                     "src/woltspace/compatibility.py"):
            assert path in text, path
            assert (ROOT / path).is_file(), path

    def test_the_pre_publish_recipe_matches_the_one_the_cli_prints(self):
        from woltspace.compatibility import TUI_VERSION
        from woltspace.tui import local_tarball_recipe

        text = self.DOC.read_text()
        assert f"woltspace-tui-{TUI_VERSION}.tgz" in text
        assert f"woltspace-tui-{TUI_VERSION}.tgz" in local_tarball_recipe()

    def test_the_config_example_is_the_shape_the_connector_reads(self, tmp_path):
        import re

        from woltspace.channels import TelegramConnector

        text = self.DOC.read_text()
        block = re.search(r'```json\n(\{.*?\})\n```', text, re.S)
        assert block, "the doc must show a config.json example"
        layout = _layout(tmp_path)
        layout.platform_state.mkdir(parents=True)
        (layout.platform_state / "config.json").write_text(block.group(1))
        plan = TelegramConnector().plan(layout, {})
        assert plan.enabled is True
        assert plan.env["TELEGRAM_BOT_TOKEN"] == "123456:your-bot-token"
        assert plan.env["TELEGRAM_ALLOWED_USERS"] == "11111111"

    def test_the_documented_status_output_is_what_status_renders(self):
        from woltspace.channel_supervisor import TOKEN_CLASH_ERROR, TOKEN_CLASH_REMEDY

        text = self.DOC.read_text()
        assert TOKEN_CLASH_ERROR in text
        assert TOKEN_CLASH_REMEDY.split(".")[0] in text

    def test_it_is_linked_from_the_readme(self):
        assert "docs/native-and-container.md" in (ROOT / "README.md").read_text()
