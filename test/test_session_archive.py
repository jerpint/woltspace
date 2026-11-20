"""The session archive: new parts only, content names, a hash chain that restore
checks, natively only the lodge's own sessions, and off unless the lodge asks."""

import gzip
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path

import pytest
from harnesses import HARNESSES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace import session_archive as sa
from woltspace.backup import create_backup


def _lodge(tmp_path, keep=True):
    root = tmp_path / "wolts"
    (root / "wolt-a" / "wolt").mkdir(parents=True)
    (root / "wolt-a" / "wolt" / "wolt.json").write_text("{}")
    (root / "woltspace.json").write_text(json.dumps({"sessions": {"archive": True}} if keep else {}))
    return root


def _session(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode() if isinstance(text, str) else text)
    return path


def _names(root):
    return sorted(p.relative_to(root / sa.ARCHIVE).as_posix()
                  for p in (root / sa.ARCHIVE / sa.FORMAT).rglob("*") if p.is_file())


CLAUDE = "wolt-a/.claude/projects/-w/s1.jsonl"


def test_a_second_run_writes_nothing_and_a_rebuilt_archive_gets_the_same_names(tmp_path):
    root = _lodge(tmp_path)
    _session(root, CLAUDE, '{"a": 1}\n{"b": 2}\n')
    _session(root, "wolt-a/.codex/sessions/2026/r.jsonl", '{"c": 3}\n')
    assert len(sa.refresh(root, HARNESSES).stored) == 2
    names = _names(root)
    assert sa.refresh(root, HARNESSES).stored == []
    shutil.rmtree(root / sa.ARCHIVE)  # a lost archive: the same content makes the same names
    assert len(sa.refresh(root, HARNESSES).stored) == 2 and _names(root) == names


def test_only_complete_lines_are_stored_and_the_digest_names_those_bytes(tmp_path):
    root = _lodge(tmp_path)
    _session(root, CLAUDE, '{"a": 1}\n{"half": ')  # caught mid-write
    sa.refresh(root, HARNESSES)
    meta_path, = (root / sa.ARCHIVE / sa.FORMAT).rglob("*.json")
    stored = meta_path.with_suffix(".gz").read_bytes()
    assert stored[:2] == b"\x1f\x8b"  # kept compressed
    meta, part = json.loads(meta_path.read_text()), gzip.decompress(stored)
    assert part == b'{"a": 1}\n' and meta["sha256"] == hashlib.sha256(part).hexdigest()
    assert meta_path.stem.endswith(meta["sha256"]) and meta["path"] == CLAUDE


def test_ten_appends_are_ten_parts_and_restore_joins_them_byte_equal(tmp_path):
    root = _lodge(tmp_path)
    path = _session(root, CLAUDE, "")
    for i in range(10):
        with open(path, "a") as handle:
            handle.write(json.dumps({"turn": i}) + "\n")
        os.utime(path, ns=(i * 10**9, (1_700_000_000 + i) * 10**9))
        sa.refresh(root, HARNESSES)
    metas = [json.loads(p.read_text()) for p in (root / sa.ARCHIVE / sa.FORMAT).rglob("*.json")]
    assert len(metas) == 10 and sum(m["length"] for m in metas) == path.stat().st_size
    assert sorted(m["offset"] for m in metas)[1] > 0  # appended, not whole copies
    _, refused = sa.restore(root / sa.ARCHIVE, tmp_path / "back")
    out = tmp_path / "back" / "root" / CLAUDE
    assert refused == [] and out.read_bytes() == path.read_bytes()
    assert out.stat().st_mtime_ns == path.stat().st_mtime_ns


def test_a_rewritten_file_is_a_whole_new_version_and_the_old_one_stays(tmp_path):
    root = _lodge(tmp_path)
    path = _session(root, CLAUDE, '{"a": 1}\n{"b": 2}\n')
    sa.refresh(root, HARNESSES)
    _session(root, CLAUDE, '{"compacted": true}\n')
    os.utime(path, (1, 1))
    sa.refresh(root, HARNESSES)
    metas = sorted((json.loads(p.read_text()) for p in (root / sa.ARCHIVE / sa.FORMAT).rglob("*.json")),
                   key=lambda m: m["captured_at"])
    assert [m["offset"] for m in metas] == [0, 0] and metas[1]["continues"] is None
    restored, _ = sa.restore(root / sa.ARCHIVE, tmp_path / "back")
    back = tmp_path / "back" / "root" / CLAUDE
    assert back.read_bytes() == path.read_bytes()
    older = tmp_path / "back" / "versions" / "root" / f"{CLAUDE}.v1"
    assert older.read_text() == '{"a": 1}\n{"b": 2}\n'  # nothing kept is hidden
    assert restored == [f"root:{CLAUDE}", f"versions/root:{CLAUDE}.v1"]


