"""The retired woltspace claude hooks get swept out of existing wolts.

Usage: uv run pytest test/test_hooks_normalize.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hooks_normalize import (  # noqa: E402
    normalize_all_wolt_hooks,
    normalize_settings_file,
    strip_woltspace_hooks,
)


def _container_hooks() -> dict:
    """The shape wolts.py / entrypoint_setup.py used to bake in."""
    return {
        "Stop": [{"hooks": [{"type": "command", "command": "/workspace/woltspace/container/hooks/session-done.sh"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "/workspace/woltspace/container/hooks/notify.sh"}]}],
    }


def _mac_hooks() -> dict:
    """Bloggo's hand-patched shape — paths point at a mac checkout."""
    return {
        "Stop": [{"hooks": [{"type": "command", "command": "/Users/bloggo/woltspace/container/hooks/session-done.sh"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "/Users/bloggo/woltspace/container/hooks/notify.sh"}]}],
    }


def _write_settings(wolts_dir: Path, name: str, settings: dict) -> Path:
    path = wolts_dir / name / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    return path


class TestStripWoltspaceHooks:
    def test_container_paths_are_stripped_others_preserved(self):
        settings = {
            "skipDangerousModePermissionPrompt": True,
            "permissions": {"allow": ["Bash"]},
            "hooks": _container_hooks(),
        }
        assert strip_woltspace_hooks(settings) is True
        assert "hooks" not in settings
        assert settings["skipDangerousModePermissionPrompt"] is True
        assert settings["permissions"] == {"allow": ["Bash"]}

    def test_mac_paths_are_stripped(self):
        settings = {"skipDangerousModePermissionPrompt": True, "hooks": _mac_hooks()}
        assert strip_woltspace_hooks(settings) is True
        assert "hooks" not in settings

    def test_a_wolts_own_hook_survives(self):
        settings = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "/workspace/woltspace/container/hooks/session-done.sh"}]}],
                "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/node/mine.sh"}]}],
            }
        }
        assert strip_woltspace_hooks(settings) is True
        assert "Stop" not in settings["hooks"]
        assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/home/node/mine.sh"

    def test_own_and_woltspace_hook_in_the_same_event(self):
        settings = {
            "hooks": {
                "Notification": [
                    {"hooks": [
                        {"type": "command", "command": "/anywhere/notify.sh"},
                        {"type": "command", "command": "/home/node/mine.sh"},
                    ]},
                ],
            }
        }
        assert strip_woltspace_hooks(settings) is True
        kept = settings["hooks"]["Notification"][0]["hooks"]
        assert kept == [{"type": "command", "command": "/home/node/mine.sh"}]

    def test_no_woltspace_hooks_reports_no_change(self):
        settings = {
            "hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "/home/node/mine.sh"}]}]}
        }
        assert strip_woltspace_hooks(settings) is False
        assert "PreToolUse" in settings["hooks"]

    def test_no_hooks_key_reports_no_change(self):
        settings = {"skipDangerousModePermissionPrompt": True}
        assert strip_woltspace_hooks(settings) is False


class TestNormalizeSettingsFile:
    def test_container_shape_is_rewritten(self, tmp_path):
        path = _write_settings(tmp_path, "nw", {"skipDangerousModePermissionPrompt": True, "hooks": _container_hooks()})
        normalize_settings_file(path)
        assert json.loads(path.read_text()) == {"skipDangerousModePermissionPrompt": True}

    def test_a_clean_file_is_left_byte_identical(self, tmp_path):
        original = json.dumps({"skipDangerousModePermissionPrompt": True}, indent=2) + "\n"
        path = tmp_path / "settings.json"
        path.write_text(original)
        before = path.stat().st_mtime_ns
        normalize_settings_file(path)
        assert path.read_text() == original
        assert path.stat().st_mtime_ns == before  # not rewritten at all

    def test_missing_file_is_skipped(self, tmp_path):
        normalize_settings_file(tmp_path / "nope" / "settings.json")  # no crash

    def test_malformed_json_is_left_alone(self, tmp_path):
        path = tmp_path / "settings.json"
        path.write_text("{ not json")
        normalize_settings_file(path)
        assert path.read_text() == "{ not json"

    def test_is_idempotent(self, tmp_path):
        path = _write_settings(tmp_path, "nw", {"skipDangerousModePermissionPrompt": True, "hooks": _mac_hooks()})
        normalize_settings_file(path)
        first = path.read_text()
        normalize_settings_file(path)
        assert path.read_text() == first


