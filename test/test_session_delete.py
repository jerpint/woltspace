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
        f"{sa.FORMAT}/{p.parent.name}/{p.name}.gz.age" for p in removed)


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
