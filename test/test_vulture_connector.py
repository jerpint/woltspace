"""The vulture runs natively as a supervised child, like the wolf.

Nothing launched it after the container's boot stopped doing so, so idle
sessions were never closed on any lodge.
"""

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
    VULTURE_MODULE,
    VultureConnector,
    plan_connectors,
)
from woltspace.layout import RuntimeLayout  # noqa: E402

ENTRY = {"WOLTSPACE_ENTRYPOINT": "1"}


def _layout(tmp_path, install_root=None, port=7799):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts", install_root=install_root or ROOT,
        host="127.0.0.1", port=port, isolation="host",
    )


class TestPlan:
    def test_enabled_by_default_for_the_entrypoint(self, tmp_path):
        plan = VultureConnector().plan(_layout(tmp_path, port=8123), ENTRY)
        assert plan.enabled is True
        assert plan.command[1:] == ("-m", VULTURE_MODULE)
        assert plan.process_signature == ("-m", VULTURE_MODULE)
        assert plan.cwd == str(ROOT / "container")
        assert plan.env["WOLTSPACE_WOLTS_DIR"] == str(tmp_path / "wolts")
        assert plan.env["WOLTSPACE_API"] == "http://127.0.0.1:8123"

    def test_a_guest_never_reaps(self, tmp_path):
        plan = VultureConnector().plan(_layout(tmp_path), {})
        assert plan.enabled is False
        assert plan.command == ()

    def test_can_be_switched_off(self, tmp_path):
        layout = _layout(tmp_path)
        off = VultureConnector().plan(layout, {**ENTRY, "WOLTSPACE_VULTURE": "false"})
        assert off.enabled is False
        layout.platform_state.mkdir(parents=True)
        (layout.platform_state / "config.json").write_text(
            json.dumps({"channels": {"vulture": {"enabled": False}}})
        )
        plan = VultureConnector().plan(layout, ENTRY)
        assert plan.enabled is False
        assert "channels.vulture" in plan.remedy

    def test_an_install_without_the_runtime_is_a_remedy(self, tmp_path):
        root = tmp_path / "bundle"
        (root / "server").mkdir(parents=True)
        plan = VultureConnector().plan(_layout(tmp_path, root), ENTRY)
        assert plan.enabled is False
        assert VULTURE_MODULE in plan.detail

    def test_plan_connectors_carries_the_vulture(self, tmp_path):
        names = [plan.name for plan in plan_connectors(_layout(tmp_path), ENTRY)]
        assert names[-1] == "vulture"


class TestSupervision:
    def test_an_enabled_vulture_is_spawned_and_reported(self, tmp_path):
        # The real module against an empty colony: one pass finds nothing,
        # then it sleeps — so this exercises the actual command.
        layout = _layout(tmp_path)
        layout.wolts_dir.mkdir(parents=True)
        sup = ChannelSupervisor(layout, [VultureConnector().plan(layout, ENTRY)])
        sup.start(watch=False)
        try:
            record = read_connector_report(layout)["connectors"][0]
            assert record["name"] == "vulture"
            assert record["state"] == "running"
            pid = record["pid"]
            assert pid and pid > 0
        finally:
            sup.stop()
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    def test_the_control_plane_supervises_it(self, tmp_path, monkeypatch):
        from woltspace.supervisor import Supervisor

        for key in ("WOLTSPACE_VULTURE", "ENABLE_TELEGRAM_BOT", "TELEGRAM_BOT_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        channels = Supervisor(
            _layout(tmp_path), probe_token=lambda token: False
        ).channel_supervisor()
        assert channels.states["vulture"].plan.enabled is True
