"""Wolt discovery & creature-wolt creation tests — pure Python, no server required.

Tests the discovery, type system, and creature-wolt creation logic
in container/lib/wolts.py.

Usage: uv run pytest test/test_wolts.py -v
"""

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add container and lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

ENTRYPOINT_SETUP = Path(__file__).resolve().parent.parent / "container" / "entrypoint_setup.py"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

class TestListWolts:
    """Unit: scanning wolts directories."""

    def test_finds_wolts(self, tmp_path):
        from wolts import list_wolts
        # Create two wolts
        for name, wtype in [("alpha", "rodent"), ("beta", "wolf")]:
            d = tmp_path / name / "wolt"
            d.mkdir(parents=True)
            (d / "wolt.json").write_text(json.dumps({"name": name, "type": wtype, "role": "test"}))

        with patch("wolts.WOLTS_DIR", tmp_path):
            result = list_wolts()
        assert len(result) == 2
        names = {w["name"] for w in result}
        assert names == {"alpha", "beta"}

    def test_defaults_to_rodent(self, tmp_path):
        """Wolts without a type field should default to rodent."""
        from wolts import list_wolts
        d = tmp_path / "old" / "wolt"
        d.mkdir(parents=True)
        (d / "wolt.json").write_text(json.dumps({"name": "old", "role": "legacy"}))

        with patch("wolts.WOLTS_DIR", tmp_path):
            result = list_wolts()
        assert result[0]["type"] == "rodent"

    def test_skips_invalid_json(self, tmp_path):
        from wolts import list_wolts
        d = tmp_path / "broken" / "wolt"
        d.mkdir(parents=True)
        (d / "wolt.json").write_text("not json {{{")

        with patch("wolts.WOLTS_DIR", tmp_path):
            result = list_wolts()
        assert result == []

    def test_empty_dir(self, tmp_path):
        from wolts import list_wolts
        with patch("wolts.WOLTS_DIR", tmp_path):
            result = list_wolts()
        assert result == []


class TestIsRodent:
    """Unit: rodent type checking."""

    def test_rodent_types(self):
        from wolts import is_rodent
        assert is_rodent("raccoon")
        assert is_rodent("beaver")
        assert is_rodent("otter")
        assert is_rodent("rodent")  # legacy

    def test_non_rodent_types(self):
        from wolts import is_rodent
        assert not is_rodent("wolf")
        assert not is_rodent("dog")
        assert not is_rodent("spider")


class TestFindByType:
    """Unit: filtering wolts by creature type."""

    def test_finds_wolves(self, tmp_path):
        from wolts import find_by_type
        for name, wtype in [("a", "rodent"), ("b", "wolf"), ("c", "rodent")]:
            d = tmp_path / name / "wolt"
            d.mkdir(parents=True)
            (d / "wolt.json").write_text(json.dumps({"name": name, "type": wtype}))

        with patch("wolts.WOLTS_DIR", tmp_path):
            wolves = find_by_type("wolf")
        assert len(wolves) == 1
        assert wolves[0]["name"] == "b"

    def test_no_matches(self, tmp_path):
        from wolts import find_by_type
        d = tmp_path / "a" / "wolt"
        d.mkdir(parents=True)
        (d / "wolt.json").write_text(json.dumps({"name": "a", "type": "rodent"}))

        with patch("wolts.WOLTS_DIR", tmp_path):
            assert find_by_type("wolf") == []


# ---------------------------------------------------------------------------
# Active creature tracking
# ---------------------------------------------------------------------------

