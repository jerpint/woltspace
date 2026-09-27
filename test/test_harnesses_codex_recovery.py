"""Codex conversation ids: where discovery looks, and how a missed one is recovered."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import harnesses


def _rollout(sessions_dir: Path, sid: str, cwd: str, start: str) -> None:
    day = sessions_dir / "2026" / "09" / "27"
    day.mkdir(parents=True, exist_ok=True)
    meta = {"type": "session_meta", "payload": {"id": sid, "timestamp": start, "cwd": cwd}}
    (day / f"rollout-2026-09-27T12-00-00-{sid}.jsonl").write_text(json.dumps(meta) + "\n")


def _setup(tmp_path, monkeypatch):
    wolts = tmp_path / "wolts"
    host = tmp_path / "host-codex"
    (wolts / "n00b").mkdir(parents=True)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
    monkeypatch.setenv("CODEX_HOME", str(host))
    return wolts, host / "sessions"


SID_A = "01a0e464-dc51-7053-a4b5-0afe1fed6bb9"
SID_B = "01a0e337-d82e-7611-95b5-3ffbb625c456"
T0 = 1790523810.0  # 2026-09-27T15:43:30Z
ISO = "2026-09-27T15:43:30Z"
ISO_LATER = "2026-09-27T15:44:10Z"


def test_native_discovery_finds_the_shared_codex_home(tmp_path, monkeypatch):
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "n00b"), ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_discover_session_id(data, T0 - 15) == SID_A


def test_shared_home_never_hands_out_another_wolts_conversation(tmp_path, monkeypatch):
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "pixie"), ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_discover_session_id(data, T0 - 15) is None
    assert harnesses._codex_recover_session_id(data, set()) is None


def test_recovery_matches_the_session_start(tmp_path, monkeypatch):
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "n00b"), ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_recover_session_id(data, set()) == SID_A
    assert harnesses._codex_recover_session_id(data, {SID_A}) is None  # already owned


def test_recovery_refuses_when_two_conversations_fit(tmp_path, monkeypatch):
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "n00b"), ISO)
    _rollout(shared, SID_B, str(wolts / "n00b"), ISO_LATER)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_recover_session_id(data, set()) is None
    assert harnesses._codex_recover_session_id(data, {SID_B}) == SID_A


def test_recovery_ignores_conversations_outside_the_window(tmp_path, monkeypatch):
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "n00b"), ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0 + 3600}
    assert harnesses._codex_recover_session_id(data, set()) is None


def test_per_wolt_home_still_works(tmp_path, monkeypatch):
    wolts, _ = _setup(tmp_path, monkeypatch)
    _rollout(wolts / "n00b" / ".codex" / "sessions", SID_A, "/somewhere/else", ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_discover_session_id(data, T0 - 15) == SID_A
