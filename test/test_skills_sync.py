"""Platform skill sync — the container's boot chore, run natively too.

Usage: uv run pytest test/test_skills_sync.py -v
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import skills_sync  # noqa: E402
from skills_sync import (  # noqa: E402
    _recover_interrupted_sync,
    ensure_platform_skills,
    seed_wolt_skills,
    sync_all_wolt_skills,
    sync_wolt_skills,
)
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
        _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)
        (skills / "woltspace-notify" / "SKILL.md").write_text("stale\n")

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"

    def test_leaves_wolt_owned_skills_and_skill_less_wolts_alone(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
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
        assert not (bare / ".claude").exists()

    def test_a_bare_source_name_is_delivered_under_the_legacy_prefix(self, tmp_path):
        """Un-ratcheted wolts keep the names their boot prompts already use."""
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"
        assert not (skills / "notify").exists()

    def test_the_plugin_manifest_is_not_a_skill(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        manifest = install_root / "container" / "skills" / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "plugin.json").write_text("{}")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        delivered = sorted(p.name for p in skills.iterdir() if p.is_dir())
        assert delivered == ["woltspace-notify"]

    def test_no_platform_skills_is_a_no_op(self, tmp_path):
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)

        sync_all_wolt_skills(tmp_path / "install", tmp_path / "wolts")

        assert (skills / "woltspace-notify").is_dir()

    def test_a_source_holding_only_the_manifest_deletes_nothing(self, tmp_path):
        """A stale bundle must not strip the colony on its way past."""
        install_root = tmp_path / "install"
        manifest = install_root / "container" / "skills" / ".claude-plugin"
        manifest.mkdir(parents=True)
        (manifest / "plugin.json").write_text("{}")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)
        (skills / "woltspace-notify" / "SKILL.md").write_text("still here\n")
        (skills / "woltspace-wolf").mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "still here\n"
        assert (skills / "woltspace-wolf").is_dir()

    def test_an_empty_skills_folder_deletes_nothing(self, tmp_path):
        install_root = tmp_path / "install"
        (install_root / "container" / "skills").mkdir(parents=True)
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify").is_dir()


class TestSyncIsCrashSafe:
    """An interrupted sync must never leave a wolt without its skills."""

    def test_a_crash_mid_sync_leaves_every_other_skill_intact(self, tmp_path):
        install_root = tmp_path / "install"
        first = _platform_skill(install_root, "notify", "fresh\n")
        _platform_skill(install_root, "wolf", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        for name in ("woltspace-notify", "woltspace-wolf"):
            (skills / name).mkdir(parents=True)
            (skills / name / "SKILL.md").write_text("stale\n")

        real_copytree = skills_sync.shutil.copytree

        def blow_up_on_the_second(src, dst, *args, **kwargs):
            if Path(src).name != first.name:
                raise OSError("interrupted")
            return real_copytree(src, dst, *args, **kwargs)

        with patch.object(skills_sync.shutil, "copytree", blow_up_on_the_second):
            with pytest.raises(OSError):
                sync_all_wolt_skills(install_root, tmp_path / "wolts")

        # The one that made it is new, the one that didn't still has its old copy.
        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"
        assert (skills / "woltspace-wolf" / "SKILL.md").read_text() == "stale\n"

    def test_a_half_copied_staging_dir_is_never_a_skill(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)
        (skills / f".woltspace-notify{skills_sync.STAGE_SUFFIX}").mkdir()

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"
        leftovers = sorted(
            p.name for p in skills.iterdir() if p.name != skills_sync.LOCK_NAME
        )
        assert leftovers == ["woltspace-notify"]

    def test_a_retired_skill_is_restored_when_its_swap_never_landed(self, tmp_path):
        skills = tmp_path / "skills"
        retired = skills / f".woltspace-wolf{skills_sync.RETIRED_SUFFIX}"
        retired.mkdir(parents=True)
        (retired / "SKILL.md").write_text("the only copy left\n")

        _recover_interrupted_sync(skills)

        assert (skills / "woltspace-wolf" / "SKILL.md").read_text() == "the only copy left\n"
        assert not retired.exists()

    def test_a_retired_skill_is_dropped_when_the_swap_did_land(self, tmp_path):
        skills = tmp_path / "skills"
        (skills / "woltspace-wolf").mkdir(parents=True)
        (skills / "woltspace-wolf" / "SKILL.md").write_text("live\n")
        retired = skills / f".woltspace-wolf{skills_sync.RETIRED_SUFFIX}"
        retired.mkdir()
        (retired / "SKILL.md").write_text("old\n")

        _recover_interrupted_sync(skills)

        assert (skills / "woltspace-wolf" / "SKILL.md").read_text() == "live\n"
        assert not retired.exists()

    def test_a_wolts_own_dotdir_wearing_our_suffix_survives(self, tmp_path):
        """`.notes.wsync-new` is the wolt's. Our leftovers are `.woltspace-*`."""
        skills = tmp_path / "skills"
        skills.mkdir()
        mine = skills / f".notes{skills_sync.STAGE_SUFFIX}"
        mine.mkdir()
        (mine / "keep.md").write_text("mine\n")
        also_mine = skills / f".notes{skills_sync.RETIRED_SUFFIX}"
        also_mine.mkdir()
        (also_mine / "keep.md").write_text("also mine\n")

        _recover_interrupted_sync(skills)

        assert (mine / "keep.md").read_text() == "mine\n"
        assert (also_mine / "keep.md").read_text() == "also mine\n"
        # ...and nothing was resurrected under a name we invented.
        assert not (skills / "notes").exists()

    def test_a_skill_this_install_no_longer_ships_is_removed(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "skills"
        (skills / "woltspace-retired").mkdir(parents=True)
        (skills / "check-usage").mkdir()

        sync_wolt_skills([source], skills)

        assert (skills / "woltspace-notify").is_dir()
        assert not (skills / "woltspace-retired").exists()
        assert (skills / "check-usage").is_dir()  # not ours to touch


class TestSyncIsSerialised:
    """Two syncs share every staging name. Only one may be in the directory."""

    def test_a_second_sync_stands_down_while_one_holds_the_lock(self, tmp_path, capsys):
        import fcntl
        import os

        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "skills"
        skills.mkdir()

        fd = os.open(str(skills / skills_sync.LOCK_NAME), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            sync_wolt_skills([source], skills)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

        # It did not stage half a copy over the holder's work.
        assert not (skills / "woltspace-notify").exists()
        assert not (skills / f".woltspace-notify{skills_sync.STAGE_SUFFIX}").exists()
        assert "another sync" in capsys.readouterr().err

        # And once the holder lets go, the next sync does the job.
        sync_wolt_skills([source], skills)
        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"

    def test_the_lock_is_released_and_reusable(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "skills"

        sync_wolt_skills([source], skills)
        sync_wolt_skills([source], skills)  # would hang or skip on a leaked lock

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"

    def test_the_lockfile_is_never_mistaken_for_a_skill(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "skills"

        sync_wolt_skills([source], skills)
        _recover_interrupted_sync(skills)
        sync_wolt_skills([source], skills)

        assert (skills / skills_sync.LOCK_NAME).is_file()
        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"


class TestSeedWoltSkills:
    def test_a_new_wolt_gets_a_skills_dir_the_sync_can_find(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "create-wolt", "hello\n")
        wolt_dir = tmp_path / "wolts" / "new"
        wolt_dir.mkdir(parents=True)

        seed_wolt_skills(install_root, wolt_dir)

        seeded = wolt_dir / ".claude" / "skills" / "woltspace-create-wolt"
        assert (seeded / "SKILL.md").read_text() == "hello\n"

    def test_a_source_with_nothing_to_give_leaves_the_wolt_unseeded(self, tmp_path):
        install_root = tmp_path / "install"
        (install_root / "container" / "skills" / ".claude-plugin").mkdir(parents=True)
        wolt_dir = tmp_path / "wolts" / "new"
        wolt_dir.mkdir(parents=True)

        seed_wolt_skills(install_root, wolt_dir)

        assert not (wolt_dir / ".claude").exists()

    def test_native_creation_seeds_skills_without_credentials(self, tmp_path, monkeypatch):
        from wolts import setup_wolt_claude_config

        install_root = tmp_path / "install"
        _platform_skill(install_root, "create-wolt", "hello\n")
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


# ---------------------------------------------------------------------------
# The shipped tree — what the plugin actually hands out
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SHIPPED_SKILLS = ROOT / "container" / "skills"


class TestShippedManifest:
    """Claude reads a plugin skill's name off its DIRECTORY, codex reads it off
    the FRONTMATTER. The two only converge on `woltspace:<base>` while every
    skill's two names agree — so that agreement is a test, not a habit."""

    def _skill_dirs(self):
        return sorted(d for d in SHIPPED_SKILLS.iterdir()
                      if d.is_dir() and not d.name.startswith("."))

    def test_every_skill_has_a_skill_md(self):
        for d in self._skill_dirs():
            assert (d / "SKILL.md").is_file(), f"{d.name} has no SKILL.md"

    def test_frontmatter_name_matches_the_directory(self):
        for d in self._skill_dirs():
            head = (d / "SKILL.md").read_text().split("---")[1]
            names = [line.split(":", 1)[1].strip()
                     for line in head.splitlines() if line.startswith("name:")]
            assert names == [d.name], f"{d.name}: frontmatter name {names}"

    def test_no_skill_still_wears_the_legacy_prefix(self):
        for d in self._skill_dirs():
            assert not d.name.startswith(skills_sync.LEGACY_PREFIX)

    def test_retired_skills_live_outside_the_plugin_root(self):
        """Codex recurses into subdirectories — anything left under the plugin
        root is a live skill, retired or not."""
        assert not (SHIPPED_SKILLS / "legacy").exists()
        assert (ROOT / "container" / "legacy-skills").is_dir()

    def test_the_plugin_manifest_points_at_the_plugin_root(self):
        plugin_dir = SHIPPED_SKILLS / ".claude-plugin"
        marketplace = json.loads((plugin_dir / "marketplace.json").read_text())
        plugin = json.loads((plugin_dir / "plugin.json").read_text())

        assert marketplace["name"] == skills_sync.PLUGIN_MARKETPLACE
        entries = [p for p in marketplace["plugins"]
                   if p["name"] == skills_sync.PLUGIN_NAME]
        assert len(entries) == 1
        assert entries[0]["source"] == "./"
        assert plugin["name"] == skills_sync.PLUGIN_NAME
        assert plugin["skills"] == "./"


# ---------------------------------------------------------------------------
# Plugin delivery
# ---------------------------------------------------------------------------

def _plugin_wolt(tmp_path, name="nw", harness=None):
    """A wolt opted into plugin delivery, with a skills dir the sync can see."""
    wolt = tmp_path / "wolts" / name
    (wolt / ".claude" / "skills").mkdir(parents=True)
    (wolt / "wolt").mkdir(parents=True)
    config = {"name": name, "type": "raccoon", "skills_delivery": "plugin"}
    if harness:
        config["harness"] = harness
    (wolt / "wolt" / "wolt.json").write_text(json.dumps(config))
    return wolt


class TestEnsurePlatformSkills:
    def test_the_symlink_is_the_whole_delivery(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)

        ensure_platform_skills(wolt, "codex", source)

        link = wolt / ".claude" / "skills" / "woltspace"
        assert link.is_symlink()
        assert link.resolve() == source.resolve()

    def test_it_is_idempotent(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)

        ensure_platform_skills(wolt, "codex", source)
        ensure_platform_skills(wolt, "codex", source)

        link = wolt / ".claude" / "skills" / "woltspace"
        assert link.is_symlink()
        assert link.resolve() == source.resolve()

    def test_a_link_pointing_somewhere_else_is_repointed(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        stale = tmp_path / "old-install"
        stale.mkdir()
        wolt = _plugin_wolt(tmp_path)
        (wolt / ".claude" / "skills" / "woltspace").symlink_to(stale)

        ensure_platform_skills(wolt, "codex", source)

        assert (wolt / ".claude" / "skills" / "woltspace").resolve() == source.resolve()

    def test_a_real_directory_on_the_name_is_never_deleted(self, tmp_path):
        """Wolt-owned skills live in the same directory. An rmtree here is not
        recoverable; a warning is."""
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)
        theirs = wolt / ".claude" / "skills" / "woltspace"
        theirs.mkdir()
        (theirs / "SKILL.md").write_text("mine\n")

        ensure_platform_skills(wolt, "codex", source)

        assert (theirs / "SKILL.md").read_text() == "mine\n"

    def test_other_entries_are_never_touched(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)
        mine = wolt / ".claude" / "skills" / "check-usage"
        mine.mkdir()
        (mine / "SKILL.md").write_text("mine\n")

        ensure_platform_skills(wolt, "codex", source)

        assert (mine / "SKILL.md").read_text() == "mine\n"

    def test_stale_copies_go_but_only_the_ones_the_old_sync_owned(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        (source / "notify").mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)
        skills = wolt / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir()
        (skills / "woltspace-notes").mkdir()   # the wolt's own, prefix or not
        (skills / "check-usage").mkdir()

        ensure_platform_skills(wolt, "codex", source)

        assert not (skills / "woltspace-notify").exists()
        assert (skills / "woltspace-notes").is_dir()
        assert (skills / "check-usage").is_dir()

    def test_a_codex_wolt_gets_no_claude_settings(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path)

        with patch.object(skills_sync, "_install_plugin") as install:
            ensure_platform_skills(wolt, "codex", source)

        assert not (wolt / ".claude" / "settings.json").exists()
        install.assert_not_called()


class TestPluginSettings:
    def test_the_merge_preserves_unrelated_keys(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        settings = wolt / ".claude" / "settings.json"
        settings.write_text(json.dumps({
            "skipDangerousModePermissionPrompt": True,
            "extraKnownMarketplaces": {"theirs": {"source": "keep me"}},
            "enabledPlugins": {"theirs@theirs": True},
        }))

        with patch.object(skills_sync, "_install_plugin"):
            ensure_platform_skills(wolt, "claude", source)

        written = json.loads(settings.read_text())
        assert written["skipDangerousModePermissionPrompt"] is True
        assert written["extraKnownMarketplaces"]["theirs"] == {"source": "keep me"}
        assert written["enabledPlugins"]["theirs@theirs"] is True
        assert written["enabledPlugins"]["woltspace@woltspace"] is True
        assert written["extraKnownMarketplaces"]["woltspace"]["source"] == {
            "source": "directory", "path": str(source),
        }

    def test_an_unparseable_settings_file_is_left_alone(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        settings = wolt / ".claude" / "settings.json"
        settings.write_text("{not json,}")

        with patch.object(skills_sync, "_install_plugin") as install:
            ensure_platform_skills(wolt, "claude", source)

        assert settings.read_text() == "{not json,}"
        # The link still lands — only the claude-specific half stood down.
        assert (wolt / ".claude" / "skills" / "woltspace").is_symlink()
        install.assert_called_once()

    def test_the_install_runs_once_under_the_wolts_own_home(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs.get("env", {}).get("HOME")))
            if cmd[2] == "install":
                installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text(json.dumps(
                    {"version": 2, "plugins": {"woltspace@woltspace": [{}]}}))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(skills_sync.subprocess, "run", fake_run):
            ensure_platform_skills(wolt, "claude", source)
            ensure_platform_skills(wolt, "claude", source)

        # marketplace add THEN install, once — the second pass sees it installed
        assert [c[0][2:] for c in calls] == [
            ["marketplace", "add", str(source)],
            ["install", "woltspace@woltspace"],
        ]
        assert {c[1] for c in calls} == {str(wolt)}

    def test_a_failed_install_warns_instead_of_crashing(self, tmp_path, capsys):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")

        with patch.object(skills_sync.subprocess, "run",
                          side_effect=FileNotFoundError("no claude here")):
            assert ensure_platform_skills(wolt, "claude", source) is False

        assert "no claude here" in capsys.readouterr().err
        assert (wolt / ".claude" / "skills" / "woltspace").is_symlink()

    def test_a_flat_installed_key_also_counts_as_installed(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({"woltspace@woltspace": {"version": "1"}}))

        with patch.object(skills_sync.subprocess, "run") as run:
            ensure_platform_skills(wolt, "claude", source)

        run.assert_not_called()


class TestDeliveryDispatch:
    """`sync_all_wolt_skills` keeps its signature and routes per wolt."""

    def test_an_opted_in_wolt_gets_the_link_and_loses_its_copies(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        wolt = _plugin_wolt(tmp_path, harness="codex")
        (wolt / ".claude" / "skills" / "woltspace-notify").mkdir()

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        skills = wolt / ".claude" / "skills"
        assert skills.joinpath("woltspace").is_symlink()
        assert not (skills / "woltspace-notify").exists()

    def test_everyone_else_still_gets_copies(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        wolt = tmp_path / "wolts" / "old"
        (wolt / ".claude" / "skills").mkdir(parents=True)
        (wolt / "wolt").mkdir(parents=True)
        (wolt / "wolt" / "wolt.json").write_text(json.dumps({"name": "old"}))

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        skills = wolt / ".claude" / "skills"
        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "fresh\n"
        assert not (skills / "woltspace").exists()

    def test_a_wolt_with_no_wolt_json_is_on_the_old_path(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify").is_dir()
        assert not (skills / "woltspace").exists()

    def test_the_lodge_default_decides_the_harness_when_a_wolt_has_no_pin(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "fresh\n")
        wolts_dir = tmp_path / "wolts"
        _plugin_wolt(tmp_path)
        (wolts_dir / "woltspace.json").write_text(
            json.dumps({"harness": {"default": "codex"}}))

        with patch.object(skills_sync, "_install_plugin") as install:
            sync_all_wolt_skills(install_root, wolts_dir)

        install.assert_not_called()   # codex needs no plugin install


# ---------------------------------------------------------------------------
# Review findings — each of these failed before the fix it guards
# ---------------------------------------------------------------------------

class TestTheCopyPathKeepsItsContract:
    """A copy-path wolt's skills must be named `woltspace-<name>` BOTH ways:
    claude reads the directory, codex reads the frontmatter."""

    def test_the_staged_copy_gets_its_frontmatter_renamed_too(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify",
                        "---\nname: notify\ndescription: ping\n---\n\nbody\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        delivered = (skills / "woltspace-notify" / "SKILL.md").read_text()
        assert "name: woltspace-notify" in delivered
        assert "name: notify\n" not in delivered
        assert "description: ping" in delivered   # nothing else disturbed
        assert "body" in delivered

    def test_the_source_frontmatter_is_never_touched(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "---\nname: notify\n---\nb\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (source / "SKILL.md").read_text() == "---\nname: notify\n---\nb\n"

    def test_a_skill_md_without_frontmatter_survives_intact(self, tmp_path):
        install_root = tmp_path / "install"
        _platform_skill(install_root, "notify", "no frontmatter here\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        skills.mkdir(parents=True)

        sync_all_wolt_skills(install_root, tmp_path / "wolts")

        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == \
            "no frontmatter here\n"


class TestACrashBetweenTheTwoRenamesIsRecoverable:
    """The retired name has to be the one recovery looks for, or an interrupted
    swap loses the skill outright: the old copy is stranded under a name the
    recovery pass never scans, and the staged replacement is deleted as an
    assumed-incomplete copy."""

    def test_the_old_copy_is_restored_after_a_crash_mid_swap(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)
        (skills / "woltspace-notify" / "SKILL.md").write_text("old but real\n")

        real_replace = skills_sync.os.replace
        calls = {"n": 0}

        def die_after_retiring(src, dst):
            calls["n"] += 1
            real_replace(src, dst)
            if calls["n"] == 1:          # the live -> retired rename landed
                raise KeyboardInterrupt("killed mid-swap")

        with patch.object(skills_sync.os, "replace", die_after_retiring):
            with pytest.raises(KeyboardInterrupt):
                sync_wolt_skills([source], skills)

        # The skill is gone from its real name — exactly the crash window.
        assert not (skills / "woltspace-notify").exists()

        # The next sync must find it and put it back before doing anything else.
        _recover_interrupted_sync(skills)
        assert (skills / "woltspace-notify" / "SKILL.md").read_text() == "old but real\n"

    def test_the_retired_name_is_the_delivered_name(self, tmp_path):
        install_root = tmp_path / "install"
        source = _platform_skill(install_root, "notify", "fresh\n")
        skills = tmp_path / "wolts" / "nw" / ".claude" / "skills"
        (skills / "woltspace-notify").mkdir(parents=True)

        seen = []
        real_replace = skills_sync.os.replace

        def watch(src, dst):
            seen.append(Path(dst).name)
            return real_replace(src, dst)

        with patch.object(skills_sync.os, "replace", watch):
            sync_wolt_skills([source], skills)

        retired = [n for n in seen if n.endswith(skills_sync.RETIRED_SUFFIX)]
        assert retired == [f".woltspace-notify{skills_sync.RETIRED_SUFFIX}"]
        # ...which is exactly what the recovery pass scans for.
        assert all(n.startswith(".woltspace-") for n in retired)


class TestPluginInstallSequence:
    """A fresh HOME needs `marketplace add` before `install`: settings.json
    declares a marketplace for a future session, it does not register one for
    the CLI. Reproduced live 2026-09-06 — install alone exits 1 with
    'Plugin "woltspace" not found in marketplace "woltspace"'."""

    def test_the_marketplace_is_added_before_the_install(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        order = []

        def fake_run(cmd, **kwargs):
            order.append(cmd[2])
            if cmd[2] == "install":
                # ...and it only succeeds because the add came first
                assert "marketplace" in order
                installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
                installed.parent.mkdir(parents=True, exist_ok=True)
                installed.write_text(json.dumps(
                    {"version": 2, "plugins": {"woltspace@woltspace": [{}]}}))
            return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        with patch.object(skills_sync.subprocess, "run", fake_run):
            assert ensure_platform_skills(wolt, "claude", source) is True

        assert order == ["marketplace", "install"]

    def test_a_failed_marketplace_add_never_reaches_the_install(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        order = []

        def fake_run(cmd, **kwargs):
            order.append(cmd[2])
            return type("R", (), {"returncode": 1, "stdout": "",
                                  "stderr": "no such directory"})()

        with patch.object(skills_sync.subprocess, "run", fake_run):
            assert ensure_platform_skills(wolt, "claude", source) is False

        assert order == ["marketplace"]

    def test_an_unconfirmed_delivery_keeps_the_copy_synced_skills(self, tmp_path):
        """The blocker: install fails, the failure is only a warning, and the
        sweep runs anyway — leaving the wolt with no platform skills at all."""
        source = tmp_path / "install" / "container" / "skills"
        (source / "notify").mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        copies = wolt / ".claude" / "skills" / "woltspace-notify"
        copies.mkdir()
        (copies / "SKILL.md").write_text("the only copy this wolt has\n")

        def fails(cmd, **kwargs):
            return type("R", (), {
                "returncode": 1, "stdout": "",
                "stderr": 'Plugin "woltspace" not found in marketplace "woltspace".',
            })()

        with patch.object(skills_sync.subprocess, "run", fails):
            assert ensure_platform_skills(wolt, "claude", source) is False

        assert (copies / "SKILL.md").read_text() == "the only copy this wolt has\n"

    def test_a_confirmed_delivery_does_sweep(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        (source / "notify").mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        (wolt / ".claude" / "skills" / "woltspace-notify").mkdir()
        installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps(
            {"version": 2, "plugins": {"woltspace@woltspace": [{}]}}))

        with patch.object(skills_sync.subprocess, "run") as run:
            assert ensure_platform_skills(wolt, "claude", source) is True

        run.assert_not_called()   # already installed
        assert not (wolt / ".claude" / "skills" / "woltspace-notify").exists()

    def test_the_live_installed_plugins_schema_is_understood(self, tmp_path):
        """Verified against claude's actual file, 2026-09-06."""
        wolt = _plugin_wolt(tmp_path)
        installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({
            "version": 2,
            "plugins": {"woltspace@woltspace": [
                {"scope": "user", "version": "1079dcf80016"},
            ]},
        }))
        assert skills_sync._plugin_installed(wolt) is True

    def test_an_absent_or_unrelated_file_reads_as_not_installed(self, tmp_path):
        wolt = _plugin_wolt(tmp_path)
        assert skills_sync._plugin_installed(wolt) is False
        installed = wolt / ".claude" / "plugins" / "installed_plugins.json"
        installed.parent.mkdir(parents=True)
        installed.write_text(json.dumps({"version": 2, "plugins": {"other@x": []}}))
        assert skills_sync._plugin_installed(wolt) is False


class TestTheAgentsBridge:
    """codex reads $HOME/.agents/skills. The wcodex wrapper lays that bridge in
    container mode and exits early in host mode, so native codex wolts had
    nothing."""

    def test_delivery_lays_the_bridge(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="codex")

        ensure_platform_skills(wolt, "codex", source)

        link = wolt / ".agents" / "skills"
        assert link.is_symlink()
        # Same shape wcodex writes — relative, so the wolt dir stays movable.
        assert os.readlink(link) == "../.claude/skills"
        assert link.resolve() == (wolt / ".claude" / "skills").resolve()

    def test_the_bridge_reaches_the_platform_tree(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        (source / "notify").mkdir(parents=True)
        (source / "notify" / "SKILL.md").write_text("hi\n")
        wolt = _plugin_wolt(tmp_path, harness="codex")

        ensure_platform_skills(wolt, "codex", source)

        reached = wolt / ".agents" / "skills" / "woltspace" / "notify" / "SKILL.md"
        assert reached.read_text() == "hi\n"

    def test_it_is_idempotent(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="codex")

        ensure_platform_skills(wolt, "codex", source)
        ensure_platform_skills(wolt, "codex", source)

        assert os.readlink(wolt / ".agents" / "skills") == "../.claude/skills"

    def test_a_real_directory_there_is_never_deleted(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="codex")
        theirs = wolt / ".agents" / "skills"
        theirs.mkdir(parents=True)
        (theirs / "keep.md").write_text("mine\n")

        ensure_platform_skills(wolt, "codex", source)

        assert (theirs / "keep.md").read_text() == "mine\n"


class TestSettingsAreReallyPreserved:
    def test_a_non_object_at_one_of_our_keys_blocks_the_write(self, tmp_path, capsys):
        """The docstring promises unrelated keys survive. Overwriting a list at
        enabledPlugins would destroy exactly what we promised to keep."""
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        settings = wolt / ".claude" / "settings.json"
        original = json.dumps({"enabledPlugins": ["theirs@theirs"], "keep": 1})
        settings.write_text(original)

        with patch.object(skills_sync, "_install_plugin", return_value=True):
            ensure_platform_skills(wolt, "claude", source)

        assert settings.read_text() == original
        assert "enabledPlugins" in capsys.readouterr().err

    def test_a_nested_non_object_also_blocks_the_write(self, tmp_path):
        source = tmp_path / "install" / "container" / "skills"
        source.mkdir(parents=True)
        wolt = _plugin_wolt(tmp_path, harness="claude")
        settings = wolt / ".claude" / "settings.json"
        original = json.dumps({"extraKnownMarketplaces": {"woltspace": "a string"}})
        settings.write_text(original)

        with patch.object(skills_sync, "_install_plugin", return_value=True):
            ensure_platform_skills(wolt, "claude", source)

        assert settings.read_text() == original


class TestWoltSkillsDelivery:
    def test_the_opt_in_is_explicit_and_everything_else_is_the_copy_path(self, tmp_path):
        from skills_sync import wolt_skills_delivery

        plugin = _plugin_wolt(tmp_path, name="ratcheted")
        assert wolt_skills_delivery(plugin) == "plugin"

        old = tmp_path / "wolts" / "old"
        (old / "wolt").mkdir(parents=True)
        (old / "wolt" / "wolt.json").write_text(json.dumps({"name": "old"}))
        assert wolt_skills_delivery(old) == "copy"

        broken = tmp_path / "wolts" / "broken"
        (broken / "wolt").mkdir(parents=True)
        (broken / "wolt" / "wolt.json").write_text("{not json")
        assert wolt_skills_delivery(broken) == "copy"

        assert wolt_skills_delivery(tmp_path / "wolts" / "nothing-here") == "copy"
