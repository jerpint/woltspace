"""Backup and restore of a wolts directory — data in, junk out, round trip.

Every fixture here is synthetic. A backup test that pointed at a real colony
would read a real `.env`, and the whole point of this module is that it never
does that to anyone's machine.
"""

import json
import os
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from woltspace import backup as backup_mod  # noqa: E402
from woltspace.backup import (  # noqa: E402
    ARCHIVE_ROOT,
    MANIFEST_NAME,
    create_backup,
    restore_backup,
    scan_tree,
    verify_archive,
)
from woltspace.cli import main as cli_main  # noqa: E402

SECRET = "sk-do-not-print-me-0123456789"


@pytest.fixture
def wolts(tmp_path):
    """A synthetic lodge: two wolts, platform state, and a pile of junk."""
    root = tmp_path / "wolts"

    def write(rel: str, text: str) -> Path:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    write(".env", f"TELEGRAM_BOT_TOKEN={SECRET}\n")
    write("woltspace.json", json.dumps({"active": {"telegram": "beaverwolt"}}))
    write(".state/registry/session-1.json", json.dumps({"name": "session-1"}))
    write(".space/platform/config.json", json.dumps({"channels": {}}))

    for name in ("beaverwolt", "raccoonwolt"):
        write(f"{name}/CLAUDE.md", f"# {name}\n")
        write(f"{name}/wolt/wolt.json", json.dumps({"name": name, "type": "rodent"}))
        write(f"{name}/wolt/memory/identity.md", f"I am {name}.\n")
        write(f"{name}/wolt/site/index.html", "<h1>pond</h1>")
        write(f"{name}/wolt/.git/HEAD", "ref: refs/heads/main\n")

    # Junk that must not survive the excludes.
    write("beaverwolt/wolt/apps/demo/node_modules/left-pad/index.js", "x" * 50_000)
    write("beaverwolt/wolt/apps/demo/dist/bundle.js", "y" * 20_000)
    write("beaverwolt/.venv/lib/site-packages/thing.py", "z" * 10_000)
    write("beaverwolt/wolt/memory/__pycache__/notes.cpython-311.pyc", "b" * 5_000)
    write("beaverwolt/wolt/memory/notes.pyc", "b" * 1_000)
    write("raccoonwolt/wolt/worktrees/feature-x/README.md", "w" * 8_000)
    write("raccoonwolt/.claude/plugins/cache/blob.bin", "c" * 7_000)
    write("raccoonwolt/wolt/site/.DS_Store", "d" * 100)
    write(".worktui/branch-a/file.txt", "t" * 4_000)

    # A symlink pointing outside the tree — the plugin-delivery shape.
    outside = tmp_path / "wheel" / "skills"
    outside.mkdir(parents=True)
    (outside / "skill.md").write_text("platform skill")
    (root / "beaverwolt" / ".claude").mkdir(parents=True, exist_ok=True)
    (root / "beaverwolt" / ".claude" / "skills").symlink_to(outside)

    return root


def _members(archive: Path) -> list[str]:
    with tarfile.open(archive, "r:gz") as tar:
        return tar.getnames()