@pytest.mark.parametrize("damage", ["drop_middle", "corrupt"])
def test_a_missing_or_damaged_part_stops_that_session_never_a_shorter_file(tmp_path, damage):
    root = _lodge(tmp_path)
    path = _session(root, CLAUDE, "")
    for i in range(3):
        with open(path, "a") as handle:
            handle.write(f'{{"turn": {i}}}\n')
        os.utime(path, (i, i))
        sa.refresh(root, HARNESSES)
    _session(root, "wolt-a/.claude/projects/-w/other.jsonl", '{"x": 1}\n')
    sa.refresh(root, HARNESSES)
    folder = root / sa.ARCHIVE / sa.FORMAT / hashlib.sha256(f"root:{CLAUDE}".encode()).hexdigest()
    middle = sorted(json.loads(p.read_text())["captured_at"] + "|" + p.stem for p in folder.glob("*.json"))[1]
    middle = folder / f"{middle.split('|')[1]}.gz"
    if damage == "drop_middle":
        middle.unlink()
        middle.with_suffix(".json").unlink()
    else:
        middle.write_bytes(gzip.compress(b'{"turn": 9}\n'))
    restored, refused = sa.restore(root / sa.ARCHIVE, tmp_path / "back")
    assert restored == ["root:wolt-a/.claude/projects/-w/other.jsonl"]  # other sessions stay whole
    assert len(refused) == 1 and CLAUDE in refused[0]
    assert not (tmp_path / "back" / "root" / CLAUDE).exists()


def test_a_tool_result_keeps_its_name_and_database_engines_are_only_reported(tmp_path):
    root = _lodge(tmp_path)
    _session(root, "wolt-a/.claude/projects/-w/s1/tool-results/r1.txt", "no newline at the end")
    _session(root, "wolt-a/.local/share/opencode/repos/x/README.md", "a clone\n")
    _session(root, "wolt-a/.hermes/state.db", b"SQLite format 3\x00")
    report = sa.refresh(root, HARNESSES)
    assert [line.split(" -> ")[0] for line in report.stored] == [
        "root:wolt-a/.claude/projects/-w/s1/tool-results/r1.txt"]
    assert sorted(report.not_archived) == ["hermes (database): snapshots only",
                                           "opencode (database): snapshots only"]
    sa.restore(root / sa.ARCHIVE, tmp_path / "back")
    assert (tmp_path / "back" / "root" / "wolt-a/.claude/projects/-w/s1/tool-results/r1.txt").read_text() \
        == "no newline at the end"


def test_natively_only_sessions_recorded_inside_the_lodge_are_taken(tmp_path):
    root, home = _lodge(tmp_path), tmp_path / "home"
    _session(home, ".claude/projects/-a/ours.jsonl", json.dumps({"cwd": str(root / "wolt-a")}) + "\n")
    _session(home, ".claude/projects/-b/mine.jsonl", json.dumps({"cwd": str(tmp_path / "elsewhere")}) + "\n")
    _session(home, ".claude/projects/-c/unsure.jsonl", '{"no": "cwd"}\n')
    _session(home, ".codex/sessions/r.jsonl", json.dumps({"payload": {"cwd": str(root)}}) + "\n")
    report = sa.refresh(root, HARNESSES, home=home)
    assert sorted(line.split(" -> ")[0] for line in report.stored) == [
        "home:.claude/projects/-a/ours.jsonl", "home:.codex/sessions/r.jsonl"]
    assert report.skipped == []  # unsure natively: passed over silently, never listed


def test_the_archive_is_owner_only_and_left_out_of_snapshots(tmp_path):
    root = _lodge(tmp_path)
    _session(root, CLAUDE, '{"a": 1}\n')
    sa.refresh(root, HARNESSES)
    archive = root / sa.ARCHIVE
    assert stat.S_IMODE(archive.stat().st_mode) == 0o700
    assert {stat.S_IMODE(p.stat().st_mode) for p in archive.rglob("*") if p.is_file()} == {0o600}
    result = create_backup(root, out_dir=tmp_path / "out", tag="t")
    with tarfile.open(result.archive, "r:gz") as tar:
        names = tar.getnames()
    assert any(name.endswith(CLAUDE) for name in names)  # the engine file itself still goes in
    assert not any("session-archive" in name for name in names)


