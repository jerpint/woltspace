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


def _restore_env(snapshot: dict):
    for key, value in snapshot.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_supervisor_prepare_freezes_environment_and_creates_only_state(
    tmp_path, monkeypatch, request
):
    layout = _layout(tmp_path)
    keys = (
        "WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
        "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL",
    )
    # delenv on an absent variable records nothing to undo, so the values
    # prepare() writes would outlive this test and reconfigure every test after
    # it (`WOLTSPACE_ISOLATION=host` silently disables container-mode code).
    snapshot = {key: os.environ.get(key) for key in keys}
    request.addfinalizer(lambda: _restore_env(snapshot))
    for key in keys:
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


def test_entrypoint_supervisor_does_not_override_tunnel_default(tmp_path, monkeypatch, request):
    # prepare() writes os.environ directly; keep the rewrite inside this test.
    # Restore by hand: setenv("") on an absent variable leaves an empty string
    # behind, which is not the same as absent for `os.environ.get(k, default)`.
    keys = ("WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
            "WOLTSPACE_HOST", "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL",
            "PORT")
    snapshot = {key: os.environ.get(key) for key in keys}
    request.addfinalizer(lambda: _restore_env(snapshot))
    monkeypatch.delenv("WOLTSPACE_PUBLIC_TUNNEL", raising=False)
    monkeypatch.setenv("WOLTSPACE_ENTRYPOINT", "1")
    layout = _layout(tmp_path, isolation="external")
    layout.wolts_dir.mkdir(parents=True)  # a container always has the mount
    Supervisor(layout).prepare()
    assert "WOLTSPACE_PUBLIC_TUNNEL" not in os.environ


def test_supervisor_adopts_registry_before_serving(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    events = []
    for key in (
        "WOLTS_DIR", "WOLT_DIR", "WOLTSPACE_DIR", "WOLTSPACE_ISOLATION",
        "WOLTSPACE_INSTANCE_ID", "WOLTSPACE_PUBLIC_TUNNEL", "WOLTSPACE_HOST",
        "PORT",
    ):
        monkeypatch.setenv(key, os.environ.get(key, ""))
    with (
        patch(
            "woltspace.supervisor.adopt_runtime_sessions",
            side_effect=lambda prepared: events.append(("adopt", prepared)),
        ),
        patch("uvicorn.run", side_effect=lambda *a, **kw: events.append(("serve", kw))),
    ):
        Supervisor(layout, instance_id="adoption-order", reload=True).run()

    assert events[0] == ("adopt", layout)
    assert events[1][0] == "serve"