def test_excludes_drop_the_junk_and_keep_the_data(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = _members(result.archive)

    for kept in (
        f"{ARCHIVE_ROOT}/.env",
        f"{ARCHIVE_ROOT}/woltspace.json",
        f"{ARCHIVE_ROOT}/.state/registry/session-1.json",
        f"{ARCHIVE_ROOT}/.space/platform/config.json",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/wolt.json",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/identity.md",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/.git/HEAD",
    ):
        assert kept in names

    junk = ("node_modules", "/dist/", ".venv", "__pycache__", ".pyc", "worktrees",
            "plugins/cache", ".DS_Store", ".worktui")
    assert not [name for name in names if any(bit in name for bit in junk)]


def test_the_size_win_is_reported(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    totals = result.manifest["totals"]
    assert totals["excluded_bytes"] > 100_000
    assert totals["included_bytes"] < totals["scanned_bytes"]
    assert totals["excluded_files"] >= 8


def test_symlinks_are_archived_as_links_not_followed(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    link = f"{ARCHIVE_ROOT}/beaverwolt/.claude/skills"
    with tarfile.open(result.archive, "r:gz") as tar:
        member = tar.getmember(link)
        assert member.issym()
    assert not [name for name in _members(result.archive) if name.endswith("skills/skill.md")]


def test_manifest_describes_the_lodge_without_leaking_secrets(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="probe")
    manifest = result.manifest
    assert manifest["tag"] == "probe"
    assert [wolt["name"] for wolt in manifest["wolts"]] == ["beaverwolt", "raccoonwolt"]
    assert all(wolt["files"] > 0 for wolt in manifest["wolts"])
    assert "node_modules" in manifest["excludes"]["dir_names"]

    blob = json.dumps(manifest)
    assert SECRET not in blob

    printed = "\n".join(backup_mod.summary_lines(result))
    assert SECRET not in printed


def test_verification_catches_a_manifest_that_lies(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert result.verified
    assert verify_archive(result.archive)["entries"] == result.manifest["entries"]

    bogus = tmp_path / "bogus.tar.gz"
    with tarfile.open(result.archive, "r:gz") as src, tarfile.open(bogus, "w:gz") as dst:
        for member in src.getmembers():
            if member.name == MANIFEST_NAME:
                manifest = json.loads(src.extractfile(member).read().decode())
                manifest["entries"] += 5
                blob = json.dumps(manifest).encode()
                member.size = len(blob)
                dst.addfile(member, __import__("io").BytesIO(blob))
            else:
                dst.addfile(member, src.extractfile(member) if member.isreg() else None)
    with pytest.raises(ValueError, match="entries"):
        verify_archive(bogus)


def test_unreadable_files_warn_rather_than_fail(wolts, tmp_path):
    locked = wolts / "beaverwolt" / "wolt" / "memory" / "locked.md"
    locked.write_text("secretive")
    os.chmod(locked, 0o000)
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out")
    finally:
        os.chmod(locked, 0o600)
    warned = [item["path"] for item in result.manifest["unreadable"]]
    assert any(path.endswith("locked.md") for path in warned)
    assert result.verified


def test_round_trip_restores_the_tree_byte_for_byte(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    restored = restore_backup(result.archive, to=tmp_path / "restored")

    def snapshot(root: Path) -> dict:
        seen = {}
        scan = scan_tree(root)
        for entry in scan.entries:
            rel = entry.arcname[len(ARCHIVE_ROOT) + 1:]
            if entry.kind == "file":
                seen[rel] = ("file", entry.path.read_bytes())
            elif entry.kind == "symlink":
                seen[rel] = ("symlink", os.readlink(entry.path))
            else:
                seen[rel] = ("dir", None)
        return seen

    before = snapshot(wolts)
    after = snapshot(restored.wolts_dir)
    assert before == after
    assert restored.manifest["tag"] == result.manifest["tag"]
    assert restored.entries == result.manifest["entries"]
    assert (restored.wolts_dir / ".env").read_text().endswith(f"{SECRET}\n")


def test_restore_refuses_a_populated_target(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "something").write_text("mine")
    with pytest.raises(FileExistsError):
        restore_backup(result.archive, to=occupied)
    assert (occupied / "something").read_text() == "mine"


def test_restore_default_target_sits_beside_the_archive(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="beside")
    restored = restore_backup(result.archive)
    assert restored.target == tmp_path / "out" / "woltspace-backup-beside-restored"
    assert (restored.wolts_dir / "woltspace.json").is_file()


def test_restore_refuses_a_member_that_escapes_the_target(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    payload = tmp_path / "payload"
    payload.write_text("pwn")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="../escaped")
    with pytest.raises(ValueError, match="outside the target"):
        restore_backup(archive, to=tmp_path / "target")


def test_cli_backup_and_restore(wolts, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("WOLTS_DIR", str(wolts))
    assert cli_main(["backup", "--tag", "cli", "--out", str(tmp_path / "out"), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    archive = Path(payload["archive"])
    assert archive.name == "woltspace-backup-cli.tar.gz"

    assert cli_main(["restore", str(archive), "--to", str(tmp_path / "back")]) == 0
    out = capsys.readouterr().out
    assert f"WOLTS_DIR={tmp_path / 'back' / ARCHIVE_ROOT}" in out
    assert (tmp_path / "back" / ARCHIVE_ROOT / "beaverwolt" / "wolt" / "wolt.json").is_file()


def test_cli_reports_a_missing_source_without_traceback(tmp_path, capsys):
    code = cli_main(["backup", "--wolts-dir", str(tmp_path / "nope"), "--out", str(tmp_path)])
    assert code == 1
    assert "backup failed" in capsys.readouterr().out