class TestNormalizeAllWoltHooks:
    def test_sweeps_container_and_mac_wolts_and_spares_the_rest(self, tmp_path):
        wolts = tmp_path / "wolts"
        _write_settings(wolts, "containerwolt", {"skipDangerousModePermissionPrompt": True, "hooks": _container_hooks()})
        _write_settings(wolts, "bloggo", {"skipDangerousModePermissionPrompt": True, "hooks": _mac_hooks()})
        own = _write_settings(wolts, "tinkerer", {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "/home/node/mine.sh"}]}]}})
        own_before = own.read_text()
        # A dotted lodge dir must be ignored.
        _write_settings(wolts, ".state", {"hooks": _container_hooks()})
        dotted_before = (wolts / ".state" / ".claude" / "settings.json").read_text()

        normalize_all_wolt_hooks(wolts)

        assert json.loads((wolts / "containerwolt" / ".claude" / "settings.json").read_text()) == {"skipDangerousModePermissionPrompt": True}
        assert json.loads((wolts / "bloggo" / ".claude" / "settings.json").read_text()) == {"skipDangerousModePermissionPrompt": True}
        assert own.read_text() == own_before
        assert (wolts / ".state" / ".claude" / "settings.json").read_text() == dotted_before

    def test_a_wolt_without_settings_is_skipped(self, tmp_path):
        wolts = tmp_path / "wolts"
        (wolts / "bare").mkdir(parents=True)
        normalize_all_wolt_hooks(wolts)  # no crash

    def test_missing_wolts_dir_is_a_no_op(self, tmp_path):
        normalize_all_wolt_hooks(tmp_path / "absent")  # no crash


class TestStartNormalizesHooks:
    def _layout(self, tmp_path):
        from woltspace.layout import RuntimeLayout

        return RuntimeLayout(
            wolts_dir=tmp_path / "wolts",
            install_root=tmp_path / "install",
            host="127.0.0.1",
            port=18780,
            isolation="host",
        )

    def test_start_normalizes_hooks(self, tmp_path):
        from woltspace.lifecycle import start

        layout = self._layout(tmp_path)
        stopped = {"state": "stopped", "owner": {}, "health": None}
        with (
            patch("woltspace.lifecycle.inspect_instance", return_value=stopped),
            patch("woltspace.lifecycle.run_doctor", return_value=[]),
            patch("woltspace.lifecycle.doctor_ok", return_value=True),
            patch("woltspace.lifecycle.sync_platform_skills"),
            patch("woltspace.lifecycle.normalize_platform_hooks") as normalize,
            patch("woltspace.lifecycle.subprocess.Popen") as popen,
            patch("woltspace.lifecycle.read_health", return_value=None),
        ):
            popen.return_value.poll.return_value = 3
            start(layout, timeout=0.1)

        normalize.assert_called_once_with(layout)

    def test_a_failed_normalize_is_reported_not_raised(self, tmp_path):
        from woltspace.lifecycle import start

        layout = self._layout(tmp_path)
        stopped = {"state": "stopped", "owner": {}, "health": None}
        with (
            patch("woltspace.lifecycle.inspect_instance", return_value=stopped),
            patch("woltspace.lifecycle.run_doctor", return_value=[]),
            patch("woltspace.lifecycle.doctor_ok", return_value=True),
            patch("woltspace.lifecycle.sync_platform_skills"),
            patch("woltspace.lifecycle.normalize_platform_hooks", side_effect=PermissionError("settings locked")),
            patch("woltspace.lifecycle.subprocess.Popen") as popen,
            patch("woltspace.lifecycle.read_health") as read_health,
        ):
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            read_health.side_effect = lambda endpoint: {
                "instance_id": popen.call_args.args[0][popen.call_args.args[0].index("--instance-id") + 1]
            }
            code, result = start(layout, timeout=1.0)

        assert code == 0
        assert result["state"] == "healthy"
        assert result["hooks_normalize_error"] == "PermissionError: settings locked"


class TestNewWoltSettingsHaveNoHooks:
    def test_entrypoint_writes_hookless_settings(self, tmp_path):
        import entrypoint_setup

        with patch.object(entrypoint_setup, "HOME", tmp_path):
            entrypoint_setup.write_settings_json()

        settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
        assert settings == {"skipDangerousModePermissionPrompt": True}
        assert "hooks" not in settings

    def test_native_creation_writes_hookless_settings(self, tmp_path, monkeypatch):
        from wolts import setup_wolt_claude_config

        install_root = tmp_path / "install"
        skill = install_root / "container" / "skills" / "woltspace-create-wolt"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("hi\n")
        (tmp_path / ".claude").mkdir()
        wolt_dir = tmp_path / "mywolt"
        wolt_dir.mkdir()
        monkeypatch.setenv("WOLTSPACE_ISOLATION", "external")

        with (
            patch("wolts.WOLTS_DIR", tmp_path),
            patch("wolts.WOLTSPACE_DIR", install_root),
            patch("wolts.Path.home", return_value=tmp_path),
        ):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        settings = json.loads((wolt_dir / ".claude" / "settings.json").read_text())
        assert settings == {"skipDangerousModePermissionPrompt": True}
