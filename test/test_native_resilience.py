"""Lock contention, sleep/wake, upgrade, and missing mounts — with a connector.

Phase 3 proved these for a bare control plane. A supervised connector is a
second moving part: it must not be stolen by a rejected second instance, must
survive a suspended machine, and must come back after an upgrade without
disturbing the tmux sessions the registry already owns.
"""

import json
import os
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


# A string that appears in the stand-in child's argv and nowhere else, so the
# reaper's marker matching is exercised the way the real adapter module is.
PROBE_MARKER = "woltspace-test-connector"
# An adjacent token pair in the stand-in child's argv, standing in for the real
# `-m <module>` pair. A pair is the point: no filename can forge one.
PROBE_SIGNATURE = ("#", PROBE_MARKER)


def _sleeper(seconds: int = 60):
    return (
        sys.executable,
        "-c",
        f"import time; time.sleep({seconds})  # {PROBE_MARKER}",
    )


def _plan(command=None, signature=PROBE_SIGNATURE):
    return ConnectorPlan(
        "telegram", True, "resilience child", command or _sleeper(), str(ROOT), {},
        process_signature=signature,
    )


def _alive(pid: int) -> bool:
    """Running, and not merely a zombie waiting to be collected."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        status = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0]
    except (OSError, IndexError):
        return True
    return status != "Z"


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
    def test_stop_then_start_readopts_sessions_and_the_connector(self, tmp_path, request):
        layout = _layout(tmp_path)
        registry = SessionRegistry(layout.wolts_dir)
        registry.create("upgrade-session", wolt="testwolt", harness="claude")
        registry.update(
            "upgrade-session", wolt="testwolt",
            runtime=RuntimeHandle("upgrade-session", "upgrade-tmux", "%1").to_record(),
        )
        runtime = AdoptionRuntime(alive={"upgrade-tmux"})
        set_runtime(runtime)
        # A process-wide fake left installed makes every later in-process test
        # see a runtime that reports all sessions dead and swallows every kill.
        request.addfinalizer(lambda: set_runtime(None))

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
        assert "except (InstanceConflict, MountError, DataRootConflict)" in source
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

    def test_a_clash_from_a_previous_life_does_not_degrade_a_fresh_connector(self, tmp_path):
        """The log is appended across restarts and across the container/native
        boundary. Pointing native at the container's data root, the very first
        tick read weeks-old 409s and reported a healthy bot as degraded."""
        layout = _layout(tmp_path)
        layout.logs_dir.mkdir(parents=True, exist_ok=True)
        stale = layout.logs_dir / "connector-telegram.log"
        stale.write_text(
            "telegram.error.Conflict: Conflict: terminated by other getUpdates request\n" * 50
        )
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor.start(watch=False)
        try:
            supervisor.poll_once()
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "running"
            assert record["error"] is None
            # ...but a clash logged by *this* life is still caught.
            with stale.open("a") as handle:
                handle.write('{"ok":false,"error_code":409,"description":"Conflict"}\n')
            supervisor.poll_once()
            assert read_connector_report(layout)["connectors"][0]["state"] == "degraded"
        finally:
            supervisor.stop()

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


def fake_ps(mapping: dict):
    """An injectable `ps` answering both `-o command=` and `-o comm=`.

    `comm` is derived from the first token, the way a real ps would: the
    executable, not the argument vector.
    """

    def runner(argv, **kwargs):
        pid = int(argv[-1])
        command = mapping.get(pid, "")
        if "comm=" in argv:
            command = command.split()[0] if command else ""
        return subprocess.CompletedProcess(argv, 0, command, "")

    return runner


def own_owner(layout, **overrides):
    from woltspace.instance import InstanceOwner

    values = {
        "instance_id": "incumbent",
        "pid": 4711,
        "started_at": 0,
        "endpoint": "http://127.0.0.1:7777",
        "isolation": "external",
        "hostname": "some-container",
    }
    values.update(overrides)
    return InstanceOwner(**values)


class TestSharedDataRootWarning:
    def test_a_live_container_control_plane_stops_a_native_run(self, tmp_path):
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import write_owner

        layout = _layout(tmp_path)
        write_owner(layout, own_owner(layout, pid=os.getpid()))
        check = shared_data_root_check(
            layout, runner=fake_ps({os.getpid(): "python -m woltspace serve"})
        )
        assert check is not None
        assert check.status == "warn"  # native guest: informative, not fatal
        assert "owned by a live control plane" in check.detail
        assert "fresh data root" in check.remedy
        assert check.ok

    def test_a_dead_owner_record_is_stale_not_a_conflict(self, tmp_path):
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import write_owner

        layout = _layout(tmp_path)
        write_owner(layout, own_owner(layout, pid=999999))
        assert shared_data_root_check(layout, runner=fake_ps({})) is None

    def test_a_recycled_pid_running_something_else_is_stale(self, tmp_path):
        """A container reboot recycles pids; the number alone proves nothing."""
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import write_owner

        layout = _layout(tmp_path)
        write_owner(layout, own_owner(layout, pid=os.getpid()))
        check = shared_data_root_check(
            layout, runner=fake_ps({os.getpid(): "/usr/sbin/sshd -D"})
        )
        assert check is None

    def test_an_unclaimed_data_root_says_nothing(self, tmp_path):
        from woltspace.doctor import shared_data_root_check

        assert shared_data_root_check(_layout(tmp_path)) is None


class TestEntrypointsAreNotExempt:
    """R1: being the entrypoint means being deliberate, not being alone.

    A second `woltspace start` on another port is exactly as deliberate as the
    first, and across a Docker bind mount the flock cannot tell them apart.
    What `as_entrypoint` changes is which evidence counts — not whether any
    evidence is looked at.
    """

    def test_a_second_entrypoint_start_on_a_live_root_refuses(self, tmp_path):
        from woltspace.doctor import DataRootConflict, ensure_data_root_available
        from woltspace.instance import write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True, exist_ok=True)
        write_owner(layout, own_owner(layout, pid=os.getpid()))
        runner = fake_ps({os.getpid(): "python -m woltspace serve --port 7777"})

        with pytest.raises(DataRootConflict) as caught:
            ensure_data_root_available(layout, as_entrypoint=True, runner=runner)
        assert "owned by a live control plane" in str(caught.value)
        assert "WOLTSPACE_ALLOW_SHARED_DATA_ROOT" in str(caught.value)

    def test_a_live_foreign_tunnel_also_refuses_an_entrypoint(self, tmp_path):
        from woltspace.doctor import DataRootConflict, ensure_data_root_available

        layout = _layout(tmp_path, isolation="external")
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "pid": os.getpid(), "url": "https://incumbent.example",
            "instance_id": "somebody-else",
        }))
        runner = fake_ps({os.getpid(): "cloudflared tunnel run"})
        with pytest.raises(DataRootConflict):
            ensure_data_root_available(layout, as_entrypoint=True, runner=runner)

    def test_restarting_over_its_own_stale_state_proceeds(self, tmp_path):
        """The ordinary container reboot: dead owner, dead tunnel, live records."""
        from woltspace.doctor import ensure_data_root_available, shared_data_root_check
        from woltspace.instance import write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        write_owner(layout, own_owner(layout, pid=999999, hostname="old-container"))
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "pid": 999998, "url": "https://old.example", "instance_id": "previous-boot",
        }))
        sessions = layout.wolts_dir / "realwolt" / ".state" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s.json").write_text(json.dumps({"status": "running"}))

        runner = fake_ps({})  # nothing from the old boot survived
        assert shared_data_root_check(layout, as_entrypoint=True, runner=runner) is None
        ensure_data_root_available(layout, as_entrypoint=True, runner=runner)

    def test_running_sessions_alone_never_stop_an_entrypoint(self, tmp_path):
        """They are what it exists to re-adopt."""
        from woltspace.doctor import shared_data_root_check

        layout = _layout(tmp_path, isolation="external")
        sessions = layout.wolts_dir / "realwolt" / ".state" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s.json").write_text(json.dumps({"status": "running"}))

        assert shared_data_root_check(layout, as_entrypoint=True) is None
        # …but they still stop a guest.
        guest = shared_data_root_check(layout)
        assert guest is not None and guest.status == "fail"

    def test_its_own_instance_id_is_never_a_conflict(self, tmp_path, monkeypatch):
        from woltspace.doctor import shared_data_root_check
        from woltspace.instance import write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("WOLTSPACE_INSTANCE_ID", "me")
        write_owner(layout, own_owner(layout, instance_id="me", pid=os.getpid()))
        runner = fake_ps({os.getpid(): "python -m woltspace serve"})
        assert shared_data_root_check(layout, as_entrypoint=True, runner=runner) is None

    def test_the_override_still_releases_an_entrypoint(self, tmp_path, monkeypatch):
        from woltspace.doctor import ensure_data_root_available
        from woltspace.instance import write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True, exist_ok=True)
        write_owner(layout, own_owner(layout, pid=os.getpid()))
        monkeypatch.setenv("WOLTSPACE_ALLOW_SHARED_DATA_ROOT", "1")
        ensure_data_root_available(
            layout, as_entrypoint=True,
            runner=fake_ps({os.getpid(): "python -m woltspace serve"}),
        )


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


class TestAStrayServeCannotTakeOverALiveDataRoot:
    """F0: `woltspace serve` typed in a worktree inside the running container.

    Every ingredient is ambient — WOLTSPACE_ISOLATION=external, the production
    bot token, WOLTSPACE_PUBLIC_TUNNEL=true — and the instance lock cannot
    defend, because a control plane old enough not to take one leaves no owner
    record. This happened for real; these tests are the fence.
    """

    CONTAINER_ENV = {
        "WOLTSPACE_ISOLATION": "external",
        "WOLTSPACE_PUBLIC_TUNNEL": "true",
        "ENABLE_TELEGRAM_BOT": "true",
        "TELEGRAM_BOT_TOKEN": "999999:production-token",
        "TELEGRAM_BOT_DIR": "/workspace/woltspace/container",
        "DEV_MODE": "true",
    }

    @pytest.fixture
    def occupied_root(self, tmp_path):
        """A data root a live control plane is already using, owner-record-free."""
        layout = _layout(tmp_path, isolation="external")
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        # The incumbent's evidence: a cloudflared this process can see is alive.
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "pid": os.getpid(), "url": "https://incumbent.example", "type": "named",
        }))
        sessions = layout.wolts_dir / "realwolt" / ".state" / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "realwolt-live.json").write_text(json.dumps({
            "name": "realwolt-live", "wolt": "realwolt", "status": "running",
        }))
        return layout

    # prepare() writes these directly; snapshot so a refused-then-allowed run
    # cannot repoint WOLTS_DIR for every test after it.
    RUNTIME_KEYS = (
        "WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
        "WOLTSPACE_HOST", "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL",
        "WOLTSPACE_ENTRYPOINT", "PORT",
    )

    @pytest.fixture(autouse=True)
    def container_ambient_env(self, monkeypatch):
        snapshot = {key: os.environ.get(key) for key in self.RUNTIME_KEYS}
        for key, value in self.CONTAINER_ENV.items():
            monkeypatch.setenv(key, value)
        monkeypatch.delenv("WOLTSPACE_ENTRYPOINT", raising=False)
        monkeypatch.delenv("WOLTSPACE_ALLOW_SHARED_DATA_ROOT", raising=False)
        try:
            yield
        finally:
            for key, value in snapshot.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_prepare_refuses_and_names_the_conflict(self, occupied_root):
        from woltspace.doctor import DataRootConflict

        with pytest.raises(DataRootConflict) as caught:
            Supervisor(occupied_root).prepare()
        message = str(caught.value)
        assert "already using it" in message or "claimed by" in message
        assert "fresh data root" in message

    def test_it_writes_nothing_into_the_data_root_it_was_refused(self, occupied_root):
        from woltspace.doctor import DataRootConflict

        before = sorted(p.name for p in occupied_root.platform_state.iterdir())
        with pytest.raises(DataRootConflict):
            Supervisor(occupied_root).prepare()
        after = sorted(p.name for p in occupied_root.platform_state.iterdir())
        assert after == before
        assert not (occupied_root.platform_state / "control-plane.json").exists()
        assert not (occupied_root.platform_state / "adoption.json").exists()
        assert not (occupied_root.platform_state / "connectors.json").exists()

    def test_the_incumbent_tunnel_state_survives(self, occupied_root):
        from woltspace.doctor import DataRootConflict

        state = (occupied_root.platform_state / "tunnel.json").read_text()
        with pytest.raises(DataRootConflict):
            Supervisor(occupied_root).prepare()
        assert (occupied_root.platform_state / "tunnel.json").read_text() == state

    def test_an_owner_record_alone_is_enough_to_refuse(self, tmp_path):
        from woltspace.doctor import DataRootConflict, ensure_data_root_available
        from woltspace.instance import write_owner

        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        write_owner(layout, own_owner(layout, pid=os.getpid()))
        with pytest.raises(DataRootConflict):
            ensure_data_root_available(
                layout, runner=fake_ps({os.getpid(): "python -m woltspace serve"})
            )

    def test_an_empty_data_root_is_still_fine(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        Supervisor(layout).prepare()  # no conflict, no exception
        assert layout.platform_state.is_dir()

    def test_an_explicit_override_still_lets_you_through(self, occupied_root, monkeypatch):
        monkeypatch.setenv("WOLTSPACE_ALLOW_SHARED_DATA_ROOT", "1")
        Supervisor(occupied_root).prepare()

    def test_a_guest_never_publishes_even_with_the_tunnel_enabled(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        Supervisor(layout).prepare()
        assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "false"

    def test_no_connector_is_planned_from_inherited_environment(self, tmp_path):
        from woltspace.channels import plan_connectors

        layout = _layout(tmp_path, isolation="external")
        plans = {plan.name: plan for plan in plan_connectors(layout)}
        assert [plan.enabled for plan in plans.values()] == [False, False]
        assert "ambient environment" in plans["telegram"].detail
        assert plans["telegram"].command == ()
        # The pty bridge is a guest here too: the real instance owns that port.
        assert "not the platform entrypoint" in plans["tui"].detail
        assert plans["tui"].command == ()

    def test_the_supervisor_plans_no_child_for_a_guest(self, tmp_path):
        layout = _layout(tmp_path, isolation="external")
        layout.wolts_dir.mkdir(parents=True)
        supervisor = Supervisor(layout)
        supervisor.prepare()
        channels = supervisor.channel_supervisor()
        assert all(not state.plan.enabled for state in channels.states.values())
        # And the resolved secret is not silently exported for something else.
        assert channels.states["telegram"].plan.env == {}


class TestTokenPreflight:
    """Never spawn a poller onto a token someone else already holds."""

    @pytest.fixture(autouse=True)
    def restore_runtime_env(self):
        keys = TestAStrayServeCannotTakeOverALiveDataRoot.RUNTIME_KEYS
        snapshot = {key: os.environ.get(key) for key in keys}
        try:
            yield
        finally:
            for key, value in snapshot.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_a_busy_token_is_refused_before_any_child_is_spawned(self, tmp_path, monkeypatch):
        from woltspace.channels import TOKEN_BUSY_DETAIL

        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        monkeypatch.setenv("ENABLE_TELEGRAM_BOT", "true")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:busy")
        layout = _layout(tmp_path)
        supervisor = Supervisor(layout, probe_token=lambda token: True)
        channels = supervisor.channel_supervisor()
        state = channels.states["telegram"]
        assert state.plan.enabled is False
        assert state.plan.detail == TOKEN_BUSY_DETAIL
        assert "own bot token" in state.plan.remedy

    def test_a_free_token_is_allowed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        monkeypatch.setenv("ENABLE_TELEGRAM_BOT", "true")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:free")
        layout = _layout(tmp_path)
        supervisor = Supervisor(layout, probe_token=lambda token: False)
        assert supervisor.channel_supervisor().states["telegram"].plan.enabled is True

    def test_a_409_means_busy_and_anything_else_means_proceed(self):
        import urllib.error

        from woltspace.channels import telegram_token_is_busy

        def conflict(url, timeout=0):
            raise urllib.error.HTTPError(url, 409, "Conflict", {}, None)

        def unreachable(url, timeout=0):
            raise OSError("network is down")

        assert telegram_token_is_busy("t", opener=conflict) is True
        # Not knowing must never block a legitimate start.
        assert telegram_token_is_busy("t", opener=unreachable) is False
        assert telegram_token_is_busy("") is False


class TestOrphanedConnectorsAreReaped:
    """A kill -9 leaves the connector holding the token; nothing tracked it."""

    def test_start_kills_a_previous_run_that_still_holds_the_token(self, tmp_path):
        layout = _layout(tmp_path)
        first = ChannelSupervisor(layout, [_plan()])
        first.start(watch=False)
        orphan = read_connector_report(layout)["connectors"][0]["pid"]
        # Simulate the control plane dying without ever calling stop().
        first._children.clear()
        first._thread = None
        assert _alive(orphan)

        second = ChannelSupervisor(layout, [_plan()])
        try:
            reaped = second.reap_orphans()
            assert orphan in reaped
            for _ in range(40):
                if not _alive(orphan):
                    break
                time.sleep(0.05)
            assert not _alive(orphan)
        finally:
            second.stop()

    def test_it_never_kills_a_pid_that_is_not_the_connector(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        # A stale record whose pid was recycled by something unrelated.
        (layout.platform_state / "connectors.json").write_text(json.dumps({
            "connectors": [{
                "name": "telegram", "pid": os.getpid(),
                "command": [sys.executable, "-m", "bot.telegram_adapter"],
            }],
        }))
        assert supervisor.reap_orphans() == []

    def test_a_dead_record_is_ignored(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "connectors.json").write_text(json.dumps({
            "connectors": [{"name": "telegram", "pid": 999999, "command": ["x"]}],
        }))
        assert supervisor.reap_orphans() == []


class TestReapBeforePreflight:
    """R2: the orphan holds the token, so asking Telegram first always loses.

    Preflight-then-reap produced the worst outcome available: 409 → connector
    disabled → *then* the orphan is killed, leaving the channel down with
    nothing running and nothing holding the token either.
    """

    @pytest.fixture(autouse=True)
    def entrypoint_env(self, monkeypatch, tmp_path):
        keys = TestAStrayServeCannotTakeOverALiveDataRoot.RUNTIME_KEYS
        snapshot = {key: os.environ.get(key) for key in keys}
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        monkeypatch.setenv("ENABLE_TELEGRAM_BOT", "true")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:token")
        monkeypatch.setenv("TELEGRAM_BOT_DIR", str(ROOT / "container"))
        try:
            yield
        finally:
            for key, value in snapshot.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_the_orphan_is_gone_before_the_token_is_probed(self, tmp_path):
        layout = _layout(tmp_path)
        # An orphan from a previous control plane, still holding the token.
        previous = ChannelSupervisor(layout, [_plan()])
        previous.start(watch=False)
        orphan = read_connector_report(layout)["connectors"][0]["pid"]
        previous._children.clear()
        assert _alive(orphan)

        order = []

        def probe(_token):
            # The token is only "busy" while the orphan is alive.
            order.append(("probe", _alive(orphan)))
            return _alive(orphan)

        supervisor = Supervisor(layout, probe_token=probe)
        channels = supervisor.channel_supervisor()
        try:
            assert order == [("probe", False)], (
                "the orphan must be reaped before the token is probed"
            )
            assert not _alive(orphan)
            plan = channels.states["telegram"].plan
            assert plan.enabled is True, "the channel must not be left disabled"
        finally:
            channels.stop()

    def test_a_genuinely_busy_token_still_refuses(self, tmp_path):
        from woltspace.channels import TOKEN_BUSY_DETAIL

        layout = _layout(tmp_path)
        supervisor = Supervisor(layout, probe_token=lambda _token: True)
        channels = supervisor.channel_supervisor()
        assert channels.states["telegram"].plan.enabled is False
        assert channels.states["telegram"].plan.detail == TOKEN_BUSY_DETAIL

    def test_the_reaper_is_not_run_twice(self, tmp_path):
        """channel_supervisor() already reaped; start() must not do it again."""
        layout = _layout(tmp_path)
        supervisor = Supervisor(layout, probe_token=lambda _token: False)
        channels = supervisor.channel_supervisor()
        calls = []
        channels.reap_orphans = lambda: calls.append(1) or []
        try:
            channels.start(watch=False)
            assert calls == []
        finally:
            channels.stop()


class TestPidValidationIsPortable:
    """R2: /proc does not exist on macOS, which is where this ships."""

    def test_it_asks_ps_rather_than_reading_proc(self):
        """The docstring may mention /proc; the code must not read it."""
        import ast

        path = ROOT / "src" / "woltspace" / "processes.py"
        tree = ast.parse(path.read_text())
        literals = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
        docstrings = {
            ast.get_docstring(node, clean=False)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        code_strings = [text for text in literals if text not in docstrings]
        assert not any("/proc/" in text for text in code_strings)
        assert '"-o", "command="' in path.read_text()

    def test_it_asks_for_unlimited_width(self):
        """ps truncates to the terminal width, silently breaking long argv."""
        source = (ROOT / "src" / "woltspace" / "processes.py").read_text()
        assert '"-ww"' in source

    def test_the_executable_is_matched_whole(self):
        from woltspace.processes import pid_runs_program

        runner = fake_ps({os.getpid(): "/usr/local/bin/cloudflared tunnel --url http://x"})
        assert pid_runs_program(os.getpid(), "cloudflared", runner=runner) is True

    def test_a_process_merely_mentioning_it_is_not_it(self):
        from woltspace.processes import pid_runs_program

        runner = fake_ps({os.getpid(): "tail -f /tmp/abc-cloudflared.log"})
        assert pid_runs_program(os.getpid(), "cloudflared", runner=runner) is False

    def test_an_argv_token_must_be_whole(self):
        from woltspace.processes import pid_argv_has_token

        assert pid_argv_has_token(
            os.getpid(), "woltspace",
            runner=fake_ps({os.getpid(): "python -m woltspace serve --port 7777"}),
        ) is True
        assert pid_argv_has_token(
            os.getpid(), "woltspace",
            runner=fake_ps({os.getpid(): "tail -f /var/log/woltspace.log"}),
        ) is False

    def test_a_dead_pid_is_never_anything(self):
        from woltspace.processes import pid_argv_has_token, pid_runs_program

        assert pid_runs_program(999999, "cloudflared", runner=fake_ps({})) is False
        assert pid_argv_has_token(999999, "woltspace", runner=fake_ps({})) is False

    def test_an_unusable_ps_reports_nothing_rather_than_crashing(self):
        from woltspace.processes import process_command

        def broken(argv, **kwargs):
            raise OSError("ps: not found")

        assert process_command(os.getpid(), runner=broken) == ""

    def test_the_reaper_uses_the_injected_ps(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(
            layout, [_plan()], runner=fake_ps({4242: "python -m bot.telegram_adapter"})
        )
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "connectors.json").write_text(json.dumps({
            "connectors": [{
                "name": "telegram", "pid": 999999,
                "command": [sys.executable, "-m", "bot.telegram_adapter"],
                "process_signature": ["-m", "bot.telegram_adapter"],
            }],
        }))
        # Dead pid: nothing to reap, and no /proc lookup involved.
        assert supervisor.reap_orphans() == []


class TestSpawnAndStopDoNotRaceOnState:
    """R3: a child registered under the lock but published outside it."""

    def test_a_child_born_during_shutdown_is_not_reported_running(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor._stopping.set()  # stop() has already begun
        supervisor.layout.logs_dir.mkdir(parents=True, exist_ok=True)
        supervisor._spawn("telegram")
        state = supervisor.states["telegram"]
        assert state.state == "stopped"
        assert state.pid is None
        assert supervisor._children == {}

    def test_stop_marks_state_under_the_same_lock_it_clears_children(self, tmp_path):
        """The interleaving that let a dead child be published as running."""
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor.start(watch=False)
        pid = supervisor.states["telegram"].pid
        assert pid and _alive(pid)

        supervisor.stop()
        assert supervisor.states["telegram"].state == "stopped"
        assert supervisor.states["telegram"].pid is None
        assert read_connector_report(layout)["connectors"][0]["state"] == "stopped"

        # A late _spawn arriving after stop() cannot resurrect the record.
        supervisor._spawn("telegram")
        assert supervisor.states["telegram"].state == "stopped"
        assert supervisor.states["telegram"].pid is None

    def test_registration_and_publication_happen_together(self):
        """Guard the guard: state must be set inside the lock that inserts."""
        import re

        source = (ROOT / "src" / "woltspace" / "channel_supervisor.py").read_text()
        body = source.split("def _spawn(")[1].split("def _terminate(")[0]
        block = re.search(r"with self\._lock:(.*?)\n        if stillborn", body, re.S)
        assert block, "the spawn lock block moved; re-check the race"
        assert 'state.state = "running"' in block.group(1)
        assert "self._children[name] = child" in block.group(1)


class TestAStaleTunnelPidIsNeverSignalled:
    """Gate A: the guard called it stale, and then something shot it anyway.

    `shared_data_root_check` classifies a live-but-recycled tunnel pid as stale
    so a start may proceed — and `start_tunnel` then handed that same pid to
    `stop_cloudflared`, which only checked that it was alive. The validated
    evidence standard has to hold at the point that signals, not only at the
    point that decides.
    """

    @pytest.fixture
    def innocent_process(self):
        """A live pid recorded in tunnel.json that is emphatically not a tunnel."""
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            start_new_session=True,
        )
        time.sleep(0.2)
        try:
            yield child
        finally:
            child.kill()
            child.wait()

    def _tunnel_lib(self):
        sys.path.insert(0, str(ROOT / "container" / "lib"))
        import tunnel as tunnel_lib

        return tunnel_lib

    def test_a_log_file_named_after_cloudflared_is_not_cloudflared(self, tmp_path):
        """codexw's round-2 repro — and our own logs are named `*-cloudflared.log`."""
        tunnel_lib = self._tunnel_lib()
        log = tmp_path / "abc-cloudflared.log"
        log.write_text("")
        child = subprocess.Popen(
            ["tail", "-f", str(log)], start_new_session=True,
        )
        time.sleep(0.3)
        try:
            assert tunnel_lib.is_cloudflared(child.pid) is False
            assert tunnel_lib.stop_cloudflared(child.pid) is False
            time.sleep(0.3)
            assert child.poll() is None, "an unrelated tail was signalled"
        finally:
            child.kill()
            child.wait()

    def test_identity_comes_from_the_executable_not_the_argv(self):
        tunnel_lib = self._tunnel_lib()
        me = os.getpid()  # a pid that is genuinely alive
        assert tunnel_lib.is_cloudflared(
            me, runner=fake_ps({me: "/usr/local/bin/cloudflared tunnel run"})
        ) is True
        assert tunnel_lib.is_cloudflared(
            me, runner=fake_ps({me: "tail -f cloudflared.log"})
        ) is False

    def test_stop_cloudflared_refuses_a_pid_that_is_not_cloudflared(self, innocent_process):
        tunnel_lib = self._tunnel_lib()
        assert tunnel_lib.stop_cloudflared(innocent_process.pid) is False
        time.sleep(0.2)
        assert innocent_process.poll() is None, "an innocent process was signalled"

    def test_stop_cloudflared_still_stops_a_real_one(self):
        tunnel_lib = self._tunnel_lib()
        runner = fake_ps({4242: "cloudflared tunnel --url http://localhost:7777"})
        signalled = []
        original = os.kill
        try:
            os.kill = lambda pid, sig: signalled.append((pid, sig)) if sig else None
            assert tunnel_lib.stop_cloudflared(4242, runner=runner) is True
        finally:
            os.kill = original
        assert signalled and signalled[0][0] == 4242

    def test_a_dead_pid_is_simply_false(self):
        tunnel_lib = self._tunnel_lib()
        assert tunnel_lib.stop_cloudflared(999999) is False

    def test_the_guard_and_the_start_path_agree(self, tmp_path, innocent_process):
        """Composed: guard says stale → start_tunnel must discard, not signal."""
        from woltspace.doctor import shared_data_root_check

        layout = _layout(tmp_path, isolation="external")
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "tunnel.json").write_text(json.dumps({
            "pid": innocent_process.pid, "url": "https://stale.example",
            "instance_id": "a-previous-boot",
        }))

        # The guard allows the start: this pid is not cloudflared, so the
        # record is stale rather than evidence of a live control plane.
        assert shared_data_root_check(layout, as_entrypoint=True) is None

        # …and the start path must therefore not signal it either.
        tunnel_lib = self._tunnel_lib()
        assert tunnel_lib.stop_cloudflared(innocent_process.pid) is False
        time.sleep(0.3)
        assert innocent_process.poll() is None, (
            "the guard classified this pid as stale and something killed it anyway"
        )

    def test_the_kill_point_validates_rather_than_trusting_its_caller(self):
        source = (ROOT / "container" / "lib" / "tunnel.py").read_text()
        body = source.split("def stop_cloudflared(")[1].split("\ndef ")[0]
        assert "is_cloudflared(" in body, (
            "stop_cloudflared must validate the process itself; callers act on "
            "stale state by definition"
        )

    def test_identity_is_an_equality_not_a_membership(self):
        """Guard the guard: substring matching is what failed twice."""
        source = (ROOT / "container" / "lib" / "tunnel.py").read_text()
        body = source.split("def is_cloudflared(")[1].split("\ndef ")[0]
        assert "CLOUDFLARED_NEEDLE in " not in body
        assert "== CLOUDFLARED_NEEDLE" in body


