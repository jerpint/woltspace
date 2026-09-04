"""Read-only doctor and foreground supervisor preparation."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace.doctor import doctor_ok, run_doctor
from woltspace.layout import RuntimeLayout, installation_root
from woltspace.supervisor import Supervisor


def _layout(tmp_path, *, isolation="host"):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=installation_root(),
        host="127.0.0.1",
        port=18777,
        isolation=isolation,
    )


def test_doctor_is_read_only_and_reports_actionable_missing_tools(tmp_path):
    layout = _layout(tmp_path)
    with patch("woltspace.doctor.shutil.which", return_value=None):
        checks = run_doctor(layout, check_port=False)

    by_name = {check.name: check for check in checks}
    assert by_name["package"].status == "pass"
    assert by_name["tmux"].status == "fail"
    assert "Install tmux" in by_name["tmux"].remedy
    assert by_name["harness"].status == "fail"
    assert "Install at least one" in by_name["harness"].remedy
    assert doctor_ok(checks) is False
    assert not layout.wolts_dir.exists()


def test_doctor_discovers_existing_host_auth_without_copying_it(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    host_home = tmp_path / "home"
    auth = host_home / ".codex" / "auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: host_home))

    def which(name):
        return f"/usr/bin/{name}" if name in {"tmux", "codex"} else None

    with patch("woltspace.doctor.shutil.which", side_effect=which):
        checks = run_doctor(layout, check_port=False)

    by_name = {check.name: check for check in checks}
    assert by_name["host-auth"].status == "pass"
    assert by_name["host-auth"].detail == "codex"
    assert auth.read_text() == "{}"
    assert not layout.wolts_dir.exists()


def test_supervisor_prepare_freezes_environment_and_creates_only_state(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    for key in (
        "WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
        "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL",
    ):
        monkeypatch.delenv(key, raising=False)

    supervisor = Supervisor(layout, instance_id="instance-test")
    supervisor.prepare()

    assert layout.platform_state.is_dir()
    assert layout.logs_dir.is_dir()
    assert os.environ["WOLTS_DIR"] == str(layout.wolts_dir)
    assert os.environ["WOLTSPACE_ISOLATION"] == "host"
    assert os.environ["WOLTSPACE_INSTANCE_ID"] == "instance-test"
    assert os.environ["WOLTSPACE_PUBLIC_TUNNEL"] == "false"
    assert list(layout.wolts_dir.glob("**/*credentials*")) == []
    assert list(layout.wolts_dir.glob("**/auth.json")) == []


def test_external_supervisor_does_not_override_tunnel_default(tmp_path, monkeypatch):
    monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
    Supervisor(_layout(tmp_path, isolation="external")).prepare()
    assert "WOLTSPACE_PUBLIC_TUNNEL" not in os.environ
