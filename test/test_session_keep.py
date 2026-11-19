"""woltspace.json sessions.keep_days: claude's cleanupPeriodDays in every wolt, merged,
never over a hand-set value, never 0; natively nothing is written."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import session_keep

from woltspace.doctor import run_doctor
from woltspace.layout import RuntimeLayout, installation_root
from woltspace.supervisor import Supervisor


def _lodge(tmp_path, sessions=None, wolts=("wolt-a", "pal")):
    root = tmp_path / "wolts"
    root.mkdir()
    (root / "woltspace.json").write_text(json.dumps({} if sessions is None else {"sessions": sessions}))
    for name in wolts:
        (root / name / "wolt").mkdir(parents=True)
        (root / name / "wolt" / "wolt.json").write_text("{}")
    return root


def _settings(root, wolt):
    path = root / wolt / ".claude" / "settings.json"
    return json.loads(path.read_text()) if path.exists() else None


def _layout(root, isolation):
    return RuntimeLayout(wolts_dir=root, install_root=installation_root(), host="127.0.0.1",
                         port=18777, isolation=isolation)


@pytest.mark.parametrize("value, days", [(None, 3650), (7, 7), (0, None), (-1, None), ("30", None),
                                         (True, None), (2.5, None)])
def test_null_is_forever_and_zero_or_junk_is_never_asked_for(tmp_path, value, days):
    assert session_keep.keep_days(_lodge(tmp_path, {"keep_days": value})) == days


def test_unset_changes_nothing(tmp_path):
    root = _lodge(tmp_path, {"default_execution_policy": "bypass"})
    assert session_keep.apply_to_wolts(root) == {} and _settings(root, "pal") is None
    assert session_keep.setting(root).startswith("unset")


def test_every_wolt_gets_it_merged_and_a_hand_set_value_is_never_overwritten(tmp_path):
    root = _lodge(tmp_path, {"keep_days": None}, wolts=("wolt-a", "pal", "nunu"))
    (root / "pal" / ".claude").mkdir()
    (root / "pal" / ".claude" / "settings.json").write_text(json.dumps({"skipDangerousModePermissionPrompt": True}))
    (root / "nunu" / ".claude").mkdir()
    (root / "nunu" / ".claude" / "settings.json").write_text(json.dumps({"cleanupPeriodDays": 5}))
    (root / "loose").mkdir()  # not a wolt
    assert session_keep.apply_to_wolts(root) == {"nunu": "kept", "pal": "set", "wolt-a": "set"}
    assert _settings(root, "pal") == {"skipDangerousModePermissionPrompt": True, "cleanupPeriodDays": 3650}
    assert _settings(root, "nunu") == {"cleanupPeriodDays": 5}
    assert _settings(root, "wolt-a") == {"cleanupPeriodDays": 3650}
    assert not (root / "loose" / ".claude").exists()
    assert set(session_keep.apply_to_wolts(root).values()) == {"kept"}  # the next boot writes nothing


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_an_unreadable_or_non_object_settings_file_is_left_alone(tmp_path, text):
    root = _lodge(tmp_path, {"keep_days": 30}, wolts=("pal",))
    path = root / "pal" / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text(text)
    assert session_keep.apply_to_wolts(root) == {"pal": "refused"} and path.read_text() == text


def test_zero_is_never_written(tmp_path):
    root = _lodge(tmp_path, {"keep_days": 0}, wolts=("pal",))
    assert session_keep.apply_to_wolts(root) == {} and _settings(root, "pal") is None
    assert session_keep.setting(root) == "invalid: 0"


@pytest.mark.parametrize("isolation", ["external", "host"])
def test_a_new_wolt_has_it_from_birth_in_a_container_and_natively_nothing(tmp_path, monkeypatch, isolation):
    from wolts import setup_wolt_claude_config
    root = _lodge(tmp_path, {"keep_days": 90}, wolts=())
    install = tmp_path / "install"
    (install / "container" / "skills" / "create-wolt").mkdir(parents=True)
    (install / "container" / "skills" / "create-wolt" / "SKILL.md").write_text("hi\n")
    (root / "wolt-a").mkdir()
    monkeypatch.setenv("WOLTSPACE_ISOLATION", isolation)
    with patch("wolts.WOLTS_DIR", root), patch("wolts.WOLTSPACE_DIR", install), \
            patch("wolts.Path.home", return_value=tmp_path):
        setup_wolt_claude_config(root / "wolt-a", "wolt-a")
    assert _settings(root, "wolt-a") == (
        {"skipDangerousModePermissionPrompt": True, "cleanupPeriodDays": 90} if isolation == "external" else None)


def test_the_entrypoint_home_settings_get_it_too(tmp_path):
    from woltspace import container_entrypoint
    root = _lodge(tmp_path, {"keep_days": None}, wolts=())
    with patch.object(container_entrypoint, "HOME", tmp_path / "home"):
        container_entrypoint.write_settings_json(root)
    assert json.loads((tmp_path / "home" / ".claude" / "settings.json").read_text()) == {
        "skipDangerousModePermissionPrompt": True, "cleanupPeriodDays": 3650}


@pytest.mark.parametrize("isolation", ["external", "host"])
def test_the_container_boot_sweep_applies_it_and_a_native_one_never_does(tmp_path, isolation):
    root = _lodge(tmp_path, {"keep_days": 14}, wolts=("pal",))
    with patch("woltspace.skills.sync_platform_skills"), patch("woltspace.hooks.normalize_platform_hooks"):
        Supervisor(_layout(root, isolation))._platform_preflight()
    assert _settings(root, "pal") == ({"cleanupPeriodDays": 14} if isolation == "external" else None)


@pytest.mark.parametrize("isolation, sessions, status, remedy", [
    ("host", {"keep_days": None}, "pass", 'Add "cleanupPeriodDays": 3650 to ~/.claude/settings.json'),
    ("external", {"keep_days": None}, "pass", ""),
    ("host", {}, "pass", ""),
    ("host", {"keep_days": "x"}, "warn", ""),
])
def test_doctor_shows_the_setting_and_natively_the_line_to_add(tmp_path, isolation, sessions, status, remedy):
    root = _lodge(tmp_path, sessions, wolts=())
    check = {c.name: c for c in run_doctor(_layout(root, isolation), check_port=False)}["session-keep"]
    assert check.status == status and check.detail.startswith("sessions.keep_days: ")
    assert check.remedy.startswith(remedy) and bool(check.remedy) == bool(remedy)
    assert (root / "woltspace.json").read_text() == json.dumps({"sessions": sessions})  # read only


def test_the_merge_writes_under_the_settings_lock(tmp_path, monkeypatch):  # review 5b
    from contextlib import contextmanager
    held, writes = [], []

    @contextmanager
    def lock(path):
        held.append(path)
        yield
        held.pop()
    monkeypatch.setattr(session_keep, "settings_lock", lock)
    monkeypatch.setattr(session_keep, "write_json_atomic", lambda path, data: writes.append(list(held)))
    assert session_keep.merge_claude_settings(tmp_path / ".claude", 30) == "set"
    assert writes == [[tmp_path / ".claude" / "settings.json"]]