def test_with_the_setting_unset_a_backup_makes_no_archive(tmp_path, monkeypatch):
    from woltspace.cli import main as cli_main
    root = _lodge(tmp_path, keep=False)
    _session(root, CLAUDE, '{"a": 1}\n')
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    assert cli_main(["backup", "--tag", "off", "--out", str(tmp_path / "out")]) == 0
    assert not (root / sa.ARCHIVE).exists()
    assert cli_main(["backup", "sessions"]) == 2  # asked for by name: refused, still nothing written
    assert not (root / sa.ARCHIVE).exists()


def test_restore_refuses_a_part_that_names_a_path_outside(tmp_path):
    root = _lodge(tmp_path)
    _session(root, CLAUDE, '{"a": 1}\n')
    sa.refresh(root, HARNESSES)
    meta_path, = (root / sa.ARCHIVE / sa.FORMAT).rglob("*.json")
    meta_path.write_text(json.dumps({**json.loads(meta_path.read_text()), "path": "../../escape"}))
    restored, refused = sa.restore(root / sa.ARCHIVE, tmp_path / "back")
    assert restored == [] and "outside" in refused[0] and not (tmp_path / "escape").exists()


def test_off_unless_the_lodge_sets_sessions_archive(tmp_path):
    assert not sa.enabled(_lodge(tmp_path, keep=False))
    assert sa.enabled(_lodge(tmp_path / "on", keep=True))


@pytest.mark.parametrize("sessions, on", [({"archive": True}, True), ({"archive": False}, False), ({}, False),
                                          ({"keep_days": None}, False), ({"archive": "yes"}, False)])
def test_the_archive_switch_is_sessions_archive_true(tmp_path, sessions, on):  # review (c)
    root = tmp_path / "wolts"
    root.mkdir()
    (root / "woltspace.json").write_text(json.dumps({"sessions": sessions}))
    assert sa.enabled(root) is on


def test_list_shows_one_row_per_archived_session(tmp_path, monkeypatch, capsys):  # review (b)
    from woltspace.cli import main as cli_main
    root = tmp_path / "wolts"
    (root / "wolt-a").mkdir(parents=True)
    (root / "woltspace.json").write_text(json.dumps({"sessions": {"archive": True}}))
    sid = "aaaa1111-2222-3333-4444-555566667777"
    _session(root, f"wolt-a/.claude/projects/-w/{sid}.jsonl", '{"a": 1}\n')
    _session(root, f"wolt-a/.claude/projects/-w/{sid}/tool-results/r.txt", "tool output")
    _session(root, "wolt-a/.claude/projects/-w/notes.jsonl", '{"b": 2}\n')  # no session id
    sa.refresh(root, HARNESSES)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    assert cli_main(["backup", "sessions", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["session"], r["path"], r["files"], r["bytes"]) for r in rows] == [
        (sid, f"root:wolt-a/.claude/projects/-w/{sid}.jsonl", 2, 9 + 11),  # the session file and its tool result
        (None, "root:wolt-a/.claude/projects/-w/notes.jsonl", 1, 9),
    ]
    assert all(r["last_copied"] for r in rows)
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == before  # read only
    assert cli_main(["backup", "sessions", "list"]) == 0 and sid in capsys.readouterr().out


@pytest.mark.parametrize("isolation", ["host", "external"])
@pytest.mark.parametrize("on", [True, False])
def test_doctor_advises_on_claude_retention_only_when_the_archive_is_on(tmp_path, isolation, on):  # review (a)
    from woltspace.doctor import run_doctor
    from woltspace.layout import RuntimeLayout, installation_root
    root = _lodge(tmp_path, keep=on)
    before = (root / "woltspace.json").read_text()
    checks = {c.name: c for c in run_doctor(RuntimeLayout(wolts_dir=root, install_root=installation_root(),
                                                          host="127.0.0.1", port=18777, isolation=isolation),
                                            check_port=False)}
    assert ("cleanupPeriodDays" in checks["session-archive"].remedy) if on else "session-archive" not in checks
    assert (root / "woltspace.json").read_text() == before and not (root / "wolt-a" / ".claude").exists()