class TestActiveCreature:
    """Unit: get/set active creature in woltspace.json."""

    def test_get_when_set(self, tmp_path):
        from wolts import get_active_creature
        config = {"creatures": {"active_wolf": "luna"}}
        (tmp_path / "woltspace.json").write_text(json.dumps(config))

        with patch("wolts.CONFIG_FILE", tmp_path / "woltspace.json"):
            assert get_active_creature("wolf") == "luna"

    def test_get_when_not_set(self, tmp_path):
        from wolts import get_active_creature
        config = {"creatures": {"active_wolf": None}}
        (tmp_path / "woltspace.json").write_text(json.dumps(config))

        with patch("wolts.CONFIG_FILE", tmp_path / "woltspace.json"):
            assert get_active_creature("wolf") is None

    def test_get_when_no_config(self, tmp_path):
        from wolts import get_active_creature
        with patch("wolts.CONFIG_FILE", tmp_path / "nope.json"):
            assert get_active_creature("wolf") is None

    def test_get_non_singleton_returns_none(self, tmp_path):
        from wolts import get_active_creature
        assert get_active_creature("rodent") is None

    def test_set_creates_creatures_section(self, tmp_path):
        from wolts import set_active_creature
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({"telegram": {}}))

        with patch("wolts.CONFIG_FILE", config_file):
            set_active_creature("wolf", "fang")

        config = json.loads(config_file.read_text())
        assert config["creatures"]["active_wolf"] == "fang"

    def test_set_preserves_existing(self, tmp_path):
        from wolts import set_active_creature
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({"telegram": {"active_wolt": "nw"}, "creatures": {"active_dog": "rex"}}))

        with patch("wolts.CONFIG_FILE", config_file):
            set_active_creature("wolf", "luna")

        config = json.loads(config_file.read_text())
        assert config["creatures"]["active_wolf"] == "luna"
        assert config["creatures"]["active_dog"] == "rex"
        assert config["telegram"]["active_wolt"] == "nw"


# ---------------------------------------------------------------------------
# Creature-wolt creation
# ---------------------------------------------------------------------------

