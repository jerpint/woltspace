"""The wolf runs natively as a supervised child, like every other connector."""

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.channel_supervisor import (  # noqa: E402
    ChannelSupervisor,
    read_connector_report,
)
from woltspace.channels import (  # noqa: E402
    CONNECTORS,
    WOLF_MODULE,
    WolfConnector,
    plan_connectors,
)
from woltspace.layout import RuntimeLayout  # noqa: E402

ENTRY = {"WOLTSPACE_ENTRYPOINT": "1"}


def _layout(tmp_path, install_root=None, port=7799):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts", install_root=install_root or ROOT,
        host="127.0.0.1", port=port, isolation="host",
    )


def test_the_wolf_joins_the_pack_last():
    assert CONNECTORS[-1].name == "wolf"


class TestPlan:
    def test_enabled_by_default_for_the_entrypoint(self, tmp_path):
        plan = WolfConnector().plan(_layout(tmp_path), ENTRY)
        assert plan.enabled is True
        assert plan.command[1:] == ("-m", WOLF_MODULE)
        assert plan.command[0] == sys.executable
        assert plan.cwd == str(ROOT / "container")
        assert plan.process_signature == ("-m", WOLF_MODULE)
        assert "WOLTSPACE_PORT" not in plan.to_record()  # env is never public

    def test_the_child_is_told_where_the_colony_and_the_plane_are(self, tmp_path):
        plan = WolfConnector().plan(_layout(tmp_path, port=8123), ENTRY)
        assert plan.env["WOLTS_DIR"] == str(tmp_path / "wolts")
        assert plan.env["WOLTSPACE_DIR"] == str(ROOT)
        assert plan.env["WOLTSPACE_PORT"] == "8123"
        parts = plan.env["PYTHONPATH"].split(":")
        assert str(ROOT / "container") in parts
        assert str(ROOT / "container" / "lib") in parts

    def test_the_port_follows_the_layout_not_a_literal(self, tmp_path):
        layout = RuntimeLayout.from_env(
            {"WOLTS_DIR": str(tmp_path / "wolts"), "WOLTSPACE_PORT": "9001"}
        )
        assert WolfConnector().plan(layout, ENTRY).env["WOLTSPACE_PORT"] == "9001"

    def test_a_guest_never_fires_the_schedules(self, tmp_path):
        plan = WolfConnector().plan(_layout(tmp_path), {})
        assert plan.enabled is False
        assert "not the platform entrypoint" in plan.detail
        assert plan.command == ()

    def test_can_be_switched_off(self, tmp_path):
        layout = _layout(tmp_path)
        assert WolfConnector().plan(layout, {**ENTRY, "WOLTSPACE_WOLF": "false"}).enabled is False
        layout.platform_state.mkdir(parents=True)
        (layout.platform_state / "config.json").write_text(
            json.dumps({"channels": {"wolf": {"enabled": False}}})
        )
        plan = WolfConnector().plan(layout, ENTRY)
        assert plan.enabled is False
        assert plan.detail == "disabled"
        assert "channels.wolf" in plan.remedy

    def test_an_install_without_the_runtime_is_a_remedy_not_a_crash_loop(self, tmp_path):
        root = tmp_path / "bundle"
        (root / "server").mkdir(parents=True)
        plan = WolfConnector().plan(_layout(tmp_path, root), ENTRY)
        assert plan.enabled is False
        assert WOLF_MODULE in plan.detail
        assert plan.remedy

    def test_plan_connectors_carries_the_wolf(self, tmp_path):
        plans = plan_connectors(_layout(tmp_path), ENTRY)
        assert [plan.name for plan in plans] == ["telegram", "tui", "wolf"]


class TestSupervision:
    def test_an_enabled_wolf_is_spawned_and_reported(self, tmp_path):
        # The real module against an empty colony: it finds no wolf.json,
        # says so, and idles — so this exercises the actual command.
        layout = _layout(tmp_path)
        sup = ChannelSupervisor(layout, [WolfConnector().plan(layout, ENTRY)])
        sup.start(watch=False)
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["name"] == "wolf"
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

    def test_a_disabled_wolf_is_reported_and_never_spawned(self, tmp_path):
        layout = _layout(tmp_path)
        sup = ChannelSupervisor(layout, [WolfConnector().plan(layout, {})])
        sup.start(watch=False)
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["name"] == "wolf"
            assert record["state"] == "disabled"
            assert record["pid"] is None
        finally:
            sup.stop()

    def test_the_control_plane_supervises_it(self, tmp_path, monkeypatch):
        from woltspace.supervisor import Supervisor

        for key in ("WOLTSPACE_WOLF", "ENABLE_TELEGRAM_BOT", "TELEGRAM_BOT_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        channels = Supervisor(
            _layout(tmp_path), probe_token=lambda token: False
        ).channel_supervisor()
        assert channels.states["wolf"].plan.enabled is True
