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


def test_per_wolt_home_never_falls_back_to_a_cwd_mismatch(tmp_path, monkeypatch):
    wolts, _ = _setup(tmp_path, monkeypatch)
    _rollout(wolts / "n00b" / ".codex" / "sessions", SID_A, "/somewhere/else", ISO)
    data = {"wolt": "n00b", "dir": str(wolts / "n00b"), "created_at": T0}
    assert harnesses._codex_discover_session_id(data, T0 - 15) is None


def test_spawn_discovery_atomically_claims_a_conversation(tmp_path, monkeypatch):
    """Concurrent spawn pollers may never stamp the same Codex id."""
    import threading
    import sessions

    wolts, _ = _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    registry = sessions.SessionRegistry(wolts)
    for name in ("one", "two"):
        registry.create(
            name, wolt="n00b", harness="codex", dir=str(wolts / "n00b"),
        )

    def harness(_name):
        return {
            "discover_session_id": lambda _data, _since, taken: (
                None if SID_A in taken else SID_A
            ),
        }

    monkeypatch.setattr(sessions, "get_harness", harness)
    gate = threading.Barrier(3)
    results = {}

    def claim(name):
        gate.wait()
        results[name] = sessions.discover_session_id_for(name, timeout=1)

    threads = [threading.Thread(target=claim, args=(name,)) for name in ("one", "two")]
    for thread in threads:
        thread.start()
    gate.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(results.values()) == ["", SID_A]
    claimed = [
        row.get("harness_session_id") for row in registry.list()
        if row.get("harness_session_id")
    ]
    assert claimed == [SID_A]


def test_two_sessions_cannot_claim_the_same_conversation(tmp_path, monkeypatch):
    """claim_resume_id reads the taken set and writes inside one lock, so a second
    session recovering into the same window finds the id already owned."""
    import sessions
    wolts, shared = _setup(tmp_path, monkeypatch)
    _rollout(shared, SID_A, str(wolts / "n00b"), ISO)
    records = {
        "one": {"name": "one", "wolt": "n00b", "harness": "codex", "dir": str(wolts / "n00b"), "created_at": T0},
        "two": {"name": "two", "wolt": "n00b", "harness": "codex", "dir": str(wolts / "n00b"), "created_at": T0 + 5},
    }

    class FakeRegistry:
        wolts_dir = wolts
        def list(self):
            return [dict(r) for r in records.values()]

    def writer(name):
        return lambda d: records[name].update(harness_session_id=d["harness_session_id"])

    reg = FakeRegistry()
    assert sessions.claim_resume_id(reg, dict(records["one"]), writer("one")) == SID_A
    assert sessions.claim_resume_id(reg, dict(records["two"]), writer("two")) == ""
    assert records["one"]["harness_session_id"] == SID_A
    assert "harness_session_id" not in records["two"]
