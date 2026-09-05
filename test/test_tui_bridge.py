"""The TUI pty bridge is a supervised connector, resolved exactly like the TUI."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace.channels import (  # noqa: E402
    CONNECTORS,
    TUI_BRIDGE_INSTALL_REMEDY,
    TuiBridgeConnector,
    plan_connectors,
    resolve_tui_service,
)
from woltspace.compatibility import TUI_PACKAGE, TUI_SERVICE_BINARY, TUI_VERSION  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402

ENTRY = {"WOLTSPACE_ENTRYPOINT": "1"}


def _layout(tmp_path, install_root=None):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts", install_root=install_root or ROOT,
        host="127.0.0.1", port=7799, isolation="host",
    )


def _probe_runner(payload, returncode=0):
    return lambda *a, **k: SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=returncode)


def _wheel_root(tmp_path):
    """An install root shaped like the wheel bundle: no tui/ beside it."""
    root = tmp_path / "bundle"
    (root / "server").mkdir(parents=True)
    return root


def test_the_bridge_is_the_second_connector_and_telegram_stays_first():
    assert [connector.name for connector in CONNECTORS] == ["telegram", "tui"]


class TestResolution:
    def test_a_source_checkout_runs_its_own_script(self, tmp_path):
        layout = _layout(tmp_path)  # ROOT has tui/src/tui-service.js and node_modules
        command, detail, why_not = resolve_tui_service(
            layout, {}, which=lambda name: "/usr/bin/node" if name == "node" else None,
        )
        assert command == ("/usr/bin/node", str(ROOT / "tui" / "src" / "tui-service.js"))
        assert why_not == ""
        assert "node " in detail

    def test_a_checkout_without_node_modules_says_so(self, tmp_path):
        root = tmp_path / "checkout"
        (root / "tui" / "src").mkdir(parents=True)
        (root / "tui" / "src" / "tui-service.js").write_text("")
        command, _detail, why_not = resolve_tui_service(
            _layout(tmp_path, root), {}, which=lambda name: "/usr/bin/node" if name == "node" else None,
        )
        assert command == ()
        assert "no node-pty" in why_not

    def test_a_wheel_install_uses_the_exact_global_bin(self, tmp_path):
        layout = _layout(tmp_path, _wheel_root(tmp_path))
        command, detail, why_not = resolve_tui_service(
            layout, {},
            which=lambda name: f"/tools/{name}",
            runner=_probe_runner({"name": TUI_PACKAGE, "version": TUI_VERSION, "binary": TUI_SERVICE_BINARY}),
        )
        assert command == (f"/tools/{TUI_SERVICE_BINARY}",)
        assert why_not == ""
        assert TUI_SERVICE_BINARY in detail

    def test_a_mismatched_global_bin_is_refused(self, tmp_path):
        layout = _layout(tmp_path, _wheel_root(tmp_path))
        command, _detail, why_not = resolve_tui_service(
            layout, {},
            which=lambda name: f"/tools/{name}",
            runner=_probe_runner({"name": TUI_PACKAGE, "version": "0.0.1", "binary": TUI_SERVICE_BINARY}),
        )
        assert command == ()
        assert "expected" in why_not and "0.0.1" in why_not

    def test_the_tui_bin_does_not_pass_for_the_service_bin(self, tmp_path):
        """Same package, same version, wrong bin — identity is not a substring."""
        layout = _layout(tmp_path, _wheel_root(tmp_path))
        command, _detail, why_not = resolve_tui_service(
            layout, {},
            which=lambda name: f"/tools/{name}",
            runner=_probe_runner({"name": TUI_PACKAGE, "version": TUI_VERSION, "binary": "woltspace-tui"}),
        )
        assert command == ()
        assert why_not

    def test_nothing_found_names_both_install_routes(self, tmp_path):
        layout = _layout(tmp_path, _wheel_root(tmp_path))
        command, _detail, why_not = resolve_tui_service(layout, {}, which=lambda name: None)
        assert command == ()
        assert TUI_SERVICE_BINARY in why_not

    def test_an_explicit_binary_wins(self, tmp_path):
        command, _detail, _ = resolve_tui_service(
            _layout(tmp_path), {"WOLTSPACE_TUI_SERVICE_BIN": "/opt/bridge"}, which=lambda name: None,
        )
        assert command == ("/opt/bridge",)


class TestPlan:
    def test_enabled_by_default_for_the_entrypoint(self, tmp_path):
        plan = TuiBridgeConnector().plan(
            _layout(tmp_path), ENTRY, which=lambda name: "/usr/bin/node" if name == "node" else None,
        )
        assert plan.enabled is True
        assert plan.env["TUI_PORT"] == "3001"
        assert plan.env["WOLT_DIR"] == str(tmp_path / "wolts")
        assert plan.process_signature == (str(ROOT / "tui" / "src" / "tui-service.js"),)
        assert "3001" in plan.detail
        assert "TUI_PORT" not in plan.to_record()  # env is never in the public record

    def test_a_guest_never_binds_the_port(self, tmp_path):
        plan = TuiBridgeConnector().plan(_layout(tmp_path), {})
        assert plan.enabled is False
        assert "not the platform entrypoint" in plan.detail
        assert plan.command == ()

    def test_port_comes_from_env_or_config(self, tmp_path):
        layout = _layout(tmp_path)
        layout.platform_state.mkdir(parents=True)
        (layout.platform_state / "config.json").write_text(json.dumps({"channels": {"tui": {"port": 3210}}}))
        which = lambda name: "/usr/bin/node" if name == "node" else None  # noqa: E731
        assert TuiBridgeConnector().plan(layout, ENTRY, which=which).env["TUI_PORT"] == "3210"
        assert TuiBridgeConnector().plan(
            layout, {**ENTRY, "WOLTSPACE_TUI_PORT": "4000"}, which=which
        ).env["TUI_PORT"] == "4000"

    def test_can_be_switched_off(self, tmp_path):
        layout = _layout(tmp_path)
        assert TuiBridgeConnector().plan(layout, {**ENTRY, "WOLTSPACE_TUI_BRIDGE": "false"}).enabled is False
        layout.platform_state.mkdir(parents=True)
        (layout.platform_state / "config.json").write_text(json.dumps({"channels": {"tui": {"enabled": False}}}))
        plan = TuiBridgeConnector().plan(layout, ENTRY)
        assert plan.enabled is False
        assert plan.detail == "disabled"
        assert "channels.tui" in plan.remedy

    def test_missing_bridge_is_a_named_remedy_not_a_crash_loop(self, tmp_path):
        plan = TuiBridgeConnector().plan(
            _layout(tmp_path, _wheel_root(tmp_path)), ENTRY, which=lambda name: None,
        )
        assert plan.enabled is False
        assert plan.detail.startswith("pty bridge not found")
        assert plan.remedy == TUI_BRIDGE_INSTALL_REMEDY
        assert "npm install -g" in plan.remedy

    def test_plan_connectors_keeps_telegram_at_index_zero(self, tmp_path):
        plans = plan_connectors(_layout(tmp_path), {})
        assert [plan.name for plan in plans] == ["telegram", "tui"]


class TestSupervisorWiring:
    def test_the_server_learns_the_port_before_it_imports(self, tmp_path, monkeypatch):
        import os

        from woltspace.supervisor import Supervisor

        for key in ("TUI_PORT", "WOLTSPACE_TUI_PORT", "ENABLE_TELEGRAM_BOT", "TELEGRAM_BOT_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
        monkeypatch.setenv("WOLTSPACE_TUI_PORT", "3777")
        layout = _layout(tmp_path)
        channels = Supervisor(layout, probe_token=lambda token: False).channel_supervisor()
        try:
            assert channels.states["tui"].plan.enabled is True
            assert os.environ["TUI_PORT"] == "3777"
        finally:
            monkeypatch.delenv("TUI_PORT", raising=False)


def test_doctor_reports_the_bridge(tmp_path, monkeypatch):
    from woltspace.doctor import run_doctor

    monkeypatch.delenv("WOLTSPACE_TUI_BRIDGE", raising=False)
    checks = {check.name: check for check in run_doctor(_layout(tmp_path), check_port=False)}
    assert "tui-bridge" in checks
    assert checks["tui-bridge"].status in {"pass", "warn"}
    if checks["tui-bridge"].status == "warn":
        assert checks["tui-bridge"].remedy