class TestTheReaperMarkerIsSpecific:
    """Gate B: "bot/" matched anything, and killpg is not a forgiving verb."""

    def _dev_command(self, tmp_path):
        from woltspace.channels import TelegramConnector

        layout = _layout(tmp_path, isolation="external")
        return TelegramConnector().plan(layout, {
            "WOLTSPACE_ENTRYPOINT": "1",
            "ENABLE_TELEGRAM_BOT": "true",
            "TELEGRAM_BOT_TOKEN": "1:t",
            "TELEGRAM_BOT_DIR": str(ROOT / "container"),
            "DEV_MODE": "true",
        })

    def test_the_dev_command_still_ends_in_the_generic_directory(self, tmp_path):
        """The shape that made command[-1] unusable as a marker."""
        plan = self._dev_command(tmp_path)
        assert plan.command[-1] == "bot/"

    def test_but_the_signature_is_a_token_pair(self, tmp_path):
        plan = self._dev_command(tmp_path)
        assert plan.process_signature == ("-m", "bot.telegram_adapter")
        assert plan.to_record()["process_signature"] == ["-m", "bot.telegram_adapter"]

    def test_the_real_dev_process_matches(self, tmp_path):
        from woltspace.channel_supervisor import _matches_connector

        plan = self._dev_command(tmp_path)
        argv = " ".join(plan.command)
        assert _matches_connector(
            4242, plan.to_record(), runner=fake_ps({4242: argv})
        ) is True

    def test_an_unrelated_recycled_pid_containing_bot_slash_does_not(self, tmp_path):
        from woltspace.channel_supervisor import _matches_connector

        plan = self._dev_command(tmp_path)
        for impostor in (
            "rsync -a /srv/bot/ /backup/bot/",
            "python -m http.server --directory bot/",
            "tail -f bot/output.log",
            # codexw's round-2 repro: a file *named* after the marker.
            "tail -f /tmp/logs/bot.telegram_adapter.log",
            "less /var/log/bot.telegram_adapter",
            "grep -r bot.telegram_adapter /srv",
        ):
            assert _matches_connector(
                4242, plan.to_record(), runner=fake_ps({4242: impostor})
            ) is False, impostor

    def test_a_record_with_the_old_string_marker_kills_nothing(self, tmp_path):
        """Round-1 records carry `process_marker`, which no longer matches."""
        from woltspace.channel_supervisor import _matches_connector

        legacy = {
            "name": "telegram", "pid": 4242,
            "command": [sys.executable, "-m", "watchfiles", "bot/"],
            "process_marker": "bot.telegram_adapter",
        }
        assert _matches_connector(
            4242, legacy,
            runner=fake_ps({4242: "python -m bot.telegram_adapter"}),
        ) is False

    def test_the_signature_must_be_adjacent(self, tmp_path):
        """Both tokens present but apart is not the pair."""
        from woltspace.channel_supervisor import _matches_connector

        plan = self._dev_command(tmp_path)
        assert _matches_connector(
            4242, plan.to_record(),
            runner=fake_ps({4242: "python -m other.module --note bot.telegram_adapter"}),
        ) is False

    def test_a_record_without_a_marker_kills_nothing(self, tmp_path):
        """Fail closed: no marker, no match, no signal."""
        from woltspace.channel_supervisor import _matches_connector

        legacy = {"name": "telegram", "pid": 4242, "command": ["python", "-m", "bot/"]}
        assert _matches_connector(
            4242, legacy, runner=fake_ps({4242: "anything at all bot/"})
        ) is False

    def test_the_reaper_leaves_an_impostor_alone_end_to_end(self, tmp_path):
        """Composed: a recycled pid recorded as ours must survive reap_orphans."""
        layout = _layout(tmp_path)
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)  # /srv/bot/ backup"],
            start_new_session=True,
        )
        time.sleep(0.2)
        try:
            (layout.platform_state / "connectors.json").write_text(json.dumps({
                "connectors": [{
                    "name": "telegram", "pid": child.pid,
                    "command": [sys.executable, "-m", "watchfiles", "bot/"],
                    "process_marker": "bot.telegram_adapter",
                }],
            }))
            supervisor = ChannelSupervisor(layout, [_plan()])
            assert supervisor.reap_orphans() == []
            time.sleep(0.3)
            assert child.poll() is None, "an unrelated process was killpg'd"
        finally:
            child.kill()
            child.wait()

    def test_it_still_reaps_a_genuine_orphan_end_to_end(self, tmp_path):
        layout = _layout(tmp_path)
        first = ChannelSupervisor(layout, [_plan()])
        first.start(watch=False)
        orphan = read_connector_report(layout)["connectors"][0]["pid"]
        first._children.clear()
        assert _alive(orphan)

        second = ChannelSupervisor(layout, [_plan()])
        try:
            assert orphan in second.reap_orphans()
            for _ in range(40):
                if not _alive(orphan):
                    break
                time.sleep(0.05)
            assert not _alive(orphan)
        finally:
            second.stop()


class TestNoStateIsLeftPending:
    """Advisory: memory said stopped while connectors.json still said pending."""

    def test_a_stop_before_any_spawn_publishes_stopped(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor.stop()
        record = read_connector_report(layout)["connectors"][0]
        assert record["state"] == "stopped"
        assert record["pid"] is None

    def test_a_disabled_connector_stays_disabled(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(
            layout, [ConnectorPlan("telegram", False, "disabled")]
        )
        supervisor.start(watch=False)
        supervisor.stop()
        assert read_connector_report(layout)["connectors"][0]["state"] == "disabled"

    def test_memory_and_the_report_agree_after_stop(self, tmp_path):
        layout = _layout(tmp_path)
        supervisor = ChannelSupervisor(layout, [_plan()])
        supervisor.start(watch=False)
        supervisor.stop()
        record = read_connector_report(layout)["connectors"][0]
        assert record["state"] == supervisor.states["telegram"].state == "stopped"
