"""Tests for trust-dir — the just-in-time Claude workspace trust injector.

Pure unit tests — no server, no tmux, no container needed.
Exercises the trust-dir script logic and the write_trust_config() baseline.

Usage: uv run --with pytest pytest test/test_trust_dir.py -v
"""

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TRUST_DIR_SCRIPT = Path(__file__).resolve().parent.parent / "container" / "bin" / "trust-dir"
ENTRYPOINT_SETUP = Path(__file__).resolve().parent.parent / "container" / "entrypoint_setup.py"


def run_trust_dir(work_dir: str, home: Path) -> subprocess.CompletedProcess:
    """Run the trust-dir script with a fake HOME."""
    return subprocess.run(
        [sys.executable, str(TRUST_DIR_SCRIPT), work_dir],
        env={"HOME": str(home), "PATH": ""},
        capture_output=True, text=True,
    )


def read_claude_json(home: Path) -> dict:
    return json.loads((home / ".claude.json").read_text())


# ---------------------------------------------------------------------------
# trust-dir script tests
# ---------------------------------------------------------------------------

class TestUnitTrustDir:
    """Unit tests for the trust-dir bin script."""

    def test_creates_trust_entry_for_new_dir(self, tmp_path):
        """trust-dir should add a project entry for a wolts directory."""
        home = tmp_path / "home"
        home.mkdir()
        result = run_trust_dir("/workspace/wolts/neowolt", home)
        assert result.returncode == 0
        data = read_claude_json(home)
        assert "/workspace/wolts/neowolt" in data["projects"]
        entry = data["projects"]["/workspace/wolts/neowolt"]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasCompletedProjectOnboarding"] is True

    def test_preserves_existing_claude_state(self, tmp_path):
        """trust-dir should merge into existing .claude.json, not overwrite."""
        home = tmp_path / "home"
        home.mkdir()
        existing = {
            "numStartups": 5,
            "cachedGrowthBookFeatures": {"some_flag": True},
            "projects": {
                "/workspace/wolts/oldwolt": {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                }
            },
        }
        (home / ".claude.json").write_text(json.dumps(existing))

        run_trust_dir("/workspace/wolts/newwolt", home)

        data = read_claude_json(home)
        # New entry added
        assert "/workspace/wolts/newwolt" in data["projects"]
        # Old entry preserved
        assert "/workspace/wolts/oldwolt" in data["projects"]
        # Runtime state preserved
        assert data["numStartups"] == 5
        assert data["cachedGrowthBookFeatures"]["some_flag"] is True

    def test_idempotent(self, tmp_path):
        """Running trust-dir twice for the same dir should be a no-op."""
        home = tmp_path / "home"
        home.mkdir()
        run_trust_dir("/workspace/wolts/mywolt", home)
        first = read_claude_json(home)

        run_trust_dir("/workspace/wolts/mywolt", home)
        second = read_claude_json(home)

        assert first == second

    def test_ignores_non_wolts_dirs(self, tmp_path):
        """trust-dir should be a no-op for directories outside /workspace/wolts/."""
        home = tmp_path / "home"
        home.mkdir()
        result = run_trust_dir("/some/other/path", home)
        assert result.returncode == 0
        assert not (home / ".claude.json").exists()

    def test_trusts_wolts_root(self, tmp_path):
        """trust-dir should work for /workspace/wolts itself."""
        home = tmp_path / "home"
        home.mkdir()
        run_trust_dir("/workspace/wolts", home)
        data = read_claude_json(home)
        assert "/workspace/wolts" in data["projects"]

    def test_trusts_nested_project_dir(self, tmp_path):
        """trust-dir should work for nested dirs like wolt/projects/myapp."""
        home = tmp_path / "home"
        home.mkdir()
        run_trust_dir("/workspace/wolts/neowolt/wolt/projects/dashboard", home)
        data = read_claude_json(home)
        assert "/workspace/wolts/neowolt/wolt/projects/dashboard" in data["projects"]

    def test_no_args_exits_with_error(self, tmp_path):
        """trust-dir with no arguments should exit with code 1."""
        home = tmp_path / "home"
        home.mkdir()
        result = subprocess.run(
            [sys.executable, str(TRUST_DIR_SCRIPT)],
            env={"HOME": str(home), "PATH": ""},
            capture_output=True, text=True,
        )
        assert result.returncode == 1

    def test_handles_empty_claude_json(self, tmp_path):
        """trust-dir should handle an empty/missing .claude.json gracefully."""
        home = tmp_path / "home"
        home.mkdir()
        # No .claude.json exists
        run_trust_dir("/workspace/wolts/fresh", home)
        data = read_claude_json(home)
        assert "/workspace/wolts/fresh" in data["projects"]


# ---------------------------------------------------------------------------
# write_trust_config tests (entrypoint baseline)
# ---------------------------------------------------------------------------

class TestUnitWriteTrustConfig:
    """Tests for the entrypoint write_trust_config() function."""

    def _load_module(self):
        """Import entrypoint_setup as a module."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("entrypoint_setup", ENTRYPOINT_SETUP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_trusts_all_existing_wolt_dirs(self, tmp_path):
        """write_trust_config should pre-trust every wolt directory."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        (wolts_dir / "alpha").mkdir()
        (wolts_dir / "beta").mkdir()
        (wolts_dir / ".state").mkdir()  # hidden dir, should be skipped
        home = tmp_path / "home"
        home.mkdir()

        mod = self._load_module()
        mod.HOME = home
        mod.write_trust_config(wolts_dir)

        data = json.loads((home / ".claude.json").read_text())
        assert str(wolts_dir / "alpha") in data["projects"]
        assert str(wolts_dir / "beta") in data["projects"]
        assert str(wolts_dir / ".state") not in data["projects"]
        assert data["hasCompletedOnboarding"] is True
        assert data["bypassPermissionsAccepted"] is True

    def test_does_not_include_root_wildcard(self, tmp_path):
        """write_trust_config should NOT include '/' — it gets dropped by Claude Code."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        (wolts_dir / "mywolt").mkdir()
        home = tmp_path / "home"
        home.mkdir()

        mod = self._load_module()
        mod.HOME = home
        mod.write_trust_config(wolts_dir)

        data = json.loads((home / ".claude.json").read_text())
        assert "/" not in data["projects"]
