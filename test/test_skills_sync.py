"""Platform skill sync — the container's boot chore, run natively too.

Usage: uv run pytest test/test_skills_sync.py -v
"""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from skills_sync import seed_wolt_skills, sync_all_wolt_skills  # noqa: E402
from woltspace.layout import RuntimeLayout  # noqa: E402
from woltspace.lifecycle import start  # noqa: E402


def _platform_skill(install_root: Path, name: str, body: str) -> Path:
    skill = install_root / "container" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(body)
    return skill


def _layout(tmp_path, *, port=18779):
    return RuntimeLayout(
        wolts_dir=tmp_path / "wolts",
        install_root=tmp_path / "install",
        host="127.0.0.1",
        port=port,
        isolation="host",
    )


class TestSyncAllWoltSkills:
    def test_replaces_a_stale_platform_skill(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "woltspace-notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)
        (skills / "woltspace-notify" / "SKILL.md").write_text("stale\n")

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"

    def test_leaves_wolt_owned_skills_and_skill_less_wolts_alone(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "woltspace-notify", "fresh\n")
        _platform_skill(install_root, "legacy", "never copied\n")
        wolts_dir = tmp_path / "wolts"

        skills = wolts_dir / "nw" / ".claude" / "skills"
        (skills / "check-usage").mkdir(parents=True)
        (skills / "check-usage" / "SKILL.md").write_text("mine\n")
        (wolts_dir / "nw" / "wolt").mkdir(parents=True)
        bare = wolts_dir / "dogwolt"
        bare.mkdir(parents=True)

        sync_all_wolt_skills(install_root, wolts_dir)

        assert (skills / "check-usage" / "SKILL.md").read_text() == "mine\n"
        assert (skills / "woltspace-notify").is_dir()
        assert not (skills / "legacy").exists()
        assert not (bare / ".claude").exists()

    def test_no_platform_skills_is_a_no_op(self, tmp_path):
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)

        sync_all_wolt_skills(tmp_path / "install", tmp_path / "wolts")

        assert (skills / "woltspace-notify").is_dir()


class TestSeedWoltSkills:
    def test_a_new_wolt_gets_a_skills_dir_the_sync_can_find(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "woltspace-create-wolt", "hello\n")
        wolt_dir = tmp_path / "wolts" / "new"
        wolt_dir.mkdir(parents=True)

        seed_wolt_skills(install_root, wolt_dir)

        seeded = wolt_dir / ".claude" / "skills" / "woltspace-create-wolt"
        assert (seeded / "SKILL.md").read_text() == "hello\n"

    def test_native_creation_seeds_skills_without_credentials(self, tmp_path, monkeypatch):
        from wolts import setup_wolt_claude_config

        install_root = tmp_path / "install"
        _platform_skill(install_root, "woltspace-create-wolt", "hello\n")
        shared = tmp_path / ".claude"
        shared.mkdir()
        (shared / ".credentials.json").write_text('{"token": "host"}')
        wolt_dir = tmp_path / "mywolt"
        wolt_dir.mkdir()
        monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", install_root):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        assert (wolt_dir / ".claude" / "skills" / "woltspace-create-wolt").is_dir()
        assert not (wolt_dir / ".claude" / ".credentials.json").exists()
        assert not (wolt_dir / ".claude" / "settings.json").exists()
        assert not (wolt_dir / ".claude.json").exists()


class TestStartSyncsSkills:
    def test_start_syncs_before_launching_the_control_plane(self, tmp_path):
        layout = _layout(tmp_path)
        stopped = {"state": "stopped", "owner": {}, "health": None}

        with (
            patch("woltspace.lifecycle.inspect_instance", return_value=stopped),
            patch("woltspace.lifecycle.run_doctor", return_value=[]),
            patch("woltspace.lifecycle.doctor_ok", return_value=True),
            patch("woltspace.lifecycle.sync_platform_skills") as sync,
            patch("woltspace.lifecycle.subprocess.Popen") as popen,
            patch("woltspace.lifecycle.read_health", return_value=None),
        ):
            popen.return_value.poll.return_value = 3
            code, result = start(layout, timeout=0.1)

        assert code == 1
        sync.assert_called_once_with(layout)

    def test_a_failed_sync_is_reported_not_raised(self, tmp_path):
        layout = _layout(tmp_path)
        stopped = {"state": "stopped", "owner": {}, "health": None}

        with (
            patch("woltspace.lifecycle.inspect_instance", return_value=stopped),
            patch("woltspace.lifecycle.run_doctor", return_value=[]),
            patch("woltspace.lifecycle.doctor_ok", return_value=True),
            patch(
                "woltspace.lifecycle.sync_platform_skills",
                side_effect=PermissionError("skills unreadable"),
            ),
            patch("woltspace.lifecycle.subprocess.Popen") as popen,
            patch("woltspace.lifecycle.read_health") as read_health,
        ):
            popen.return_value.pid = 4242
            popen.return_value.poll.return_value = None
            read_health.side_effect = lambda endpoint: {
                "instance_id": _captured_instance_id(popen)
            }
            code, result = start(layout, timeout=1.0)

        assert code == 0
        assert result["state"] == "healthy"
        assert result["skills_sync_error"] == "PermissionError: skills unreadable"


def _captured_instance_id(popen) -> str:
    """The instance id `start` just minted, read off the launch command."""
    command = popen.call_args.args[0]
    return command[command.index("--instance-id") + 1]
