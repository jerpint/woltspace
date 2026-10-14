"""A broken woltspace.json is never overwritten by the lodge's own writers."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import state  # noqa: E402


@pytest.fixture
def wolts(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "WOLTS_DIR", tmp_path)
    return tmp_path


def test_a_truncated_config_is_not_rewritten_on_first_run(wolts, monkeypatch, capsys):
    broken = '{"access": {"team_domain": "team.cloudflareaccess.com", "lodge_aud": "a'
    (wolts / "woltspace.json").write_text(broken)
    monkeypatch.setenv("WOLTSPACE_DEFAULT_HARNESS", "claude")
    installs = []

    assert state.apply_default_harness_from_env(installer=lambda **kw: installs.append(kw)) is False

    assert (wolts / "woltspace.json").read_text() == broken
    assert installs == []  # the starter seed is not fetched again
    assert "not overwriting" in capsys.readouterr().err


def test_selecting_a_harness_refuses_to_overwrite_a_broken_config(wolts):
    (wolts / "woltspace.json").write_text("[1, 2]")
    with pytest.raises(state.LodgeConfigUnreadable):
        state.select_onboarding_harness("claude")
    assert (wolts / "woltspace.json").read_text() == "[1, 2]"


def test_a_missing_or_valid_config_is_written_as_before(wolts):
    state.select_onboarding_harness("claude")
    saved = json.loads((wolts / "woltspace.json").read_text())
    assert saved["harness"]["default"] == "claude"
    saved["access"] = {"kept": True}
    (wolts / "woltspace.json").write_text(json.dumps(saved))
    state.select_onboarding_harness("codex")
    again = json.loads((wolts / "woltspace.json").read_text())
    assert again["harness"]["default"] == "codex" and again["access"] == {"kept": True}
