"""Wolt discovery & creature-wolt creation tests — pure Python, no server required.

Tests the discovery, type system, and creature-wolt creation logic
in container/lib/wolts.py.

Usage: uv run pytest test/test_wolts.py -v
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add container and lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


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
# Wolf-wolt discovery in wolf.py
# ---------------------------------------------------------------------------

class TestWolfWoltDiscovery:
    """Unit: wolf.py finds the correct wolt directory."""

    def test_finds_wolf_via_active_config(self, tmp_path):
        from creatures.wolf import _find_wolf_wolt
        # Set up woltspace.json
        config = {"creatures": {"active_wolf": "luna"}}
        (tmp_path / "woltspace.json").write_text(json.dumps(config))

        # Set up wolf-wolt with wolf.json
        wolf_dir = tmp_path / "luna" / "wolt"
        wolf_dir.mkdir(parents=True)
        (wolf_dir / "wolf.json").write_text(json.dumps({"crons": []}))

        with patch.dict(os.environ, {"WOLTS_DIR": str(tmp_path)}):
            result = _find_wolf_wolt()
        assert result == tmp_path / "luna"

    def test_finds_wolf_via_type_scan(self, tmp_path):
        from creatures.wolf import _find_wolf_wolt
        # No active_wolf in config
        (tmp_path / "woltspace.json").write_text(json.dumps({}))

        # But there's a wolt with type=wolf
        wolf_dir = tmp_path / "fang" / "wolt"
        wolf_dir.mkdir(parents=True)
        (wolf_dir / "wolt.json").write_text(json.dumps({"name": "fang", "type": "wolf"}))

        with patch.dict(os.environ, {"WOLTS_DIR": str(tmp_path)}):
            result = _find_wolf_wolt()
        assert result == tmp_path / "fang"

    def test_returns_none_when_no_wolf(self, tmp_path):
        from creatures.wolf import _find_wolf_wolt
        (tmp_path / "woltspace.json").write_text(json.dumps({}))

        with patch.dict(os.environ, {"WOLTS_DIR": str(tmp_path)}):
            result = _find_wolf_wolt()
        assert result is None

    def test_fallback_to_wolt_dir(self, tmp_path):
        """get_wolt_dir falls back to WOLT_DIR when no wolf-wolt exists."""
        from creatures.wolf import get_wolt_dir
        (tmp_path / "woltspace.json").write_text(json.dumps({}))
        fallback = tmp_path / "neowolt"

        with patch.dict(os.environ, {"WOLTS_DIR": str(tmp_path), "WOLT_DIR": str(fallback)}):
            result = get_wolt_dir()
        assert result == fallback


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

    def test_project_creates_subdir(self, tmp_path):
        from sessions import start_session
        (tmp_path / "mywolt").mkdir()
        with patch("sessions.WOLTS_DIR", tmp_path), \
             patch("sessions.subprocess") as mock_sub:
            mock_sub.run.return_value = None
            result = start_session(wolt="mywolt", project="myapp")
            assert result["project"] == "myapp"
            assert (tmp_path / "mywolt" / "wolt" / "projects" / "myapp").is_dir()
