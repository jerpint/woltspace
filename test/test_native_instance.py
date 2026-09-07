"""Exact native control-plane ownership and safe lifecycle behavior."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace.instance import (
    DataRootLock,
    InstanceConflict,
    InstanceOwner,
    inspect_instance,
    read_owner,
    write_owner,
)
from woltspace.layout import RuntimeLayout, installation_root
from woltspace.lifecycle import start, stop


def _layout(tmp_path, *, port=18778):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=installation_root(),
        host="127.0.0.1",
        port=port,
        isolation="host",
    )


def _owner(layout, *, instance_id="owner-one", endpoint=None):
    return InstanceOwner(
        instance_id=instance_id,
        pid=os.getpid(),
        started_at=123,
        endpoint=endpoint or layout.endpoint,
        isolation="host",
        hostname="test-host",
    )


def test_data_root_lock_is_exclusive_and_owner_metadata_is_private(tmp_path):
    layout = _layout(tmp_path)
    first = DataRootLock(layout, "owner-one")
    first.acquire()
    try:
        owner = read_owner(layout)
        assert owner is not None
        assert owner.instance_id == "owner-one"
        assert (layout.platform_state / "control-plane.json").stat().st_mode & 0o777 == 0o600
        assert (layout.platform_state / "control-plane.lock").stat().st_mode & 0o777 == 0o600

        with pytest.raises(InstanceConflict, match="owner-one"):
            DataRootLock(layout, "owner-two").acquire()
        assert read_owner(layout).instance_id == "owner-one"
    finally:
        first.release()

    assert read_owner(layout) is None


def test_inspection_uses_persisted_owner_endpoint(tmp_path):
    layout = _layout(tmp_path)
    owner = _owner(layout, endpoint="http://127.0.0.1:19999")
    write_owner(layout, owner)
    health = {"ok": True, "instance_id": owner.instance_id, "pid": owner.pid}

    with patch("woltspace.instance.read_health", return_value=health) as read:
        result = inspect_instance(layout)

    read.assert_called_once_with(owner.endpoint)
    assert result["state"] == "healthy"
    assert result["endpoint"] == owner.endpoint


def test_stale_owner_is_reported_without_guessing(tmp_path):
    layout = _layout(tmp_path)
    write_owner(layout, _owner(layout))
    with (
        patch("woltspace.instance.read_health", return_value=None),
        patch("woltspace.instance.pid_alive", return_value=False),
    ):
        result = inspect_instance(layout)
    assert result["state"] == "stale"


@pytest.mark.parametrize("state, detail", [
    ("healthy", "already running"),
    ("starting", "already starting; no second instance launched"),
])
def test_start_is_idempotent_for_owned_instances(tmp_path, state, detail):
    current = {
        "state": state,
        "endpoint": "http://127.0.0.1:19999",
        "wolts_dir": str(tmp_path),
        "owner": {},
        "health": None,
    }
    with (
        patch("woltspace.lifecycle.inspect_instance", return_value=current),
        patch("woltspace.lifecycle.run_doctor") as doctor,
        patch("woltspace.lifecycle.subprocess.Popen") as popen,
    ):
        code, result = start(_layout(tmp_path))
    assert code == 0
    assert result["detail"] == detail
    doctor.assert_not_called()
    popen.assert_not_called()


def test_stop_verifies_owner_endpoint_then_signals_only_that_pid(tmp_path):
    layout = _layout(tmp_path)
    owner = _owner(layout, endpoint="http://127.0.0.1:19999")
    current = {
        "state": "healthy",
        "endpoint": owner.endpoint,
        "wolts_dir": str(layout.wolts_dir),
        "owner": owner.to_record(),
        "health": {"instance_id": owner.instance_id},
    }
    with (
        patch("woltspace.lifecycle.inspect_instance", return_value=current),
        patch("woltspace.lifecycle.read_owner", return_value=owner),
        patch("woltspace.lifecycle.read_health", side_effect=[
            {"instance_id": owner.instance_id}, None,
        ]) as health,
        patch("woltspace.lifecycle.pid_alive", return_value=False),
        patch("woltspace.lifecycle.clear_owner_if_unlocked") as clear_owner,
        patch("woltspace.lifecycle.os.kill") as kill,
    ):
        code, result = stop(layout)

    assert code == 0
    assert result["state"] == "stopped"
    kill.assert_called_once_with(owner.pid, 15)
    clear_owner.assert_called_once_with(layout, owner.instance_id)
    assert health.call_args_list[0].args == (owner.endpoint,)


def test_stop_refuses_mismatched_health_without_signalling(tmp_path):
    layout = _layout(tmp_path)
    owner = _owner(layout)
    current = {
        "state": "healthy",
        "endpoint": owner.endpoint,
        "wolts_dir": str(layout.wolts_dir),
        "owner": owner.to_record(),
        "health": {"instance_id": owner.instance_id},
    }
    with (
        patch("woltspace.lifecycle.inspect_instance", return_value=current),
        patch("woltspace.lifecycle.read_owner", return_value=owner),
        patch("woltspace.lifecycle.read_health", return_value={"instance_id": "other"}),
        patch("woltspace.lifecycle.os.kill") as kill,
    ):
        code, result = stop(layout)

    assert code == 1
    assert "identity changed" in result["error"]
    kill.assert_not_called()
