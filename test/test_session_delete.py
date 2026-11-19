"""Deleting a session on purpose: it lists first, needs --yes, refuses a running
session, removes the engine file and the archive parts, leaves every other
session whole, and writes the bucket keys for the owner's own delete."""

import json
import os
import sys
from pathlib import Path

import pytest
from harnesses import HARNESSES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace import session_archive as sa
from woltspace.cli import main as cli_main

GONE, KEPT = "aaaa1111-2222-3333-4444-555566667777", "bbbb1111-2222-3333-4444-555566667777"


@pytest.fixture
def lodge(tmp_path, monkeypatch):
    root = tmp_path / "wolts"
    (root / "wolt-a" / "wolt").mkdir(parents=True)
    (root / "wolt-a" / "wolt" / "wolt.json").write_text("{}")
    (root / "woltspace.json").write_text(json.dumps({"sessions": {"keep_days": None}}))
    for sid, when in ((GONE, 1_600_000_000), (KEPT, 1_800_000_000)):
        for rel in (f"{sid}.jsonl", f"{sid}/tool-results/r.txt"):
            path = root / "wolt-a" / ".claude" / "projects" / "-w" / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"turn": 0}\n')
            os.utime(path, (when, when))
    sa.refresh(root, HARNESSES)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    return root


def _engine(root):
    return sorted(p.relative_to(root).as_posix() for p in (root / "wolt-a" / ".claude").rglob("*") if p.is_file())


@pytest.mark.parametrize("how", [["--session", GONE], ["--before", "2025-01-01"]])
def test_it_lists_first_and_only_yes_deletes_and_only_that_session(lodge, tmp_path, capsys, how):
    before = _engine(lodge)
    assert cli_main(["backup", "sessions", "delete", *how]) == 0
    assert _engine(lodge) == before and "nothing deleted" in capsys.readouterr().out
    names = [p for p in (lodge / sa.ARCHIVE / sa.FORMAT).rglob("*") if p.is_file()]
    assert cli_main(["backup", "sessions", "delete", *how, "--yes"]) == 0
    assert _engine(lodge) == sorted(p for p in before if GONE not in p)
    restored, refused = sa.restore(lodge / sa.ARCHIVE, tmp_path / "back")
    assert refused == [] and sorted(restored) == [f"root:wolt-a/.claude/projects/-w/{KEPT}.jsonl",
                                                  f"root:wolt-a/.claude/projects/-w/{KEPT}/tool-results/r.txt"]
    listing, = (lodge / sa.ARCHIVE / "deletes").iterdir()
    removed = [p for p in names if not p.exists()]  # 2 sources, one part each: data + metadata
    assert len(removed) == 4 and sorted(listing.read_text().split()) == sorted(  # the names write_out gave them
        f"{sa.FORMAT}/{p.parent.name}/{p.name}.age" for p in removed)


def test_a_running_session_is_refused(lodge):
    from sessions import SessionRegistry
    registry = SessionRegistry(lodge)
    registry.create("wolt-a-live", wolt="wolt-a")
    registry.update("wolt-a-live", wolt="wolt-a", status="running", harness_session_id=GONE)
    before = _engine(lodge)
    assert cli_main(["backup", "sessions", "delete", "--session", GONE, "--yes"]) == 1
    assert _engine(lodge) == before and not (lodge / sa.ARCHIVE / "deletes").exists()


def test_exactly_one_selector_and_a_real_date(lodge):
    assert cli_main(["backup", "sessions", "delete"]) == 2
    assert cli_main(["backup", "sessions", "delete", "--session", GONE, "--before", "2025-01-01"]) == 2
    assert cli_main(["backup", "sessions", "delete", "--before", "last week"]) == 2


CL, CX, PI = "wolt-a/.claude/projects/-w", "wolt-a/.codex/sessions/2026/01/01", "wolt-a/.pi/agent/sessions/--w--"


@pytest.mark.parametrize("path, sid", [
    (f"{CL}/{GONE}.jsonl", GONE),
    (f"{CL}/{GONE}/subagents/agent-1.jsonl", GONE),  # the folder right below <proj>
    (f"{CL}/{GONE}/tool-results/r.txt", GONE),
    (f"{CX}/rollout-2026-01-01T00-00-00-{GONE}.jsonl", GONE),
    (f"{PI}/1700000000_{GONE}.jsonl", GONE),
    (f"wolt-a/.claude/projects/{GONE}/notes.jsonl", None),  # a UUID one level too high is not the id
    (f"{GONE}/.claude/projects/-w/notes.jsonl", None),  # nor an owner folder
    (f"{CL}/x{GONE}.jsonl", None),
    (f"{CX}/{GONE}.jsonl", None),  # codex names only rollout-<time>-<id>
    (f"{CL}/WOLT-A.jsonl", None),
])
def test_a_session_id_comes_from_one_fixed_place_per_engine(path, sid):
    assert sa.session_id(path) == sid


@pytest.mark.parametrize("given", ["wolt-a", GONE[:8], GONE.split("-")[-1], GONE + "0", "WOLT-A"])
def test_only_a_whole_uuid_is_accepted(lodge, capsys, given):
    before = _engine(lodge)
    assert cli_main(["backup", "sessions", "delete", "--session", given, "--yes"]) == 2
    assert _engine(lodge) == before and "sessions:" not in capsys.readouterr().out  # nothing listed
    assert sa.plan_delete(lodge, session=given).sources == []


def test_an_upper_case_id_is_the_same_id(lodge):
    assert cli_main(["backup", "sessions", "delete", "--session", GONE.upper(), "--yes"]) == 0
    assert not any(GONE in p for p in _engine(lodge)) and any(KEPT in p for p in _engine(lodge))


def test_a_subagent_folder_goes_with_its_session_and_a_sibling_stays(lodge):
    for sid in (GONE, KEPT):
        path = lodge / CL / sid / "subagents" / "agent-1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"turn": 0}\n')
    sa.refresh(lodge, HARNESSES)
    plan = sa.plan_delete(lodge, session=GONE)
    assert sorted(plan.sources) == sorted(f"root:{CL}/{rel}" for rel in (
        f"{GONE}.jsonl", f"{GONE}/subagents/agent-1.jsonl", f"{GONE}/tool-results/r.txt"))
    assert cli_main(["backup", "sessions", "delete", "--session", GONE, "--yes"]) == 0
    assert _engine(lodge) == sorted(f"{CL}/{KEPT}{rest}" for rest in (
        ".jsonl", "/subagents/agent-1.jsonl", "/tool-results/r.txt"))


def test_owner_name_is_not_a_session_id(tmp_path):  # the reviewer's round-3 regression
    root = tmp_path / "wolts"
    (root / "wolt-a").mkdir(parents=True)
    (root / "woltspace.json").write_text(json.dumps({"sessions": {"keep_days": None}}))
    for sid in [GONE, KEPT]:
        path = root / CL / (sid + ".jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"turn":1}\n')
    sa.refresh(root, HARNESSES)
    plan = sa.plan_delete(root, session="wolt-a")
    assert plan.sources == [] and plan.engine_files == []
