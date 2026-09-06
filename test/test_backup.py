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
    excludes = manifest["excludes"]
    assert "node_modules" in excludes["by_dir_name"]["below_root"]
    assert excludes["by_dir_name"]["at_root"] == [".worktui"]
    assert ".local/share/claude" in excludes["by_path"]["suffixes"]
    assert "node_modules" not in excludes["by_path"]["suffixes"]

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
    # The secret is the one thing that does NOT come back — by design.
    assert not (restored.wolts_dir / ".env").exists()


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


# ---------------------------------------------------------------------------
# The root is not the inside of a project
# ---------------------------------------------------------------------------

def test_a_wolt_named_dist_is_backed_up_whole(wolts, tmp_path):
    """The blocker: name-based excludes must not eat a top-level wolt.

    `dist`, `build`, `target` are junk inside a project and ordinary names for
    a wolt. Silently dropping one is the worst thing a backup can do.
    """
    for name in ("dist", "build", "target", ".cache"):
        (wolts / name / "wolt" / "memory").mkdir(parents=True)
        (wolts / name / "wolt" / "wolt.json").write_text(json.dumps({"name": name}))
        (wolts / name / "wolt" / "memory" / "identity.md").write_text(f"I am {name}.\n")
        # …and the junk *inside* it must still go.
        (wolts / name / "wolt" / "node_modules").mkdir()
        (wolts / name / "wolt" / "node_modules" / "blob.js").write_text("j" * 4_000)

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = _members(result.archive)
    for name in ("dist", "build", "target", ".cache"):
        assert f"{ARCHIVE_ROOT}/{name}/wolt/wolt.json" in names
        assert f"{ARCHIVE_ROOT}/{name}/wolt/memory/identity.md" in names
        assert not [entry for entry in names if entry.startswith(f"{ARCHIVE_ROOT}/{name}/wolt/node_modules")]
    assert {wolt["name"] for wolt in result.manifest["wolts"]} >= {"dist", "build", "target", ".cache"}


def test_a_plain_top_level_dir_is_kept_even_with_a_junk_name(wolts, tmp_path):
    (wolts / "build").mkdir()
    (wolts / "build" / "notes.md").write_text("not a wolt, still data")
    (wolts / "apps" / "site").mkdir(parents=True)
    (wolts / "apps" / "site" / "app.json").write_text("{}")
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert f"{ARCHIVE_ROOT}/build/notes.md" in names
    assert f"{ARCHIVE_ROOT}/apps/site/app.json" in names


def test_the_platforms_own_worktree_store_still_goes(wolts, tmp_path):
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert not [name for name in names if ".worktui" in name]


def test_a_wolt_nested_under_a_junk_name_survives(wolts, tmp_path):
    nested = wolts / "beaverwolt" / "wolt" / "apps" / "build" / "rescuedwolt"
    (nested / "wolt").mkdir(parents=True)
    (nested / "wolt" / "wolt.json").write_text(json.dumps({"name": "rescuedwolt"}))
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert any(name.endswith("build/rescuedwolt/wolt/wolt.json") for name in names)


def test_the_agent_clis_version_store_goes_but_the_rest_of_local_stays(wolts, tmp_path):
    """`.local/share/claude` is the biggest thing in a lived-in colony.

    12.8GB of redownloadable CLI binaries on the colony this was measured
    against — and the only part of `.local` that may go. `opencode.db` beside
    it is session state, and large, and stays.
    """
    home = wolts / "beaverwolt" / "home" / ".local" / "share"
    (home / "claude" / "versions").mkdir(parents=True)
    (home / "claude" / "versions" / "1.2.3").write_text("b" * 60_000)
    (home / "claude" / "runtime-cache.bin").write_text("c" * 20_000)
    (home / "opencode").mkdir(parents=True)
    (home / "opencode" / "opencode.db").write_text("session state")
    (home.parent / "state" / "notes").mkdir(parents=True)
    (home.parent / "state" / "notes" / "keep.md").write_text("kept")
    # A wolt's own directory called `claude`, nowhere near `.local/share`.
    (wolts / "beaverwolt" / "wolt" / "sparks" / "claude").mkdir(parents=True)
    (wolts / "beaverwolt" / "wolt" / "sparks" / "claude" / "poster.html").write_text("mine")

    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert not [name for name in names if "/.local/share/claude" in name]
    assert f"{ARCHIVE_ROOT}/beaverwolt/home/.local/share/opencode/opencode.db" in names
    assert f"{ARCHIVE_ROOT}/beaverwolt/home/.local/state/notes/keep.md" in names
    assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/sparks/claude/poster.html" in names


