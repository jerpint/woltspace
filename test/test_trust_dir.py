"""Tests for trust-dir — the just-in-time Claude workspace trust injector.

Pure unit tests — no server, no tmux, no container needed.
Exercises the trust-dir script logic and the write_trust_config() baseline.

Usage: uv run --with pytest pytest test/test_trust_dir.py -v
"""

import json
import subprocess
import sys
import tomllib
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


# ---------------------------------------------------------------------------
# ensure_claude_dir_trusted — the shared helper the spawn path calls
# ---------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))


class TestEnsureClaudeDirTrusted:
    """The data root is the boundary; ~/.claude.json is someone else's file."""

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_claude_dir_trusted
        return ensure_claude_dir_trusted(work_dir, wolts_dir)

    def test_trusts_dir_inside_the_data_root(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        data = read_claude_json(claude_trust_home)
        entry = data["projects"][str(work_dir.resolve())]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasCompletedProjectOnboarding"] is True

    def test_preserves_unrelated_claude_state(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        (claude_trust_home / ".claude.json").write_text(json.dumps({
            "numStartups": 7,
            "oauthAccount": {"emailAddress": "someone@example.com"},
            "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}},
        }))

        self._trust(work_dir, wolts_dir)

        data = read_claude_json(claude_trust_home)
        assert data["numStartups"] == 7
        assert data["oauthAccount"]["emailAddress"] == "someone@example.com"
        assert "/elsewhere" in data["projects"]
        assert str(work_dir.resolve()) in data["projects"]

    def test_outside_the_data_root_is_untouched(self, tmp_path, claude_trust_home):
        """The scope guard is the security story — nothing outside gets trusted."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        outsider = tmp_path / "somewhere-else"
        outsider.mkdir()
        config = claude_trust_home / ".claude.json"
        config.write_text(json.dumps({"projects": {}}))
        before = config.read_bytes()

        assert self._trust(outsider, wolts_dir) is False
        assert config.read_bytes() == before

    def test_outside_the_data_root_creates_nothing(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        outsider = tmp_path / "somewhere-else"
        outsider.mkdir()

        assert self._trust(outsider, wolts_dir) is False
        assert not (claude_trust_home / ".claude.json").exists()

    def test_sibling_prefix_is_not_inside(self, tmp_path, claude_trust_home):
        """'/data/wolts-backup' must not read as inside '/data/wolts'."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        lookalike = tmp_path / "wolts-backup"
        lookalike.mkdir()

        assert self._trust(lookalike, wolts_dir) is False
        assert not (claude_trust_home / ".claude.json").exists()

    def test_already_trusted_is_not_rewritten(self, tmp_path, claude_trust_home):
        """~/.claude.json is claude's live state; a redundant write races it."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True
        config = claude_trust_home / ".claude.json"
        before = config.read_bytes()

        assert self._trust(work_dir, wolts_dir) is False
        assert config.read_bytes() == before

    def test_partial_entry_is_completed(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        (claude_trust_home / ".claude.json").write_text(json.dumps({
            "projects": {str(work_dir.resolve()): {"lastCost": 0.5}},
        }))

        assert self._trust(work_dir, wolts_dir) is True

        entry = read_claude_json(claude_trust_home)["projects"][str(work_dir.resolve())]
        assert entry["lastCost"] == 0.5
        assert entry["hasTrustDialogAccepted"] is True

    def test_missing_config_is_created_with_just_the_entry(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert not (claude_trust_home / ".claude.json").exists()
        self._trust(work_dir, wolts_dir)

        data = read_claude_json(claude_trust_home)
        assert list(data) == ["projects"]
        assert list(data["projects"]) == [str(work_dir.resolve())]

    def test_unreadable_config_is_left_alone(self, tmp_path, claude_trust_home):
        """Half-written JSON is not an invitation to truncate the file."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        config = claude_trust_home / ".claude.json"
        config.write_text('{"projects": {"a"')

        assert self._trust(work_dir, wolts_dir) is False
        assert config.read_text() == '{"projects": {"a"'

    def test_nested_app_workdir_is_trusted(self, tmp_path, claude_trust_home):
        """App sessions run in wolt/apps/<name> — still inside the data root."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt" / "wolt" / "apps" / "dashboard"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True
        assert str(work_dir.resolve()) in read_claude_json(claude_trust_home)["projects"]


# ---------------------------------------------------------------------------
# The spawn path — every claude session launch runs through `prepare`
# ---------------------------------------------------------------------------

class TestSpawnPathTrustsWorkdir:

    @pytest.fixture(autouse=True)
    def setup_wolt(self, tmp_path, monkeypatch):
        import sessions
        import sites
        import paths

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
        monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)
        (tmp_path / "testwolt" / "wolt").mkdir(parents=True)
        (tmp_path / "testwolt" / "wolt" / "wolt.json").write_text(json.dumps({
            "name": "testwolt", "type": "raccoon",
        }))
        self.wolts_dir = tmp_path

    def _start(self, **kwargs):
        from sessions import start_session
        return start_session(wolt="testwolt", prompt="hello", **kwargs)

    def test_spawn_trusts_the_session_workdir(self, fake_runtime, claude_trust_home):
        from sessions import prepare_session_command
        result = self._start()
        prepare_session_command(result["name"], "spawn", "hello")

        entry = read_claude_json(claude_trust_home)["projects"][result["workdir"]]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasCompletedProjectOnboarding"] is True

    def test_resume_trusts_too(self, fake_runtime, claude_trust_home):
        """A revived session launches a fresh claude — it needs trust as much."""
        from sessions import prepare_session_command
        result = self._start()
        (claude_trust_home / ".claude.json").unlink(missing_ok=True)

        prepare_session_command(result["name"], "resume", "still there?")

        assert result["workdir"] in read_claude_json(claude_trust_home)["projects"]

    def test_app_session_workdir_is_trusted(self, fake_runtime, claude_trust_home):
        from sessions import prepare_session_command
        result = self._start(app="dashboard")
        prepare_session_command(result["name"], "spawn", "hello")

        assert result["workdir"].endswith("/wolt/apps/dashboard")
        assert result["workdir"] in read_claude_json(claude_trust_home)["projects"]

    def test_codex_sessions_do_not_touch_claudes_config(self, fake_runtime, claude_trust_home):
        from sessions import prepare_session_command
        result = self._start(harness="codex")
        prepare_session_command(result["name"], "spawn", "hello")

        assert not (claude_trust_home / ".claude.json").exists()

    def test_codex_spawn_trusts_the_workdir_in_codex_config(self, fake_runtime, codex_trust_home):
        """Codex asks too — even under --dangerously-bypass-approvals-and-sandbox."""
        from sessions import prepare_session_command
        result = self._start(harness="codex")
        prepare_session_command(result["name"], "spawn", "hello")

        config = tomllib.loads((codex_trust_home / "config.toml").read_text())
        assert config["projects"][result["workdir"]]["trust_level"] == "trusted"

    def test_claude_sessions_do_not_touch_codexs_config(self, fake_runtime, codex_trust_home):
        from sessions import prepare_session_command
        result = self._start()
        prepare_session_command(result["name"], "spawn", "hello")

        assert not (codex_trust_home / "config.toml").exists()

    def test_opencode_sessions_are_left_to_their_own_mechanism(self, fake_runtime, claude_trust_home, codex_trust_home):
        from sessions import prepare_session_command
        result = self._start(harness="opencode")
        prepare_session_command(result["name"], "spawn", "hello")

        assert not (claude_trust_home / ".claude.json").exists()
        assert not (codex_trust_home / "config.toml").exists()


# ---------------------------------------------------------------------------
# ensure_codex_dir_trusted — codex's dialog, same boundary
# ---------------------------------------------------------------------------

class TestEnsureCodexDirTrusted:
    """Append the block codex itself writes, and never outside the data root."""

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_codex_dir_trusted
        return ensure_codex_dir_trusted(work_dir, wolts_dir)

    def _config(self, codex_trust_home):
        return codex_trust_home / "config.toml"

    def test_trusts_dir_inside_the_data_root(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        config = tomllib.loads(self._config(codex_trust_home).read_text())
        assert config["projects"][str(work_dir.resolve())]["trust_level"] == "trusted"

    def test_preserves_the_rest_of_the_config(self, tmp_path, codex_trust_home):
        """It is the user's TOML — comments and all — not a file we own."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        existing = '# hand written\n[shell_environment_policy]\ninherit = "all"\n'
        self._config(codex_trust_home).write_text(existing)

        assert self._trust(work_dir, wolts_dir) is True

        text = self._config(codex_trust_home).read_text()
        assert text.startswith(existing)
        assert tomllib.loads(text)["shell_environment_policy"]["inherit"] == "all"

    def test_already_trusted_is_not_rewritten(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True
        before = self._config(codex_trust_home).read_bytes()

        assert self._trust(work_dir, wolts_dir) is False
        assert self._config(codex_trust_home).read_bytes() == before

    def test_trusted_by_wcodex_is_recognised(self, tmp_path, codex_trust_home):
        """The container wrapper writes the same block; the two must agree."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        block = f'\n[projects."{work_dir.resolve()}"]\ntrust_level = "trusted"\n'
        self._config(codex_trust_home).write_text(block)

        assert self._trust(work_dir, wolts_dir) is False
        assert self._config(codex_trust_home).read_text() == block

    def test_outside_the_data_root_is_untouched(self, tmp_path, codex_trust_home):
        """The scope guard is the security story — nothing outside gets trusted."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        outsider = tmp_path / "somewhere-else"
        outsider.mkdir()
        self._config(codex_trust_home).write_text('model = "gpt-5"\n')
        before = self._config(codex_trust_home).read_bytes()

        assert self._trust(outsider, wolts_dir) is False
        assert self._config(codex_trust_home).read_bytes() == before

    def test_outside_the_data_root_creates_nothing(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        outsider = tmp_path / "somewhere-else"
        outsider.mkdir()

        assert self._trust(outsider, wolts_dir) is False
        assert not self._config(codex_trust_home).exists()

    def test_sibling_prefix_is_not_inside(self, tmp_path, codex_trust_home):
        """'/data/wolts-backup' must not read as inside '/data/wolts'."""
        wolts_dir = tmp_path / "wolts"
        wolts_dir.mkdir()
        lookalike = tmp_path / "wolts-backup"
        lookalike.mkdir()

        assert self._trust(lookalike, wolts_dir) is False
        assert not self._config(codex_trust_home).exists()

    def test_missing_codex_home_is_created(self, tmp_path, monkeypatch):
        """Codex hard-errors on an absent CODEX_HOME; creating it fixes that too."""
        codex_home = tmp_path / "never-existed" / ".codex"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        config = codex_home / "config.toml"
        assert config.read_text() == (
            f'\n[projects."{work_dir.resolve()}"]\ntrust_level = "trusted"\n'
        )
        assert list(tomllib.loads(config.read_text())) == ["projects"]

    def test_unparseable_config_falls_back_to_the_header(self, tmp_path, codex_trust_home):
        """Broken TOML we cannot parse still must not collect duplicate blocks."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        broken = f'not = toml = at = all\n[projects."{work_dir.resolve()}"]\n'
        self._config(codex_trust_home).write_text(broken)

        assert self._trust(work_dir, wolts_dir) is False
        assert self._config(codex_trust_home).read_text() == broken

    def test_unparseable_config_without_the_header_is_appended_to(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        self._config(codex_trust_home).write_text("not = toml = at = all\n")

        assert self._trust(work_dir, wolts_dir) is True
        assert f'[projects."{work_dir.resolve()}"]' in self._config(codex_trust_home).read_text()

    def test_default_codex_home_is_under_the_users_home(self, tmp_path, monkeypatch):
        """No CODEX_HOME exported — the native default — resolves to ~/.codex."""
        from trust import codex_config_path
        monkeypatch.delenv("CODEX_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        assert codex_config_path() == tmp_path / ".codex" / "config.toml"

    def test_nested_app_workdir_is_trusted(self, tmp_path, codex_trust_home):
        """App sessions run in wolt/apps/<name> — still inside the data root."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt" / "wolt" / "apps" / "dashboard"
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        config = tomllib.loads(self._config(codex_trust_home).read_text())
        assert str(work_dir.resolve()) in config["projects"]


# ---------------------------------------------------------------------------
# Fail-open, locking, and TOML surgery — the trust writers must never be the
# reason a session does not launch, and never the reason a config stops parsing
# ---------------------------------------------------------------------------

WEIRD = 'neo"wolt\\den'  # a path segment with both TOML escape characters in it


def _stderr(capsys) -> str:
    return capsys.readouterr().err


class TestClaudeTrustFailsOpen:
    """An unreadable ~/.claude.json costs a dialog, never a session."""

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_claude_dir_trusted
        return ensure_claude_dir_trusted(work_dir, wolts_dir)

    def test_invalid_utf8_is_survived(self, tmp_path, claude_trust_home, capsys):
        """`session-reg prepare` used to die here, and the pane never rendered."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        config = claude_trust_home / ".claude.json"
        raw = b'{"projects": {"\xff\xfe": {}}}'
        config.write_bytes(raw)

        assert self._trust(work_dir, wolts_dir) is False
        assert config.read_bytes() == raw
        assert "not valid UTF-8" in _stderr(capsys)

    def test_malformed_json_warns_and_skips(self, tmp_path, claude_trust_home, capsys):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        (claude_trust_home / ".claude.json").write_text("{oh no")

        assert self._trust(work_dir, wolts_dir) is False
        assert "not valid JSON" in _stderr(capsys)

    def test_json_that_is_not_an_object_warns_and_skips(self, tmp_path, claude_trust_home, capsys):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        (claude_trust_home / ".claude.json").write_text("[1, 2, 3]")

        assert self._trust(work_dir, wolts_dir) is False
        assert "not a JSON object" in _stderr(capsys)

    def test_quotes_and_backslashes_in_the_path(self, tmp_path, claude_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / WEIRD
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        data = read_claude_json(claude_trust_home)
        assert data["projects"][str(work_dir.resolve())]["hasTrustDialogAccepted"] is True


class TestClaudeTrustIsSerialised:
    """Two spawns landing together must both keep their entry."""

    def test_concurrent_spawns_all_survive(self, tmp_path, claude_trust_home, monkeypatch):
        """Last-writer-wins used to drop every entry but one."""
        import threading
        import time
        import trust

        wolts_dir = tmp_path / "wolts"
        dirs = [wolts_dir / f"wolt{i}" for i in range(8)]
        for d in dirs:
            d.mkdir(parents=True)

        # Widen the read-modify-write window so an unserialised version would
        # lose entries every single run rather than one run in a hundred.
        real_write = trust._write_atomically

        def slow_write(path, text):
            time.sleep(0.02)
            real_write(path, text)

        monkeypatch.setattr(trust, "_write_atomically", slow_write)

        barrier = threading.Barrier(len(dirs))
        results = {}

        def worker(d):
            barrier.wait()
            results[d] = trust.ensure_claude_dir_trusted(d, wolts_dir)

        threads = [threading.Thread(target=worker, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(results.values())
        data = read_claude_json(claude_trust_home)  # also asserts it still parses
        for d in dirs:
            assert data["projects"][str(d.resolve())]["hasTrustDialogAccepted"] is True

    def test_concurrent_spawns_preserve_unrelated_state(self, tmp_path, claude_trust_home):
        import threading
        import trust

        wolts_dir = tmp_path / "wolts"
        dirs = [wolts_dir / f"wolt{i}" for i in range(6)]
        for d in dirs:
            d.mkdir(parents=True)
        (claude_trust_home / ".claude.json").write_text(json.dumps({
            "numStartups": 12, "projects": {"/elsewhere": {"hasTrustDialogAccepted": True}},
        }))

        barrier = threading.Barrier(len(dirs))

        def worker(d):
            barrier.wait()
            trust.ensure_claude_dir_trusted(d, wolts_dir)

        threads = [threading.Thread(target=worker, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        data = read_claude_json(claude_trust_home)
        assert data["numStartups"] == 12
        assert "/elsewhere" in data["projects"]
        assert len(data["projects"]) == len(dirs) + 1


class TestCodexTrustDoesNotDuplicateTables:
    """TOML has room for one [projects."x"] header. Two is a config codex dies on."""

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_codex_dir_trusted
        return ensure_codex_dir_trusted(work_dir, wolts_dir)

    def _config(self, codex_trust_home):
        return codex_trust_home / "config.toml"

    def test_existing_untrusted_table_is_flipped_not_duplicated(self, tmp_path, codex_trust_home):
        """The bug: an untrusted entry made the append produce invalid TOML."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())
        self._config(codex_trust_home).write_text(
            f'# mine\nmodel = "gpt-5"\n\n[projects."{key}"]\ntrust_level = "untrusted"\n'
        )

        assert self._trust(work_dir, wolts_dir) is True

        text = self._config(codex_trust_home).read_text()
        assert text.count(f'[projects."{key}"]') == 1
        config = tomllib.loads(text)  # would raise on a duplicate table
        assert config["projects"][key]["trust_level"] == "trusted"
        assert config["model"] == "gpt-5"
        assert "# mine" in text

    def test_flipping_an_untrusted_table_is_idempotent(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())
        self._config(codex_trust_home).write_text(
            f'[projects."{key}"]\ntrust_level = "untrusted"\n'
        )

        assert self._trust(work_dir, wolts_dir) is True
        after_first = self._config(codex_trust_home).read_bytes()

        assert self._trust(work_dir, wolts_dir) is False
        assert self._config(codex_trust_home).read_bytes() == after_first

    def test_table_without_a_trust_level_gains_one(self, tmp_path, codex_trust_home):
        """codex writes other per-project keys; the table can exist untrusted."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())
        self._config(codex_trust_home).write_text(
            f'[projects."{key}"]\nsomething_else = 1\n\n[other]\nx = 2\n'
        )

        assert self._trust(work_dir, wolts_dir) is True

        text = self._config(codex_trust_home).read_text()
        assert text.count(f'[projects."{key}"]') == 1
        config = tomllib.loads(text)
        assert config["projects"][key] == {"trust_level": "trusted", "something_else": 1}
        assert config["other"]["x"] == 2

    def test_a_differently_quoted_header_is_the_same_table(self, tmp_path, codex_trust_home):
        """Literal-string headers name the same path; do not append beside one."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())
        self._config(codex_trust_home).write_text(
            f"[projects.'{key}']\ntrust_level = 'untrusted'\n"
        )

        assert self._trust(work_dir, wolts_dir) is True

        text = self._config(codex_trust_home).read_text()
        assert tomllib.loads(text)["projects"][key]["trust_level"] == "trusted"
        assert text.count("[projects.") == 1

    def test_inline_table_is_left_alone(self, tmp_path, codex_trust_home, capsys):
        """No header line to edit — decline rather than append a rival table."""
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())
        original = f'projects = {{ "{key}" = {{ trust_level = "untrusted" }} }}\n'
        self._config(codex_trust_home).write_text(original)

        assert self._trust(work_dir, wolts_dir) is False
        assert self._config(codex_trust_home).read_text() == original
        assert "cannot edit safely" in _stderr(capsys)

    def test_concurrent_preparations_write_one_table(self, tmp_path, codex_trust_home):
        """Two spawns racing used to stack two identical headers."""
        import threading
        import trust

        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        key = str(work_dir.resolve())

        barrier = threading.Barrier(6)

        def worker():
            barrier.wait()
            trust.ensure_codex_dir_trusted(work_dir, wolts_dir)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = self._config(codex_trust_home).read_text()
        assert text.count("[projects.") == 1
        assert tomllib.loads(text)["projects"][key]["trust_level"] == "trusted"

    def test_concurrent_preparations_for_different_workdirs(self, tmp_path, codex_trust_home):
        import threading
        import trust

        wolts_dir = tmp_path / "wolts"
        dirs = [wolts_dir / f"wolt{i}" for i in range(6)]
        for d in dirs:
            d.mkdir(parents=True)

        barrier = threading.Barrier(len(dirs))

        def worker(d):
            barrier.wait()
            trust.ensure_codex_dir_trusted(d, wolts_dir)

        threads = [threading.Thread(target=worker, args=(d,)) for d in dirs]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        config = tomllib.loads(self._config(codex_trust_home).read_text())
        for d in dirs:
            assert config["projects"][str(d.resolve())]["trust_level"] == "trusted"


class TestCodexTrustEscapesThePath:
    """A `"` or `\\` in a workdir is legal on macOS and lethal to raw interpolation."""

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_codex_dir_trusted
        return ensure_codex_dir_trusted(work_dir, wolts_dir)

    def test_quoted_and_backslashed_path_round_trips(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / WEIRD
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True

        text = (codex_trust_home / "config.toml").read_text()
        config = tomllib.loads(text)  # raw interpolation would not even parse
        assert config["projects"][str(work_dir.resolve())]["trust_level"] == "trusted"

    def test_escaped_path_is_idempotent(self, tmp_path, codex_trust_home):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / WEIRD
        work_dir.mkdir(parents=True)

        assert self._trust(work_dir, wolts_dir) is True
        before = (codex_trust_home / "config.toml").read_bytes()

        assert self._trust(work_dir, wolts_dir) is False
        assert (codex_trust_home / "config.toml").read_bytes() == before


class TestCodexTrustFailsOpen:

    def _trust(self, work_dir, wolts_dir):
        from trust import ensure_codex_dir_trusted
        return ensure_codex_dir_trusted(work_dir, wolts_dir)

    def test_invalid_utf8_config_is_survived(self, tmp_path, codex_trust_home, capsys):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        config = codex_trust_home / "config.toml"
        raw = b'model = "\xff\xfe"\n'
        config.write_bytes(raw)

        assert self._trust(work_dir, wolts_dir) is False
        assert config.read_bytes() == raw
        assert "not valid UTF-8" in _stderr(capsys)

    def test_unparseable_config_naming_the_key_warns(self, tmp_path, codex_trust_home, capsys):
        wolts_dir = tmp_path / "wolts"
        work_dir = wolts_dir / "neowolt"
        work_dir.mkdir(parents=True)
        broken = f'not = toml = at = all\n[projects."{work_dir.resolve()}"]\n'
        (codex_trust_home / "config.toml").write_text(broken)

        assert self._trust(work_dir, wolts_dir) is False
        assert "does not parse as TOML" in _stderr(capsys)