class TestCreateCreatureWolt:
    """Unit: create_creature_wolt builds correct directory structure."""

    def test_creates_directory_structure(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            result = create_creature_wolt("luna", "wolf", role="Scheduler", description="Pack leader")

        assert result["dir"] == tmp_path / "luna"
        assert result["demoted"] is None
        assert (tmp_path / "luna" / "wolt" / "wolt.json").exists()
        assert (tmp_path / "luna" / "wolt" / "memory" / "identity.md").exists()
        assert (tmp_path / "luna" / "wolt" / "memory" / "context.md").exists()
        assert (tmp_path / "luna" / "wolt" / "memory" / "learnings.md").exists()
        assert (tmp_path / "luna" / "wolt" / "memory" / "archive").is_dir()
        assert (tmp_path / "luna" / ".state").is_dir()

    def test_wolt_json_has_correct_type(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            create_creature_wolt("fang", "wolf", role="Scheduler")

        data = json.loads((tmp_path / "fang" / "wolt" / "wolt.json").read_text())
        assert data["name"] == "fang"
        assert data["type"] == "wolf"
        assert data["role"] == "Scheduler"

    def test_sets_active_creature(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            create_creature_wolt("rex", "dog", role="Lodge companion")

        config = json.loads(config_file.read_text())
        assert config["creatures"]["active_dog"] == "rex"

    def test_demotes_old_singleton(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({"creatures": {"active_wolf": "old"}}))

        # Create existing wolf wolt
        old_dir = tmp_path / "old" / "wolt"
        old_dir.mkdir(parents=True)
        (old_dir / "wolt.json").write_text(json.dumps({"name": "old", "type": "wolf"}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            result = create_creature_wolt("new", "wolf")

        # Old wolf should be demoted to rodent
        old_data = json.loads((tmp_path / "old" / "wolt" / "wolt.json").read_text())
        assert old_data["type"] == "rodent"

        # Return value should report demotion
        assert result["demoted"] == "old"

        # New wolf should be active
        config = json.loads(config_file.read_text())
        assert config["creatures"]["active_wolf"] == "new"

    def test_raises_on_duplicate_name(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        (tmp_path / "taken").mkdir()

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            with pytest.raises(ValueError, match="already exists"):
                create_creature_wolt("taken", "wolf")

    def test_raises_on_invalid_type(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            with pytest.raises(ValueError, match="Invalid creature type"):
                create_creature_wolt("test", "dragon")

    def test_rodent_no_singleton_tracking(self, tmp_path):
        from wolts import create_creature_wolt
        config_file = tmp_path / "woltspace.json"
        config_file.write_text(json.dumps({}))

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.CONFIG_FILE", config_file):
            create_creature_wolt("chip", "rodent", role="Builder")

        data = json.loads((tmp_path / "chip" / "wolt" / "wolt.json").read_text())
        assert data["type"] == "rodent"

        # No creatures tracking for rodents
        config = json.loads(config_file.read_text())
        assert "active_rodent" not in config.get("creatures", {})


# ---------------------------------------------------------------------------
# Credential management
# ---------------------------------------------------------------------------

class TestCredentials:
    """Unit: setup_wolt_claude_config manages credential copies correctly."""

    def test_copies_shared_credentials(self, tmp_path):
        """Fresh wolt gets a copy (not symlink) of shared creds."""
        from wolts import setup_wolt_claude_config
        shared_claude = tmp_path / ".claude"
        shared_claude.mkdir()
        (shared_claude / ".credentials.json").write_text('{"token": "test"}')

        wolt_dir = tmp_path / "mywolt"
        wolt_dir.mkdir()

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", tmp_path / "woltspace"):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        wolt_creds = wolt_dir / ".claude" / ".credentials.json"
        assert wolt_creds.exists()
        assert not wolt_creds.is_symlink()
        assert wolt_creds.read_text() == '{"token": "test"}'

    def test_replaces_legacy_symlink_with_copy(self, tmp_path):
        """Legacy symlink gets replaced with a real file."""
        from wolts import setup_wolt_claude_config
        shared_claude = tmp_path / ".claude"
        shared_claude.mkdir()
        shared_creds = shared_claude / ".credentials.json"
        shared_creds.write_text('{"token": "test"}')

        wolt_dir = tmp_path / "mywolt"
        claude_dir = wolt_dir / ".claude"
        claude_dir.mkdir(parents=True)

        # Legacy symlink
        creds = claude_dir / ".credentials.json"
        creds.symlink_to(shared_creds)
        assert creds.is_symlink()

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", tmp_path / "woltspace"):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        assert not creds.is_symlink()
        assert creds.read_text() == '{"token": "test"}'

    def test_preserves_existing_credentials(self, tmp_path):
        """Wolt with its own credentials file is left alone."""
        from wolts import setup_wolt_claude_config
        shared_claude = tmp_path / ".claude"
        shared_claude.mkdir()
        (shared_claude / ".credentials.json").write_text('{"token": "shared"}')

        wolt_dir = tmp_path / "mywolt"
        claude_dir = wolt_dir / ".claude"
        claude_dir.mkdir(parents=True)
        wolt_creds = claude_dir / ".credentials.json"
        wolt_creds.write_text('{"token": "mine"}')

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", tmp_path / "woltspace"):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        assert wolt_creds.read_text() == '{"token": "mine"}'

    def test_no_shared_creds_no_copy(self, tmp_path):
        """No shared creds file means no credentials created."""
        from wolts import setup_wolt_claude_config
        wolt_dir = tmp_path / "mywolt"
        wolt_dir.mkdir()

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", tmp_path / "woltspace"):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        wolt_creds = wolt_dir / ".claude" / ".credentials.json"
        assert not wolt_creds.exists()
        assert not wolt_creds.is_symlink()


# ---------------------------------------------------------------------------
# Dog identity loading in core.py
# ---------------------------------------------------------------------------

class TestDogIdentity:
    """Unit: bot loads dog identity from dog-wolt."""

    def test_loads_from_dog_wolt(self, tmp_path):
        from bot.core import _load_dog_identity
        # Set up dog-wolt
        dog_dir = tmp_path / "rex" / "wolt" / "memory"
        dog_dir.mkdir(parents=True)
        (dog_dir / "identity.md").write_text("# Rex\n\nI'm Rex. Loyal. Fast.")

        with patch("wolts.WOLTS_DIR", tmp_path), \
             patch("wolts.CONFIG_FILE", tmp_path / "woltspace.json"), \
             patch("bot.core.WOLTS_DIR", tmp_path), \
             patch("bot.core.get_active_creature", return_value="rex"):
            result = _load_dog_identity()
        assert "Rex" in result
        assert "Loyal" in result

    def test_returns_none_when_no_dog(self):
        from bot.core import _load_dog_identity
        with patch("bot.core.get_active_creature", return_value=None):
            assert _load_dog_identity() is None

    def test_returns_none_when_no_identity_file(self, tmp_path):
        from bot.core import _load_dog_identity
        # Dog-wolt exists but no identity.md
        (tmp_path / "rex" / "wolt" / "memory").mkdir(parents=True)

        with patch("bot.core.WOLTS_DIR", tmp_path), \
             patch("bot.core.get_active_creature", return_value="rex"):
            assert _load_dog_identity() is None


# ---------------------------------------------------------------------------
# Session spawning (container/lib/sessions.py)
# ---------------------------------------------------------------------------

class TestSessionNaming:
    """Unit: session name generation."""

    def test_session_name_format(self):
        from sessions import session_name
        name = session_name("neowolt")
        parts = name.split("-")
        assert parts[0] == "neowolt"
        assert len(parts) == 4  # prefix-adj-noun-hex
        assert len(parts[3]) == 6  # 6-char hex

    def test_session_name_uses_prefix(self):
        from sessions import session_name
        name = session_name("UXwolt")
        assert name.startswith("UXwolt-")


class TestStartSession:
    """Unit: start_session validates wolt and spawns correctly."""

    def test_rejects_unknown_wolt(self, tmp_path):
        from sessions import start_session
        with patch("sessions.WOLTS_DIR", tmp_path):
            with pytest.raises(ValueError, match="not found"):
                start_session(wolt="nonexistent", prompt="hey")

    def test_resolves_wolt_dir(self, tmp_path):
        from sessions import start_session
        # Create a valid wolt dir
        (tmp_path / "mywolt").mkdir()
        with patch("sessions.WOLTS_DIR", tmp_path), \
             patch("sessions.subprocess") as mock_sub:
            mock_sub.run.return_value = None
            result = start_session(wolt="mywolt", prompt="hey")
            assert result["wolt"] == "mywolt"
            assert result["name"].startswith("mywolt-")
            # Verify tmux was called with the right working dir
            call_args = mock_sub.run.call_args
            tmux_cmd = call_args[0][0]
            c_idx = tmux_cmd.index("-c")
            assert str(tmp_path / "mywolt") == tmux_cmd[c_idx + 1]

    def test_creature_sets_model(self, tmp_path):
        from sessions import start_session
        (tmp_path / "mywolt").mkdir()
        with patch("sessions.WOLTS_DIR", tmp_path), \
             patch("sessions.subprocess") as mock_sub:
            mock_sub.run.return_value = None
            result = start_session(wolt="mywolt", creature="raccoon")
            assert result["creature"] == "raccoon"
            assert result["model"] == "opus"

    def test_app_creates_subdir(self, tmp_path):
        from sessions import start_session
        (tmp_path / "mywolt").mkdir()
        with patch("sessions.WOLTS_DIR", tmp_path), \
             patch("sessions.subprocess") as mock_sub:
            mock_sub.run.return_value = None
            result = start_session(wolt="mywolt", app="myapp")
            assert result["app"] == "myapp"
            assert (tmp_path / "mywolt" / "wolt" / "apps" / "myapp").is_dir()


# ---------------------------------------------------------------------------
# Skill sync tests (entrypoint_setup.py)
# ---------------------------------------------------------------------------

def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("entrypoint_setup", ENTRYPOINT_SETUP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestSyncAllWoltSkills:
    """Unit: sync_all_wolt_skills copies woltspace-* skills to all wolts."""

    def test_syncs_woltspace_skills_to_all_wolts(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        skills_src = woltspace / "container" / "skills"

        # Create two platform skills and one non-platform
        (skills_src / "woltspace-notify").mkdir(parents=True)
        (skills_src / "woltspace-notify" / "SKILL.md").write_text("notify skill")
        (skills_src / "woltspace-viewport").mkdir()
        (skills_src / "woltspace-viewport" / "SKILL.md").write_text("viewport skill")
        (skills_src / "legacy").mkdir()  # should NOT be copied

        # Create two wolts with .claude/skills/
        wolts = tmp_path / "wolts"
        for name in ["alpha", "beta"]:
            (wolts / name / ".claude" / "skills").mkdir(parents=True)

        mod.sync_all_wolt_skills(woltspace, wolts)

        for name in ["alpha", "beta"]:
            assert (wolts / name / ".claude" / "skills" / "woltspace-notify" / "SKILL.md").exists()
            assert (wolts / name / ".claude" / "skills" / "woltspace-viewport" / "SKILL.md").exists()
            assert not (wolts / name / ".claude" / "skills" / "legacy").exists()

    def test_preserves_wolt_owned_skills(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        (woltspace / "container" / "skills" / "woltspace-notify").mkdir(parents=True)
        (woltspace / "container" / "skills" / "woltspace-notify" / "SKILL.md").write_text("x")

        wolts = tmp_path / "wolts"
        skills_dir = wolts / "alpha" / ".claude" / "skills"
        (skills_dir / "my-custom-skill").mkdir(parents=True)
        (skills_dir / "my-custom-skill" / "SKILL.md").write_text("mine")

        mod.sync_all_wolt_skills(woltspace, wolts)

        # Wolt's own skill is untouched
        assert (skills_dir / "my-custom-skill" / "SKILL.md").read_text() == "mine"
        # Platform skill was synced
        assert (skills_dir / "woltspace-notify" / "SKILL.md").exists()

    def test_replaces_stale_platform_skills(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        (woltspace / "container" / "skills" / "woltspace-notify").mkdir(parents=True)
        (woltspace / "container" / "skills" / "woltspace-notify" / "SKILL.md").write_text("v2")

        wolts = tmp_path / "wolts"
        skills_dir = wolts / "alpha" / ".claude" / "skills"
        (skills_dir / "woltspace-notify").mkdir(parents=True)
        (skills_dir / "woltspace-notify" / "SKILL.md").write_text("v1")

        mod.sync_all_wolt_skills(woltspace, wolts)

        assert (skills_dir / "woltspace-notify" / "SKILL.md").read_text() == "v2"

    def test_skips_wolts_without_skills_dir(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        (woltspace / "container" / "skills" / "woltspace-notify").mkdir(parents=True)
        (woltspace / "container" / "skills" / "woltspace-notify" / "SKILL.md").write_text("x")

        wolts = tmp_path / "wolts"
        (wolts / "no-claude-dir").mkdir(parents=True)  # no .claude/skills/

        # Should not raise
        mod.sync_all_wolt_skills(woltspace, wolts)


class TestDeriveWorktuiSkill:
    """Unit: derive_worktui_skill regenerates the skill from worktui's bundled copy."""

    BUNDLED = "---\nname: worktui\ndescription: Orchestrate sessions.\n---\n\n# worktui\n\nwt spawn / wt send / wt read / wt kill\n"

    def _make_worktui(self, tmp_path):
        worktui = tmp_path / "worktui"
        skill = worktui / "skills" / "worktui"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(self.BUNDLED)
        return worktui

    def test_derives_from_bundled_skill(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        worktui = self._make_worktui(tmp_path)

        mod.derive_worktui_skill(woltspace, worktui)

        derived = (woltspace / "container" / "skills" / "woltspace-worktui" / "SKILL.md").read_text()
        # Frontmatter name rewritten to the platform prefix, rest kept
        assert "name: woltspace-worktui" in derived
        assert "name: worktui\n" not in derived
        assert "description: Orchestrate sessions." in derived
        assert "wt spawn / wt send / wt read / wt kill" in derived
        # Woltspace-specific notes appended
        assert "WORKTUI_DIR=/workspace/wolts/.worktui" in derived

    def test_noop_when_worktui_missing(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"

        mod.derive_worktui_skill(woltspace, tmp_path / "nope")

        assert not (woltspace / "container" / "skills" / "woltspace-worktui").exists()

    def test_replaces_stale_derived_copy(self, tmp_path):
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        stale = woltspace / "container" / "skills" / "woltspace-worktui"
        stale.mkdir(parents=True)
        (stale / "SKILL.md").write_text("old hand-written copy")
        (stale / "extra.md").write_text("leftover")
        worktui = self._make_worktui(tmp_path)

        mod.derive_worktui_skill(woltspace, worktui)

        derived_dir = woltspace / "container" / "skills" / "woltspace-worktui"
        assert "wt spawn" in (derived_dir / "SKILL.md").read_text()
        assert not (derived_dir / "extra.md").exists()

    def test_derived_skill_syncs_to_wolts(self, tmp_path):
        """End-to-end: derive + sync gives wolts worktui's CURRENT skill."""
        mod = _load_entrypoint()
        woltspace = tmp_path / "woltspace"
        worktui = self._make_worktui(tmp_path)
        wolts = tmp_path / "wolts"
        (wolts / "alpha" / ".claude" / "skills").mkdir(parents=True)

        mod.derive_worktui_skill(woltspace, worktui)
        mod.sync_all_wolt_skills(woltspace, wolts)

        synced = (wolts / "alpha" / ".claude" / "skills" / "woltspace-worktui" / "SKILL.md").read_text()
        assert "wt spawn / wt send / wt read / wt kill" in synced
        assert "name: woltspace-worktui" in synced


class TestSyncClaudeMdPlatformSection:
    """Unit: sync_claude_md_platform_section manages the platform block in CLAUDE.md."""

    def test_prepends_to_existing_claude_md(self, tmp_path):
        mod = _load_entrypoint()
        wolts = tmp_path / "wolts"
        wolt = wolts / "alpha"
        wolt.mkdir(parents=True)
        (wolt / "CLAUDE.md").write_text("# Alpha\n\nMy wolt stuff.\n")

        woltspace = tmp_path / "woltspace"
        # Make wolts.py importable
        lib = woltspace / "container" / "lib"
        lib.mkdir(parents=True)
        import shutil
        shutil.copy2(Path(__file__).resolve().parent.parent / "container" / "lib" / "wolts.py", lib / "wolts.py")

        mod.sync_claude_md_platform_section(wolts, woltspace)

        content = (wolt / "CLAUDE.md").read_text()
        assert "<!-- WOLTSPACE:BEGIN" in content
        assert "<!-- WOLTSPACE:END -->" in content
        assert "# Alpha" in content
        assert "My wolt stuff." in content
        # Platform section comes first
        assert content.index("WOLTSPACE:BEGIN") < content.index("# Alpha")

    def test_replaces_existing_platform_section(self, tmp_path):
        mod = _load_entrypoint()
        wolts = tmp_path / "wolts"
        wolt = wolts / "alpha"
        wolt.mkdir(parents=True)
        (wolt / "CLAUDE.md").write_text(
            "<!-- WOLTSPACE:BEGIN — auto-managed, do not edit -->\nOLD STUFF\n<!-- WOLTSPACE:END -->\n\n# Alpha\n"
        )

        woltspace = tmp_path / "woltspace"
        lib = woltspace / "container" / "lib"
        lib.mkdir(parents=True)
        import shutil
        shutil.copy2(Path(__file__).resolve().parent.parent / "container" / "lib" / "wolts.py", lib / "wolts.py")

        mod.sync_claude_md_platform_section(wolts, woltspace)

        content = (wolt / "CLAUDE.md").read_text()
        assert "OLD STUFF" not in content
        assert "DO NOT edit files outside" in content
        assert "# Alpha" in content

    def test_skips_wolts_without_claude_md(self, tmp_path):
        mod = _load_entrypoint()
        wolts = tmp_path / "wolts"
        (wolts / "alpha").mkdir(parents=True)
        # No CLAUDE.md — should not raise

        woltspace = tmp_path / "woltspace"
        lib = woltspace / "container" / "lib"
        lib.mkdir(parents=True)
        import shutil
        shutil.copy2(Path(__file__).resolve().parent.parent / "container" / "lib" / "wolts.py", lib / "wolts.py")

        mod.sync_claude_md_platform_section(wolts, woltspace)
        assert not (wolts / "alpha" / "CLAUDE.md").exists()


class TestSetupWoltSkillsOnly:
    """Unit: setup_wolt_claude_config only copies woltspace-* skills."""

    def test_only_copies_woltspace_prefixed_skills(self, tmp_path):
        from wolts import setup_wolt_claude_config

        woltspace = tmp_path / "woltspace"
        skills_src = woltspace / "container" / "skills"
        (skills_src / "woltspace-notify").mkdir(parents=True)
        (skills_src / "woltspace-notify" / "SKILL.md").write_text("notify")
        (skills_src / "legacy" / "old-skill").mkdir(parents=True)
        (skills_src / "legacy" / "old-skill" / "SKILL.md").write_text("old")

        wolt_dir = tmp_path / "mywolt"
        wolt_dir.mkdir()

        with patch("wolts.WOLTS_DIR", tmp_path), patch("wolts.WOLTSPACE_DIR", woltspace):
            setup_wolt_claude_config(wolt_dir, "mywolt")

        skills_dir = wolt_dir / ".claude" / "skills"
        assert (skills_dir / "woltspace-notify" / "SKILL.md").exists()
        assert not (skills_dir / "legacy").exists()