def test_a_top_level_dir_named_claude_is_never_path_excluded(wolts, tmp_path):
    (wolts / "claude" / "wolt").mkdir(parents=True)
    (wolts / "claude" / "wolt" / "wolt.json").write_text(json.dumps({"name": "claude"}))
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert f"{ARCHIVE_ROOT}/claude/wolt/wolt.json" in names


# ---------------------------------------------------------------------------
# Secrets stay home
# ---------------------------------------------------------------------------

@pytest.fixture
def wolts_with_credentials(wolts):
    """The credential shapes a real colony actually has on disk."""
    (wolts / ".claude").mkdir(parents=True, exist_ok=True)
    (wolts / ".claude" / ".credentials.json").write_text(json.dumps({"token": SECRET}))
    (wolts / ".claude" / ".credentials.json.expired-2026.bak").write_text(SECRET)
    (wolts / ".claude" / ".credentials.json.stale-2025.bak").write_text(SECRET)
    (wolts / "beaverwolt" / "home" / ".codex").mkdir(parents=True)
    (wolts / "beaverwolt" / "home" / ".codex" / "auth.json").write_text(SECRET)
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop").mkdir(parents=True)
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / ".env").write_text(f"STRIPE={SECRET}")
    # Lookalikes that are documentation, not credentials.
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / "env.example").write_text("STRIPE=")
    (wolts / ".env.example").write_text("TELEGRAM_BOT_TOKEN=")
    (wolts / "beaverwolt" / "wolt" / "memory" / "credentials.md").write_text("how auth works")
    (wolts / "beaverwolt" / "wolt" / "memory" / ".codex").mkdir()
    (wolts / "beaverwolt" / "wolt" / "memory" / ".codex" / "notes.json").write_text("{}")
    return wolts


