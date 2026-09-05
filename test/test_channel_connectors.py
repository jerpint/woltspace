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

    def test_container_keeps_its_own_bot_project_and_module(self, layout):
        external = RuntimeLayout(
            layout.wolts_dir, layout.install_root, layout.host, layout.port, "external"
        )
        plan = TelegramConnector().plan(external, {
            "ENABLE_TELEGRAM_BOT": "true",
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_BOT_DIR": "/workspace/wolts/mywolt",
            "TELEGRAM_BOT_MODULE": "wolt.bot.telegram_adapter",
        })
        assert plan.enabled is True
        assert plan.cwd == "/workspace/wolts/mywolt"
        assert plan.command[-1] == "wolt.bot.telegram_adapter"

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
