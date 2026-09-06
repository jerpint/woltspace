"""ChannelConnector seam: configuration, planning, and supervision."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.channel_supervisor import (  # noqa: E402
    ChannelSupervisor,
    backoff_delay,
    read_connector_report,
    report_path,
)
from woltspace.channels import (  # noqa: E402
    ConnectorPlan,
    TelegramConnector,
    connector_secrets,
    plan_connectors,
)
from woltspace.config import channel_config, config_path, load_config  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402

TOKEN = "123456:configured-telegram-token"


@pytest.fixture
def layout(tmp_path):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=ROOT,
        host="127.0.0.1",
        port=7799,
        isolation="host",
    )


def write_config(layout, payload):
    layout.platform_state.mkdir(parents=True, exist_ok=True)
    path = layout.platform_state / "config.json"
    path.write_text(json.dumps(payload))
    return path


class TestNativeConfig:
    def test_config_lives_in_the_data_root_not_a_dotenv(self, layout):
        assert config_path(layout, {}) == layout.platform_state / "config.json"

    def test_missing_or_broken_config_is_an_empty_mapping(self, layout):
        assert load_config(layout, {}) == {}
        write_config(layout, {})
        (layout.platform_state / "config.json").write_text("{not json")
        assert load_config(layout, {}) == {}

    def test_explicit_config_override_wins(self, layout, tmp_path):
        elsewhere = tmp_path / "custom.json"
        elsewhere.write_text(json.dumps({"channels": {"telegram": {"token": "t"}}}))
        env = {"WOLTSPACE_CONFIG": str(elsewhere)}
        assert config_path(layout, env) == elsewhere
        assert channel_config(layout, "telegram", env) == {"token": "t"}


class TestTelegramPlan:
    def test_disabled_without_config(self, layout):
        plan = TelegramConnector().plan(layout, {})
        assert plan.enabled is False
        assert plan.command == ()
        assert "config.json" in plan.remedy

    def test_enabled_from_data_root_config(self, layout):
        write_config(layout, {
            "channels": {
                "telegram": {"enabled": True, "token": TOKEN, "allowed_users": [7, 8]}
            }
        })
        plan = TelegramConnector().plan(layout, {})
        assert plan.enabled is True
        assert plan.command[-2:] == ("-m", "bot.telegram_adapter")
        assert plan.env["TELEGRAM_BOT_TOKEN"] == TOKEN
        assert plan.env["TELEGRAM_ALLOWED_USERS"] == "7,8"
        assert plan.env["WOLTS_DIR"] == str(layout.wolts_dir)

    def test_environment_overrides_the_config_file(self, layout):
        write_config(layout, {
            "channels": {"telegram": {"enabled": True, "token": "from-file"}}
        })
        plan = TelegramConnector().plan(
            layout, {"TELEGRAM_BOT_TOKEN": "from-env"}
        )
        assert plan.env["TELEGRAM_BOT_TOKEN"] == "from-env"

    def test_environment_can_disable_an_enabled_config(self, layout):
        write_config(layout, {
            "channels": {"telegram": {"enabled": True, "token": TOKEN}}
        })
        plan = TelegramConnector().plan(layout, {"ENABLE_TELEGRAM_BOT": "false"})
        assert plan.enabled is False

    def test_enabled_without_a_token_names_the_fix(self, layout):
        write_config(layout, {"channels": {"telegram": {"enabled": True}}})
        plan = TelegramConnector().plan(layout, {})
        assert plan.enabled is False
        assert "token" in plan.detail
        assert "TELEGRAM_BOT_TOKEN" in plan.remedy

    def _container(self, layout):
        return RuntimeLayout(
            layout.wolts_dir, layout.install_root, layout.host, layout.port, "external"
        )

    def test_container_keeps_its_own_bot_project_and_module(self, layout):
        """The container entrypoint runs the adapter from the `bot` uv project."""
        plan = TelegramConnector().plan(self._container(layout), {
            "WOLTSPACE_ENTRYPOINT": "1",
            "ENABLE_TELEGRAM_BOT": "true",
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_BOT_DIR": str(ROOT / "container"),
            "TELEGRAM_BOT_MODULE": "bot.telegram_adapter",
        })
        assert plan.enabled is True
        assert plan.cwd == str(ROOT / "container")
        assert plan.command[0].endswith("uv")
        assert plan.command[1:5] == ("run", "--project", str(ROOT / "container" / "bot"), "python")
        assert plan.command[-2:] == ("-m", "bot.telegram_adapter")

    def test_dev_mode_keeps_hot_reload_as_one_supervised_child(self, layout):
        plan = TelegramConnector().plan(self._container(layout), {
            "WOLTSPACE_ENTRYPOINT": "1",
            "ENABLE_TELEGRAM_BOT": "true",
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_BOT_DIR": str(ROOT / "container"),
            "DEV_MODE": "true",
        })
        assert plan.enabled is True
        assert "watchfiles" in plan.command
        assert plan.command[-1] == "bot/"
        assert "bot.telegram_adapter" in plan.command[-2]
        assert "dev reload" in plan.detail

    def test_a_custom_wolt_adapter_still_finds_an_interpreter_with_telegram(self, layout):
        """A wolt's own adapter lives at <wolt>/wolt/bot, not <wolt>/bot.

        Probing only the latter fell through to an interpreter without
        python-telegram-bot and then blamed a missing extra.
        """
        plan = TelegramConnector().plan(self._container(layout), {
            "WOLTSPACE_ENTRYPOINT": "1",
            "ENABLE_TELEGRAM_BOT": "true",
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_BOT_DIR": "/workspace/wolts/mywolt",
            "TELEGRAM_BOT_MODULE": "wolt.bot.telegram_adapter",
        })
        assert plan.enabled is True
        assert plan.cwd == "/workspace/wolts/mywolt"
        assert plan.command[-1] == "wolt.bot.telegram_adapter"
        # Falls back to the platform's bot project, which owns the dependency.
        assert str(ROOT / "container" / "bot") in plan.command


class TestAmbientEnvironmentIsNeverEnough:
    """A stray `woltspace serve` in the container must not spawn a rival bot.

    `container/start.sh` exports ENABLE_TELEGRAM_BOT and the production
    TELEGRAM_BOT_TOKEN to every process it hosts, so inheriting them is not
    evidence that anyone wants a connector *here*.
    """

    CONTAINER_ENV = {
        "WOLTSPACE_ISOLATION": "external",
        "ENABLE_TELEGRAM_BOT": "true",
        "TELEGRAM_BOT_TOKEN": TOKEN,
        "TELEGRAM_BOT_DIR": "/workspace/woltspace/container",
        "DEV_MODE": "true",
    }

    def test_a_guest_refuses_to_spawn_from_inherited_environment(self, layout):
        plan = TelegramConnector().plan(layout, dict(self.CONTAINER_ENV))
        assert plan.enabled is False
        assert plan.command == ()
        assert "ambient environment" in plan.detail
        assert "different bot token" in plan.remedy

    def test_the_entrypoint_may_use_the_environment(self, layout):
        plan = TelegramConnector().plan(
            layout, {**self.CONTAINER_ENV, "WOLTSPACE_ENTRYPOINT": "1"}
        )
        assert plan.enabled is True

    def test_a_data_root_that_declares_it_may_use_it_without_the_entrypoint(self, layout):
        """A config.json in this data root is a deliberate statement."""
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "config.json").write_text(json.dumps({
            "channels": {"telegram": {"enabled": True, "token": "own-token"}}
        }))
        plan = TelegramConnector().plan(layout, dict(self.CONTAINER_ENV))
        assert plan.enabled is True

    def test_the_environment_can_still_switch_it_off(self, layout):
        layout.platform_state.mkdir(parents=True, exist_ok=True)
        (layout.platform_state / "config.json").write_text(json.dumps({
            "channels": {"telegram": {"enabled": True, "token": "own-token"}}
        }))
        plan = TelegramConnector().plan(layout, {
            **self.CONTAINER_ENV, "ENABLE_TELEGRAM_BOT": "false",
        })
        assert plan.enabled is False

    def test_no_secret_reaches_the_public_record(self, layout):
        write_config(layout, {
            "channels": {"telegram": {"enabled": True, "token": TOKEN}}
        })
        plan = TelegramConnector().plan(layout, {})
        assert TOKEN not in json.dumps(plan.to_record())
        assert "env" not in plan.to_record()

    def test_secrets_are_offered_only_for_enabled_connectors(self, layout):
        write_config(layout, {
            "channels": {"telegram": {"enabled": True, "token": TOKEN}}
        })
        assert connector_secrets(plan_connectors(layout, {}))["TELEGRAM_BOT_TOKEN"] == TOKEN
        assert connector_secrets([ConnectorPlan("telegram", False, "disabled")]) == {}


def _sleeper(seconds: float = 30) -> tuple[str, ...]:
    return (sys.executable, "-c", f"import time; time.sleep({seconds})")


def _crasher() -> tuple[str, ...]:
    return (sys.executable, "-c", "import sys; sys.exit(3)")


def running_plan(command, cwd=None):
    return ConnectorPlan("telegram", True, "test child", command, cwd or str(ROOT), {})


class TestChannelSupervisor:
    def test_disabled_connector_is_reported_not_spawned(self, layout):
        sup = ChannelSupervisor(layout, [ConnectorPlan("telegram", False, "disabled")])
        sup.start()
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "disabled"
            assert record["pid"] is None
        finally:
            sup.stop()

    def test_running_connector_is_visible_and_stops_cleanly(self, layout):
        sup = ChannelSupervisor(layout, [running_plan(_sleeper())])
        sup.start()
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "running"
            pid = record["pid"]
            assert pid and pid > 0
        finally:
            sup.stop()
        after = read_connector_report(layout)["connectors"][0]
        assert after["state"] == "stopped"
        assert after["pid"] is None
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_dead_connector_is_restarted(self, layout):
        sup = ChannelSupervisor(
            layout, [running_plan(_sleeper())], poll_interval=0.05
        )
        sup.start()
        try:
            first = read_connector_report(layout)["connectors"][0]["pid"]
            os.kill(first, 9)
            deadline = time.monotonic() + 20
            record = {}
            while time.monotonic() < deadline:
                record = read_connector_report(layout)["connectors"][0]
                if record.get("state") == "running" and record.get("pid") != first:
                    break
                time.sleep(0.1)
            assert record["state"] == "running", record
            assert record["pid"] != first
            assert record["restarts"] == 1
        finally:
            sup.stop()

    def test_restarts_are_bounded_and_end_with_a_named_failure(self, layout):
        sup = ChannelSupervisor(
            layout,
            [running_plan(_crasher())],
            max_restarts=2,
            poll_interval=0,
            sleep=lambda _seconds: None,
        )
        sup.start(watch=False)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.poll_once()
                if sup.states["telegram"].state == "failed":
                    break
                time.sleep(0.02)
            state = sup.states["telegram"]
            assert state.state == "failed"
            assert state.restarts == 2
            assert "not restarting" in state.error
            assert state.last_exit_code == 3
        finally:
            sup.stop()

    def test_a_child_that_never_lives_gives_up_with_its_remedy(self, layout):
        """A busy port is not a transient crash — restarting is just waiting slower."""
        plan = ConnectorPlan(
            "tui", True, "pty bridge", _crasher(), str(ROOT), {},
            remedy="Something else holds port 7778; pick another with WOLTSPACE_TUI_PORT.",
        )
        sup = ChannelSupervisor(
            layout, [plan], max_fast_exits=3, poll_interval=0, sleep=lambda _seconds: None
        )
        sup.start(watch=False)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.poll_once()
                if sup.states["tui"].state == "failed":
                    break
                time.sleep(0.02)
            state = sup.states["tui"]
            assert state.state == "failed"
            assert state.restarts == 2, "gave up on the third stillbirth, not the sixth"
            assert "within 5s of starting" in state.error
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "failed"
            assert "WOLTSPACE_TUI_PORT" in record["remedy"]
            # Failed is final — further ticks must not resurrect it.
            pid_before = record["pid"]
            sup.poll_once()
            assert sup.states["tui"].state == "failed"
            assert sup.states["tui"].restarts == 2
            assert pid_before is None
        finally:
            sup.stop()

    def test_a_busy_port_is_named_on_the_first_death_not_the_fifth(self, layout):
        """The bridge defaults to API port + 1, so an instance one port above
        another lands on that one's bridge. It used to crash-loop through five
        stillbirths and then report only "exited within 5s five times" — true,
        and useless. Say what actually happened, immediately."""
        binder = (
            sys.executable, "-c",
            "import sys; print('Error: listen EADDRINUSE: address already in use :::7778');"
            " sys.exit(1)",
        )
        plan = ConnectorPlan(
            "tui", True, "pty bridge", binder, str(ROOT), {},
            remedy="Something else holds port 7778; pick another with WOLTSPACE_TUI_PORT.",
        )
        sup = ChannelSupervisor(
            layout, [plan], poll_interval=0, sleep=lambda _seconds: None
        )
        sup.start(watch=False)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.poll_once()
                if sup.states["tui"].state == "failed":
                    break
                time.sleep(0.02)
            state = sup.states["tui"]
            assert state.state == "failed"
            assert state.restarts == 0, "gave up on the first death, not the fifth"
            assert "cannot bind its port" in state.error
            assert "7778" in state.error, "the colliding port is in the parting words"
            record = read_connector_report(layout)["connectors"][0]
            assert "WOLTSPACE_TUI_PORT" in record["remedy"]
        finally:
            sup.stop()

    def test_a_child_that_lived_a_while_still_gets_the_full_budget(self, layout):
        """Only *fast* exits are hopeless; a connector that ran is retried."""
        clock = {"now": 1000.0}
        sup = ChannelSupervisor(
            layout,
            [running_plan(_crasher())],
            max_fast_exits=2,
            poll_interval=0,
            sleep=lambda _seconds: None,
            clock=lambda: clock["now"],
        )
        sup.start(watch=False)
        try:
            for _ in range(4):
                clock["now"] += 60.0  # the child ran for a minute before dying
                deadline = time.monotonic() + 20
                restarts_before = sup.states["telegram"].restarts
                while time.monotonic() < deadline:
                    sup.poll_once()
                    if sup.states["telegram"].restarts > restarts_before:
                        break
                    time.sleep(0.02)
            state = sup.states["telegram"]
            assert state.state != "failed", state.error
            assert state.restarts == 4
        finally:
            sup.stop()

    def test_the_phrase_in_a_healthy_childs_log_is_not_a_life_sentence(self, layout):
        """A connector relays messages. One of them can say "address in use".

        The marker used to be latched forever on the first sighting: the child
        went on serving happily for an hour, died of something else entirely,
        and was buried as a port clash that never got a restart.
        """
        clock = {"now": 1000.0}
        talker = (
            sys.executable, "-c",
            "import sys, time;"
            " print('user said: address already in use, lol'); sys.stdout.flush();"
            " time.sleep(30)",
        )
        sup = ChannelSupervisor(
            layout,
            [running_plan(talker)],
            poll_interval=0,
            sleep=lambda _seconds: None,
            clock=lambda: clock["now"],
        )
        sup.start(watch=False)
        try:
            state = sup.states["telegram"]
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not state.port_clash:
                sup.poll_once()
                time.sleep(0.02)
            assert state.port_clash, "the marker was in the log and was noticed"
            assert state.state == "running"

            first = state.pid
            clock["now"] += 3600.0  # an hour of honest work
            os.kill(first, 9)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.poll_once()
                if state.state == "running" and state.pid != first:
                    break
                time.sleep(0.02)
            assert state.state == "running", state.error
            assert state.restarts == 1
        finally:
            sup.stop()

    def test_a_respawn_starts_the_clash_flag_clean(self, layout):
        """The flag describes this incarnation's log, not the last one's."""
        clock = {"now": 1000.0}
        sup = ChannelSupervisor(
            layout,
            [running_plan(_sleeper())],
            poll_interval=0,
            sleep=lambda _seconds: None,
            clock=lambda: clock["now"],
        )
        sup.start(watch=False)
        try:
            state = sup.states["telegram"]
            state.port_clash = True  # a sighting from the life about to end
            first = state.pid
            clock["now"] += 3600.0
            os.kill(first, 9)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                sup.poll_once()
                if state.state == "running" and state.pid != first:
                    break
                time.sleep(0.02)
            assert state.state == "running", state.error
            assert state.port_clash is False
        finally:
            sup.stop()

    def test_backoff_is_bounded(self):
        assert backoff_delay(1) == 1.0
        assert backoff_delay(2) == 2.0
        assert backoff_delay(50) == 30.0

    def test_report_file_is_owner_only_and_carries_no_token(self, layout):
        write_config(layout, {
            "channels": {"telegram": {"enabled": True, "token": TOKEN}}
        })
        plans = plan_connectors(layout, {})
        sup = ChannelSupervisor(layout, [ConnectorPlan(
            plans[0].name, True, plans[0].detail, _sleeper(), str(ROOT), plans[0].env
        )])
        sup.start()
        try:
            path = report_path(layout)
            assert TOKEN not in path.read_text()
            assert path.stat().st_mode & 0o777 == 0o600
        finally:
            sup.stop()

    def test_child_receives_the_connector_environment(self, layout, tmp_path):
        marker = tmp_path / "seen.txt"
        command = (
            sys.executable,
            "-c",
            f"import os,pathlib; pathlib.Path({str(marker)!r}).write_text("
            "os.environ.get('TELEGRAM_BOT_TOKEN','')); import time; time.sleep(30)",
        )
        plan = ConnectorPlan(
            "telegram", True, "env probe", command, str(ROOT),
            {"TELEGRAM_BOT_TOKEN": TOKEN},
        )
        sup = ChannelSupervisor(layout, [plan])
        sup.start()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.05)
            assert marker.read_text() == TOKEN
        finally:
            sup.stop()

    def test_unspawnable_connector_fails_with_the_os_error(self, layout):
        plan = running_plan(("/nonexistent/woltspace-connector",))
        sup = ChannelSupervisor(layout, [plan])
        sup.start()
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["state"] == "failed"
            assert record["error"]
        finally:
            sup.stop()


class TestStatusRendering:
    def test_status_lines_name_state_and_remedy(self):
        from woltspace.cli import format_connector_lines

        lines = format_connector_lines({"health": {"connectors": [
            {"name": "telegram", "state": "running", "detail": "adapter", "pid": 42,
             "restarts": 1},
            {"name": "slack", "state": "disabled", "detail": "disabled",
             "remedy": "edit config.json"},
        ]}})
        assert "connector telegram: running · adapter · pid 42 · 1 restart(s)" in lines
        assert "connector slack: disabled · disabled" in lines
        assert "  fix: edit config.json" in lines

    def test_no_connectors_renders_nothing(self):
        from woltspace.cli import format_connector_lines

        assert format_connector_lines({"health": {}}) == []


class TestContainerEntrypoint:
    def test_start_sh_no_longer_launches_a_second_telegram_bot(self):
        text = (ROOT / "container" / "start.sh").read_text()
        assert "TELEGRAM_BOT_MODULE" not in text
        assert "ChannelConnector" in text