def test_no_credential_ever_enters_the_archive(wolts_with_credentials, tmp_path):
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out")
    names = _members(result.archive)

    for secret in (
        f"{ARCHIVE_ROOT}/.env",
        f"{ARCHIVE_ROOT}/.claude/.credentials.json",
        f"{ARCHIVE_ROOT}/.claude/.credentials.json.expired-2026.bak",
        f"{ARCHIVE_ROOT}/.claude/.credentials.json.stale-2025.bak",
        f"{ARCHIVE_ROOT}/beaverwolt/home/.codex/auth.json",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/.env",
    ):
        assert secret not in names

    with tarfile.open(result.archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isreg():
                assert SECRET.encode() not in tar.extractfile(member).read()


def test_the_manifest_names_every_withheld_path(wolts_with_credentials, tmp_path):
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out")
    withheld = result.manifest["withheld"]
    paths = {item["path"]: item["rule"] for item in withheld["paths"]}
    assert paths == {
        ".env": "dotenv",
        "beaverwolt/wolt/apps/shop/.env": "dotenv",
        ".claude/.credentials.json": "claude-credentials",
        ".claude/.credentials.json.expired-2026.bak": "claude-credentials",
        ".claude/.credentials.json.stale-2025.bak": "claude-credentials",
        "beaverwolt/home/.codex/auth.json": "codex-auth",
    }
    assert {rule["rule"] for rule in withheld["rules"]} == {
        "dotenv", "claude-credentials", "codex-auth",
    }
    assert SECRET not in json.dumps(result.manifest)
    assert "6 credential file(s)" in "\n".join(backup_mod.summary_lines(result))


def test_lookalikes_are_not_withheld(wolts_with_credentials, tmp_path):
    """`env.example`, `.env.example`, `credentials.md`, `.codex/notes.json`."""
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out")
    names = _members(result.archive)
    for kept in (
        f"{ARCHIVE_ROOT}/.env.example",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/env.example",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/credentials.md",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/.codex/notes.json",
    ):
        assert kept in names
    withheld = {item["path"] for item in result.manifest["withheld"]["paths"]}
    assert not [path for path in withheld if "example" in path or path.endswith(".md")]


def test_restore_prints_the_reprovision_checklist(wolts_with_credentials, tmp_path, capsys):
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out", tag="creds")
    assert cli_main(["restore", str(result.archive), "--to", str(tmp_path / "back")]) == 0
    out = capsys.readouterr().out
    assert "6 credential file(s), by design" in out
    assert "copy .env.example" in out
    assert "woltspace init" in out
    assert "codex login" in out
    assert "beaverwolt/wolt/apps/shop/.env" in out
    assert "revoke the live one" in out
    assert not (tmp_path / "back" / ARCHIVE_ROOT / ".env").exists()


def test_a_backup_with_no_credentials_says_nothing_about_them(wolts, tmp_path):
    (wolts / ".env").unlink()
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert result.manifest["withheld"]["paths"] == []
    assert "withheld" not in "\n".join(backup_mod.summary_lines(result))
    restored = restore_backup(result.archive, to=tmp_path / "back")
    assert "by design" not in "\n".join(backup_mod.restore_lines(restored))


# ---------------------------------------------------------------------------
# Extraction safety — archives built by hand, to be refused by hand
# ---------------------------------------------------------------------------

def _outside_witness(tmp_path):
    """A directory no restore is ever allowed to touch."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "witness.txt").write_text("untouched")
    return outside


def _write_archive(path: Path, build) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        build(tar)
    return path


def _add_bytes(tar, name, data=b"payload", **kw):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    for key, value in kw.items():
        setattr(info, key, value)
    tar.addfile(info, __import__("io").BytesIO(data))


def _add_symlink(tar, name, target):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    tar.addfile(info)


def test_hard_linked_data_is_stored_whole_not_linked(wolts, tmp_path):
    """Claude's `file-history` hard-links identical versions. Kept data, real.

    tarfile's inode memory would turn the second name into a LNK member, and
    this module's restore refuses those — a backup that wrote an archive it
    could not read back. Every regular file is stored whole instead.
    """
    history = wolts / ".claude" / "file-history" / "session-a"
    history.mkdir(parents=True)
    original = history / "notes.md@v1"
    original.write_text("one version, two names")
    twin_dir = wolts / ".claude" / "file-history" / "session-b"
    twin_dir.mkdir(parents=True)
    twin = twin_dir / "notes.md@v1"
    os.link(original, twin)
    assert original.stat().st_nlink == 2

    result = create_backup(wolts, out_dir=tmp_path / "out")
    with tarfile.open(result.archive, "r:gz") as tar:
        members = {
            member.name: member
            for member in tar.getmembers()
            if member.name.endswith("notes.md@v1")
        }
        assert len(members) == 2
        for member in members.values():
            assert member.isreg(), f"{member.name} was stored as {member.type!r}"
            assert member.size == original.stat().st_size

    restored = restore_backup(result.archive, to=tmp_path / "restored")
    back_a = restored.wolts_dir / ".claude" / "file-history" / "session-a" / "notes.md@v1"
    back_b = restored.wolts_dir / ".claude" / "file-history" / "session-b" / "notes.md@v1"
    assert back_a.read_text() == back_b.read_text() == "one version, two names"
    assert back_a.stat().st_nlink == 1
    assert back_b.stat().st_nlink == 1
    assert back_a.stat().st_ino != back_b.stat().st_ino


def test_restore_refuses_a_hard_link(tmp_path):
    outside = _outside_witness(tmp_path)
    archive = tmp_path / "hardlink.tar.gz"

    def build(tar):
        _add_bytes(tar, "wolts/anchor.txt")
        info = tarfile.TarInfo("wolts/stolen")
        info.type = tarfile.LNKTYPE
        info.linkname = str(outside / "witness.txt")
        tar.addfile(info)

    _write_archive(archive, build)
    with pytest.raises(ValueError, match="hard link"):
        restore_backup(archive, to=tmp_path / "target")
    assert (outside / "witness.txt").read_text() == "untouched"
    assert not (tmp_path / "target" / "wolts").exists()


def test_restore_refuses_a_hard_link_climbing_out(tmp_path):
    archive = tmp_path / "hardlink2.tar.gz"

    def build(tar):
        info = tarfile.TarInfo("wolts/stolen")
        info.type = tarfile.LNKTYPE
        info.linkname = "../../etc/passwd"
        tar.addfile(info)

    _write_archive(archive, build)
    with pytest.raises(ValueError, match="hard link"):
        restore_backup(archive, to=tmp_path / "target")


def test_restore_refuses_a_write_through_a_symlink(tmp_path):
    """Member 1 links out of the tree; member 2 writes through it."""
    outside = _outside_witness(tmp_path)
    archive = tmp_path / "through.tar.gz"

    def build(tar):
        _add_symlink(tar, "wolts/x", str(outside))
        _add_bytes(tar, "wolts/x/evil.txt", b"pwned")

    _write_archive(archive, build)
    with pytest.raises(ValueError, match="runs through a symlink"):
        restore_backup(archive, to=tmp_path / "target")
    assert not (outside / "evil.txt").exists()
    assert sorted(item.name for item in outside.iterdir()) == ["witness.txt"]


def test_restore_refuses_a_file_that_replaces_a_symlink(tmp_path):
    """The same trick without a path component: link `x`, then a file `x`."""
    outside = _outside_witness(tmp_path)
    archive = tmp_path / "replace.tar.gz"

    def build(tar):
        _add_symlink(tar, "wolts/x", str(outside / "witness.txt"))
        _add_bytes(tar, "wolts/x", b"pwned")

    _write_archive(archive, build)
    with pytest.raises(ValueError, match="symlink|duplicate"):
        restore_backup(archive, to=tmp_path / "target")
    assert (outside / "witness.txt").read_text() == "untouched"


def test_restore_refuses_duplicate_members(tmp_path):
    """Last-wins is how a reader and an extractor get told different stories."""
    archive = tmp_path / "dupes.tar.gz"

    def build(tar):
        _add_bytes(tar, "wolts/note.md", b"first")
        _add_bytes(tar, "wolts/note.md", b"second")

    _write_archive(archive, build)
    with pytest.raises(ValueError, match="duplicate"):
        restore_backup(archive, to=tmp_path / "target")
    assert not (tmp_path / "target" / "wolts").exists()


def test_case_folding_collision_is_refused_or_kept_distinct(tmp_path):
    """On APFS two names that differ only by case cannot both exist.

    Restoring one file's bytes under the other's name is silent corruption, so
    a case-folding filesystem refuses the archive outright. On a case-sensitive
    one both files are restored, distinct and intact.
    """
    archive = tmp_path / "case.tar.gz"

    def build(tar):
        _add_bytes(tar, "wolts/Notes.md", b"upper")
        _add_bytes(tar, "wolts/notes.md", b"lower")

    _write_archive(archive, build)
    target = tmp_path / "target"
    try:
        restore_backup(archive, to=target)
    except ValueError as exc:
        assert "collide" in str(exc)
        return
    assert (target / "wolts" / "Notes.md").read_bytes() == b"upper"
    assert (target / "wolts" / "notes.md").read_bytes() == b"lower"


def test_absolute_member_is_refused(tmp_path):
    archive = tmp_path / "absolute.tar.gz"
    _write_archive(archive, lambda tar: _add_bytes(tar, "/etc/pwned"))
    with pytest.raises(ValueError, match="outside the target"):
        restore_backup(archive, to=tmp_path / "target")


def test_long_and_non_ascii_names_round_trip(wolts, tmp_path):
    deep = wolts / "beaverwolt" / "wolt" / "memory" / ("nest/" * 30)
    deep = Path(str(deep))
    deep.mkdir(parents=True)
    (deep / ("l" * 120 + ".md")).write_text("deep")
    (wolts / "beaverwolt" / "wolt" / "memory" / "мойволт-🦫-ноты.md").write_text("юникод")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    restored = restore_backup(result.archive, to=tmp_path / "restored")
    base = restored.wolts_dir / "beaverwolt" / "wolt" / "memory"
    assert (base / "мойволт-🦫-ноты.md").read_text() == "юникод"
    assert (Path(str(base / ("nest/" * 30))) / ("l" * 120 + ".md")).read_text() == "deep"


# ---------------------------------------------------------------------------
# Symlinks on the way in
# ---------------------------------------------------------------------------

def test_broken_symlink_and_cycle_neither_crash_nor_hang(wolts, tmp_path):
    memory = wolts / "beaverwolt" / "wolt" / "memory"
    (memory / "dangling.md").symlink_to(wolts / "beaverwolt" / "wolt" / "gone.md")
    (memory / "loop-a").symlink_to(memory / "loop-b")
    (memory / "loop-b").symlink_to(memory / "loop-a")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = _members(result.archive)
    for link in ("dangling.md", "loop-a", "loop-b"):
        assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/{link}" in names

    restored = restore_backup(result.archive, to=tmp_path / "restored")
    back = restored.wolts_dir / "beaverwolt" / "wolt" / "memory"
    assert os.path.islink(back / "dangling.md")
    assert not (back / "dangling.md").exists()  # still dangling, as it was
    assert os.path.islink(back / "loop-a")


def test_a_relative_link_inside_the_tree_still_resolves_after_restore(wolts, tmp_path):
    (wolts / "beaverwolt" / "wolt" / "sparks").mkdir(parents=True, exist_ok=True)
    (wolts / "beaverwolt" / "wolt" / "sparks" / "latest.html").symlink_to(
        Path("..") / "site" / "index.html"
    )
    (wolts / "shared.json").symlink_to(Path("beaverwolt") / "wolt" / "wolt.json")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    restored = restore_backup(result.archive, to=tmp_path / "restored")
    spark = restored.wolts_dir / "beaverwolt" / "wolt" / "sparks" / "latest.html"
    assert os.readlink(spark) == "../site/index.html"
    assert spark.read_text() == "<h1>pond</h1>"
    root_link = restored.wolts_dir / "shared.json"
    assert os.readlink(root_link) == "beaverwolt/wolt/wolt.json"
    assert json.loads(root_link.read_text())["name"] == "beaverwolt"


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
