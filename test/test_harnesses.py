"""Harness table tests — build_command spellings and harness field plumbing.

Usage: uv run --extra test pytest test/test_harnesses.py -v
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

from harnesses import (
    DEFAULT_HARNESS,
    HARNESSES,
    build_command,
    creature_model,
    is_valid_model,
    model_catalog,
    resolve_harness,
    resolve_model,
    tier_default_model,
)


class TestResolveHarness:
    def test_empty_falls_back_to_default(self):
        assert resolve_harness("") == "claude"
        assert resolve_harness(None) == "claude"

    def test_unknown_falls_back_to_default(self):
        assert resolve_harness("winamp") == "claude"

    def test_known_passes_through(self):
        assert resolve_harness("claude") == "claude"


class TestCreatureModel:
    def test_claude_tiers(self):
        assert creature_model("claude", "raccoon") == "opus"
        assert creature_model("claude", "beaver") == "sonnet"
        assert creature_model("claude", "otter") == "haiku"
        assert creature_model("claude", "wolf") == "sonnet"

    def test_no_creature_is_none(self):
        assert creature_model("claude", "") is None
        assert creature_model("claude", None) is None

    def test_unknown_creature_is_none(self):
        assert creature_model("claude", "capybara") is None


class TestBuildCommandClaude:
    """The generated commands must match the historical wclaude invocations."""

    def test_spawn(self):
        cmd = build_command(
            "claude", "spawn",
            session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            session_name="testwolt-chompy-dam-abc123",
            model="opus",
            prompt="hey testwolt",
        )
        assert "wclaude" in cmd
        assert "--dangerously-skip-permissions" in cmd
        assert "--session-id a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "--name testwolt-chompy-dam-abc123" in cmd
        assert "--model opus" in cmd
        assert cmd.endswith("'hey testwolt'")

    def test_resume(self):
        cmd = build_command(
            "claude", "resume",
            resume_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
            model="opus",
            prompt="continue",
        )
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "--model opus" in cmd
        assert "--session-id" not in cmd

    def test_resume_without_id_omits_flag(self):
        cmd = build_command("claude", "resume", model="opus")
        assert "--resume" not in cmd
        assert "--dangerously-skip-permissions" in cmd

    def test_login(self):
        cmd = build_command("claude", "login")
        assert cmd.endswith("wclaude /login")
        assert "--dangerously-skip-permissions" not in cmd

    def test_prompt_is_shell_quoted(self):
        cmd = build_command("claude", "spawn", prompt='say "hi"; rm -rf /')
        assert "'say \"hi\"; rm -rf /'" in cmd

    def test_unknown_mode_raises(self):
        with pytest.raises(ValueError, match="unknown mode"):
            build_command("claude", "teleport")

    def test_unknown_harness_uses_default(self):
        assert build_command("winamp", "login") == build_command("claude", "login")


class TestBuildCommandCodex:
    """Verified against codex-cli 0.144 — see wcodex + harness plan."""

    def test_spawn(self):
        cmd = build_command("codex", "spawn", prompt="hey testwolt")
        assert "wcodex" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd.endswith("'hey testwolt'")
        # codex can't preset a session id — flag must never appear
        cmd_with_id = build_command("codex", "spawn", session_id="abc", prompt="x")
        assert "--session-id" not in cmd_with_id

    def test_spawn_with_model(self):
        cmd = build_command("codex", "spawn", model="gpt-5-codex", prompt="x")
        assert "-m gpt-5-codex" in cmd

    def test_resume_with_id(self):
        cmd = build_command(
            "codex", "resume",
            resume_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890", prompt="continue",
        )
        assert "resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd
        assert "--dangerously-bypass-approvals-and-sandbox" in cmd
        assert cmd.endswith("continue")

    def test_resume_without_id_falls_back_to_fresh(self):
        """No stored id → fresh session, never --last (wrong under concurrency)."""
        cmd = build_command("codex", "resume", prompt="continue")
        assert " resume" not in cmd
        assert "--last" not in cmd

    def test_login_uses_device_auth(self):
        cmd = build_command("codex", "login")
        assert cmd.endswith("login --device-auth")

    def test_codex_tier_models(self):
        """Mapped from the live /model picker (2026-07 lineup)."""
        assert creature_model("codex", "raccoon") == "gpt-5.5"
        assert creature_model("codex", "beaver") == "gpt-5.6-terra"
        assert creature_model("codex", "otter") == "gpt-5.6-luna"


class TestCodexDiscovery:
    """Rollout-id discovery from $CODEX_HOME/sessions."""

    ROLLOUT_UUID = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def _write_rollout(self, wolts_dir, wolt, uuid_, cwd="/somewhere", day="2026/07/14"):
        d = wolts_dir / wolt / ".codex" / "sessions" / day
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"rollout-2026-07-14T12-00-00-{uuid_}.jsonl"
        f.write_text(json.dumps({"cwd": cwd}) + "\n")
        return f

    def test_finds_new_rollout(self, tmp_path, monkeypatch):
        from harnesses import _codex_discover_session_id
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        self._write_rollout(tmp_path, "testwolt", self.ROLLOUT_UUID)
        data = {"wolt": "testwolt", "dir": ""}
        assert _codex_discover_session_id(data, since=0) == self.ROLLOUT_UUID

    def test_ignores_old_rollouts(self, tmp_path, monkeypatch):
        import time as _time
        from harnesses import _codex_discover_session_id
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        self._write_rollout(tmp_path, "testwolt", self.ROLLOUT_UUID)
        data = {"wolt": "testwolt", "dir": ""}
        assert _codex_discover_session_id(data, since=_time.time() + 60) is None

    def test_prefers_cwd_match(self, tmp_path, monkeypatch):
        from harnesses import _codex_discover_session_id
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        other = "b2c3d4e5-f6a7-8901-bcde-f12345678901"
        self._write_rollout(tmp_path, "testwolt", other, cwd="/other/dir")
        self._write_rollout(tmp_path, "testwolt", self.ROLLOUT_UUID, cwd="/right/dir")
        data = {"wolt": "testwolt", "dir": "/right/dir"}
        assert _codex_discover_session_id(data, since=0) == self.ROLLOUT_UUID

    def test_no_sessions_dir(self, tmp_path, monkeypatch):
        from harnesses import _codex_discover_session_id
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        data = {"wolt": "testwolt", "dir": ""}
        assert _codex_discover_session_id(data, since=0) is None


class TestTableShape:
    """Every harness entry must carry the keys the platform relies on."""

    REQUIRED_KEYS = {"wrapper", "command", "process_names", "models",
                     "model_catalog", "skill_invoke", "instructions_file",
                     "auth_file", "preset_session_id", "discover_session_id",
                     "paste_settle", "label", "emoji"}

    def test_all_entries_complete(self):
        for name, entry in HARNESSES.items():
            missing = self.REQUIRED_KEYS - set(entry)
            assert not missing, f"harness '{name}' missing keys: {missing}"

    def test_default_exists(self):
        assert DEFAULT_HARNESS in HARNESSES


class TestHarnessMetadata:
    """The JSON-safe view the pickers consume."""

    def test_metadata_shape(self):
        from harnesses import harness_metadata
        meta = harness_metadata()
        ids = {m["id"] for m in meta}
        assert {"claude", "codex"} <= ids
        for m in meta:
            assert m["label"] and m["emoji"]
            assert set(m["models"]) == {"raccoon", "beaver", "otter"}
            # every entry carries a selectable model catalog of {id,label}
            assert m["catalog"] and all(c["id"] and c["label"] for c in m["catalog"])

    def test_metadata_is_json_safe(self):
        import json
        from harnesses import harness_metadata
        json.dumps(harness_metadata())  # must not raise (no functions/sets)


class TestDefaultHarness:
    """Lodge default read/write against woltspace.json."""

    def test_missing_config_falls_back(self, tmp_path, monkeypatch):
        from harnesses import get_default_harness
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        assert get_default_harness() == "claude"

    def test_set_and_get_roundtrip(self, tmp_path, monkeypatch):
        import json
        from harnesses import get_default_harness, set_default_harness
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps({"telegram": {"x": 1}}))
        set_default_harness("codex")
        assert get_default_harness() == "codex"
        # preserves other keys
        cfg = json.loads((tmp_path / "woltspace.json").read_text())
        assert cfg["telegram"] == {"x": 1}
        assert cfg["harness"]["default"] == "codex"

    def test_set_rejects_unknown(self, tmp_path, monkeypatch):
        import pytest as _pytest
        from harnesses import set_default_harness
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        with _pytest.raises(ValueError):
            set_default_harness("winamp")

    def test_malformed_config_falls_back(self, tmp_path, monkeypatch):
        from harnesses import get_default_harness
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text("{ not json")
        assert get_default_harness() == "claude"


class TestSessionHarnessPlumbing:
    """harness flows: param > wolt.json default > platform default; stored for life."""

    @pytest.fixture(autouse=True)
    def setup_wolt(self, tmp_path, monkeypatch):
        import sessions
        import sites
        import paths

        monkeypatch.setattr(sessions, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(sessions, "RUN_SESSION_SCRIPT", Path("/bin/true"))
        monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
        monkeypatch.setattr(paths, "WOLTS_DIR", tmp_path)

        wolt_dir = tmp_path / "testwolt" / "wolt"
        wolt_dir.mkdir(parents=True)
        site_dir = wolt_dir / "site"
        site_dir.mkdir()
        (site_dir / "index.html").write_text("<h1>test</h1>")
        (wolt_dir / "wolt.json").write_text(json.dumps({
            "name": "testwolt", "type": "raccoon",
        }))
        self.wolts_dir = tmp_path
        self.wolt_json = wolt_dir / "wolt.json"

    def _start(self, **kwargs):
        from sessions import start_session
        with patch("sessions.subprocess.run"), patch("sites.subprocess.Popen") as mock_popen:
            mock_popen.return_value.pid = 12345
            return start_session(wolt="testwolt", prompt="hello", **kwargs)

    def _session_data(self, name):
        from sessions import SessionRegistry
        return SessionRegistry(self.wolts_dir).get(name, check_alive=False)

    def test_default_harness_is_claude(self):
        result = self._start()
        assert result["harness"] == "claude"
        assert self._session_data(result["name"])["harness"] == "claude"

    def test_wolt_json_default_applies(self):
        self.wolt_json.write_text(json.dumps({
            "name": "testwolt", "type": "raccoon", "harness": "claude",
        }))
        result = self._start()
        assert result["harness"] == "claude"

    def test_unknown_harness_falls_back(self):
        result = self._start(harness="winamp")
        assert result["harness"] == "claude"

    def test_lodge_default_applies_when_no_override(self, monkeypatch):
        """A wolt with no harness field follows the woltspace.json lodge default."""
        monkeypatch.setenv("WOLTS_DIR", str(self.wolts_dir))
        (self.wolts_dir / "woltspace.json").write_text(json.dumps({"harness": {"default": "codex"}}))
        # wolt.json has no harness override
        result = self._start()
        assert result["harness"] == "codex"
        assert self._session_data(result["name"])["harness"] == "codex"

    def test_wolt_override_beats_lodge_default(self, monkeypatch):
        """A pinned wolt.json harness wins over the lodge default."""
        monkeypatch.setenv("WOLTS_DIR", str(self.wolts_dir))
        (self.wolts_dir / "woltspace.json").write_text(json.dumps({"harness": {"default": "codex"}}))
        self.wolt_json.write_text(json.dumps({"name": "testwolt", "type": "raccoon", "harness": "claude"}))
        result = self._start()
        assert result["harness"] == "claude"

    def test_codex_spawn_has_no_preset_id(self):
        """codex assigns its own session id — spawn must not stamp one."""
        from sessions import prepare_session_command
        result = self._start(harness="codex")
        assert result["harness"] == "codex"
        cmd = prepare_session_command(result["name"], "spawn", "hello world")
        assert "wcodex" in cmd
        assert "--session-id" not in cmd
        assert not self._session_data(result["name"]).get("harness_session_id")

    def test_codex_discover_stamps_id(self, monkeypatch):
        """discover_session_id_for finds the rollout and stamps the registry."""
        import json as _json
        from sessions import discover_session_id_for
        monkeypatch.setenv("WOLTS_DIR", str(self.wolts_dir))
        result = self._start(harness="codex")
        rollout_uuid = "c3d4e5f6-a7b8-9012-cdef-123456789012"
        d = self.wolts_dir / "testwolt" / ".codex" / "sessions" / "2026" / "07" / "14"
        d.mkdir(parents=True)
        (d / f"rollout-2026-07-14T12-00-00-{rollout_uuid}.jsonl").write_text(
            _json.dumps({"cwd": ""}) + "\n"
        )
        assert discover_session_id_for(result["name"], timeout=5) == rollout_uuid
        assert self._session_data(result["name"])["harness_session_id"] == rollout_uuid

    def test_claude_discover_returns_preset_id(self):
        """For preset-id harnesses discover-id is an immediate no-op."""
        from sessions import discover_session_id_for, prepare_session_command
        result = self._start()
        prepare_session_command(result["name"], "spawn", "hello")
        stamped = self._session_data(result["name"])["harness_session_id"]
        assert discover_session_id_for(result["name"], timeout=5) == stamped

    def test_old_sessions_without_harness_resolve_to_claude(self):
        """Sessions created before the harness field must resume on claude."""
        from sessions import SessionRegistry
        reg = SessionRegistry(self.wolts_dir)
        data = reg.create("testwolt-old-dam-abc123", wolt="testwolt")
        # Simulate a pre-harness session file
        del data["harness"]
        reg._write("testwolt", "testwolt-old-dam-abc123", data)
        reg.update("testwolt-old-dam-abc123", wolt="testwolt",
                   claude_session_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        from sessions import prepare_session_command, resume_session
        with patch("sessions.subprocess.run") as mock_run, \
             patch("sessions._tmux_alive", return_value=False):
            result = resume_session("testwolt-old-dam-abc123", "hello")
        assert result["status"] == "respawned"
        cmd_str = str([c for c in mock_run.call_args_list if "new-session" in str(c)][0])
        assert "--resume" in cmd_str  # wrapper delivered in resume mode
        # The agent-level command falls back to the legacy claude_session_id
        cmd = prepare_session_command("testwolt-old-dam-abc123", "resume", "hello")
        assert "wclaude" in cmd
        assert "--resume a1b2c3d4-e5f6-7890-abcd-ef1234567890" in cmd


class TestModelCatalog:
    """Selectable model list: built-in seed, overridable via woltspace.json."""

    def test_seed_includes_new_models(self):
        claude_ids = {m["id"] for m in model_catalog("claude")}
        assert {"opus", "sonnet", "haiku", "fable"} <= claude_ids
        codex_ids = {m["id"] for m in model_catalog("codex")}
        assert {"gpt-5.5", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"} <= codex_ids

    def test_entries_have_id_and_label(self):
        for c in model_catalog("claude"):
            assert c["id"] and c["label"]

    def test_unknown_harness_uses_default_catalog(self):
        # resolve_harness folds unknown -> claude
        assert model_catalog("winamp") == model_catalog("claude")

    def test_overlay_replaces_catalog_string_ids(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps(
            {"harness": {"models": {"claude": {"catalog": ["opus", "fable"]}}}}))
        ids = [m["id"] for m in model_catalog("claude")]
        assert ids == ["opus", "fable"]  # haiku/sonnet dropped by overlay
        # label still resolved from the seed
        labels = {m["id"]: m["label"] for m in model_catalog("claude")}
        assert labels["opus"] == "Opus 4.8"

    def test_overlay_can_add_new_model_by_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps(
            {"harness": {"models": {"claude": {"catalog": ["opus", "brand-new"]}}}}))
        cat = {m["id"]: m["label"] for m in model_catalog("claude")}
        assert cat["brand-new"] == "brand-new"  # label falls back to id

    def test_overlay_object_entries(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps(
            {"harness": {"models": {"claude": {"catalog": [{"id": "x", "label": "Fancy X"}]}}}}))
        assert model_catalog("claude") == [{"id": "x", "label": "Fancy X"}]

    def test_malformed_config_yields_seed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text("{ not json")
        assert {m["id"] for m in model_catalog("claude")} >= {"opus", "fable"}


class TestTierDefaultModel:
    """Per-tier default: seed unless woltspace.json overrides it."""

    def test_seed_defaults(self):
        assert tier_default_model("claude", "raccoon") == "opus"
        assert tier_default_model("claude", "otter") == "haiku"
        assert tier_default_model("codex", "beaver") == "gpt-5.6-terra"

    def test_no_tier_is_none(self):
        assert tier_default_model("claude", "") is None
        assert tier_default_model("claude", None) is None

    def test_overlay_overrides_tier(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps(
            {"harness": {"models": {"claude": {"tiers": {"otter": "fable"}}}}}))
        assert tier_default_model("claude", "otter") == "fable"
        # untouched tiers keep the seed
        assert tier_default_model("claude", "raccoon") == "opus"

    def test_creature_model_routes_through_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        (tmp_path / "woltspace.json").write_text(json.dumps(
            {"harness": {"models": {"claude": {"tiers": {"raccoon": "fable"}}}}}))
        assert creature_model("claude", "raccoon") == "fable"


class TestIsValidModel:
    def test_seed_membership(self):
        assert is_valid_model("claude", "fable")
        assert is_valid_model("codex", "gpt-5.6-sol")

    def test_cross_harness_is_invalid(self):
        assert not is_valid_model("codex", "opus")
        assert not is_valid_model("claude", "gpt-5.5")

    def test_empty_is_invalid(self):
        assert not is_valid_model("claude", "")
        assert not is_valid_model("claude", None)


class TestResolveModel:
    """Spawn-time resolution: pin wins iff valid for the resolved harness."""

    def test_no_pin_uses_tier_default(self):
        assert resolve_model("claude", "raccoon", None) == "opus"
        assert resolve_model("claude", "raccoon", "") == "opus"

    def test_valid_pin_wins(self):
        assert resolve_model("claude", "raccoon", "fable") == "fable"

    def test_pin_invalid_for_harness_falls_back(self):
        # "opus" is meaningless to codex -> codex raccoon default
        assert resolve_model("codex", "raccoon", "opus") == "gpt-5.5"

    def test_unknown_pin_falls_back(self):
        assert resolve_model("claude", "otter", "nonsense") == "haiku"

    def test_pin_can_diverge_from_tier(self):
        # Free binding: a raccoon may run a non-thinker model
        assert resolve_model("claude", "raccoon", "haiku") == "haiku"
