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
    write(".space/platform/adoption.json", json.dumps({"adopted": []}))

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
        f"{ARCHIVE_ROOT}/.space/platform/adoption.json",
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
    # The one file a restore adds: the mark that keeps the copy on its own tmux.
    assert after.pop(backup_mod.RESTORED_MARKER)[0] == "file"
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


def test_codex_shell_snapshots_go_but_the_rest_of_codex_stays(wolts, tmp_path):
    """Codex bakes the whole exported environment into runnable .sh files.

    Every secret env var lands in them verbatim — the telegram bot token, on
    three wolts of the colony this was swept — and they are rebuilt on the next
    session anyway. `sessions/` and `rollouts/` beside them are history.
    """
    codex = wolts / "beaverwolt" / "home" / ".codex"
    (codex / "shell_snapshots").mkdir(parents=True)
    (codex / "shell_snapshots" / "snapshot-1.sh").write_text(
        f"export TELEGRAM_BOT_TOKEN={SECRET}\n"
    )
    (codex / "sessions").mkdir()
    (codex / "sessions" / "2026-09-06.jsonl").write_text('{"role":"user"}\n')
    (codex / "rollouts").mkdir()
    (codex / "rollouts" / "run.json").write_text("{}")
    # A directory of the same name that is not under .codex stays.
    (wolts / "beaverwolt" / "wolt" / "sparks" / "shell_snapshots").mkdir(parents=True)
    (wolts / "beaverwolt" / "wolt" / "sparks" / "shell_snapshots" / "demo.sh").write_text("echo hi")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = _members(result.archive)
    assert not [name for name in names if "shell_snapshots" in name and ".codex" in name]
    assert f"{ARCHIVE_ROOT}/beaverwolt/home/.codex/sessions/2026-09-06.jsonl" in names
    assert f"{ARCHIVE_ROOT}/beaverwolt/home/.codex/rollouts/run.json" in names
    assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/sparks/shell_snapshots/demo.sh" in names
    with tarfile.open(result.archive, "r:gz") as tar:
        for member in tar.getmembers():
            if member.isreg():
                assert SECRET.encode() not in tar.extractfile(member).read()


def test_a_worktree_copy_of_a_wolt_is_not_rescued_from_worktui(wolts, tmp_path):
    """`.worktui` holds worktrees, and a worktree of a wolt carries wolt.json.

    The buried-wolt rescue must not resurrect what the root rule deliberately
    drops: a copy of a wolt is not the wolt.
    """
    copy = wolts / ".worktui" / "wolts" / "nw-feature" / "beaverwolt"
    (copy / "wolt").mkdir(parents=True)
    (copy / "wolt" / "wolt.json").write_text(json.dumps({"name": "beaverwolt"}))
    (copy / "wolt" / "memory").mkdir()
    (copy / "wolt" / "memory" / "identity.md").write_text("a copy, not the wolt")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert not [name for name in _members(result.archive) if ".worktui" in name]
    assert "beaverwolt" in {wolt["name"] for wolt in result.manifest["wolts"]}


def test_the_rescue_still_works_under_an_ordinary_name_exclude(wolts, tmp_path):
    """The other direction: `apps/build/rescuedwolt` is still rescued."""
    nested = wolts / "beaverwolt" / "wolt" / "apps" / "build" / "rescuedwolt"
    (nested / "wolt").mkdir(parents=True)
    (nested / "wolt" / "wolt.json").write_text(json.dumps({"name": "rescuedwolt"}))
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    assert any(name.endswith("build/rescuedwolt/wolt/wolt.json") for name in names)


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
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / ".env.local").write_text(f"DB={SECRET}")
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / ".env.production").write_text(SECRET)
    # Lookalikes that are documentation, not credentials.
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / "env.example").write_text("STRIPE=")
    (wolts / "beaverwolt" / "wolt" / "apps" / "shop" / ".env.sample").write_text("DB=")
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
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/.env.local",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/.env.production",
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
        "beaverwolt/wolt/apps/shop/.env.local": "dotenv",
        "beaverwolt/wolt/apps/shop/.env.production": "dotenv",
        ".claude/.credentials.json": "claude-credentials",
        ".claude/.credentials.json.expired-2026.bak": "claude-credentials",
        ".claude/.credentials.json.stale-2025.bak": "claude-credentials",
        "beaverwolt/home/.codex/auth.json": "codex-auth",
    }
    assert {rule["rule"] for rule in withheld["rules"]} == {
        "dotenv", "claude-credentials", "codex-auth", "platform-config",
    }
    assert SECRET not in json.dumps(result.manifest)
    assert "8 credential file(s)" in "\n".join(backup_mod.summary_lines(result))


def test_lookalikes_are_not_withheld(wolts_with_credentials, tmp_path):
    """`env.example`, `.env.example`, `credentials.md`, `.codex/notes.json`."""
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out")
    names = _members(result.archive)
    for kept in (
        f"{ARCHIVE_ROOT}/.env.example",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/env.example",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/.env.sample",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/credentials.md",
        f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/.codex/notes.json",
    ):
        assert kept in names
    withheld = {item["path"] for item in result.manifest["withheld"]["paths"]}
    assert not [
        path for path in withheld
        if path.endswith((".example", ".sample", ".md"))
    ]


def test_restore_prints_the_reprovision_checklist(wolts_with_credentials, tmp_path, capsys):
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out", tag="creds")
    assert cli_main(["restore", str(result.archive), "--to", str(tmp_path / "back")]) == 0
    out = capsys.readouterr().out
    assert "8 credential file(s), by design" in out
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
        _add_bytes(tar, "backup-manifest.json", b'{"archive_root": "wolts"}')
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
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(wolts))
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


# Slim (the default) and --full


@pytest.fixture
def lived_in(wolts):
    """The same lodge after some living: media, big logs, caches, checkouts."""

    def write(rel: str, data: bytes) -> None:
        path = wolts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    write("beaverwolt/wolt/drafts/talk/render.mp4", b"\0" * 40_000)
    write("beaverwolt/wolt/site/hero.png", b"\x89PNG" + b"\0" * 9_000)
    write("beaverwolt/wolt/drafts/huge-notes.md", b"n" * (backup_mod.SLIM_TEXT_MAX + 1))
    write("beaverwolt/wolt/drafts/plan.md", b"# plan\n")
    write("beaverwolt/wolt/drafts/nb.ipynb", b"{}")
    write("beaverwolt/worktrees/feature-y/README.md", b"checkout\n")
    write("beaverwolt/wolt/sparks/tool/lib/python3.13/site.py", b"x = 1\n")
    write("beaverwolt/wolt/sparks/npm/_cacache/index", b"c" * 100)
    write("beaverwolt/.git/worktrees/feature-y/HEAD", b"ref: refs/heads/y\n")
    # Context, kept at any size and in any format.
    big = b"{}\n" * (backup_mod.SLIM_TEXT_MAX // 3 + 1)
    write("beaverwolt/.claude/projects/-pond/session.jsonl", big)
    write("raccoonwolt/.codex/sessions/2026/10/rollout.jsonl", big)
    write("raccoonwolt/.codex/state_5.sqlite", b"SQLite format 3\0")
    write("raccoonwolt/.local/share/opencode/opencode.db", b"SQLite format 3\0")
    write("beaverwolt/wolt/.git/objects/pack/pack-1.pack", b"\0" * 30_000)
    write("beaverwolt/wolt/memory/photo.jpg", b"\xff\xd8" + b"\0" * 100)
    write(".space/logs/control-plane.log", b"log\n")
    return wolts


def test_slim_keeps_all_context_and_code_and_lists_the_rest(lived_in, tmp_path):
    result = create_backup(lived_in, out_dir=tmp_path / "out")
    names = set(_members(result.archive))

    for kept in (
        "beaverwolt/wolt/drafts/plan.md",
        "beaverwolt/wolt/drafts/nb.ipynb",
        "beaverwolt/wolt/site/index.html",
        "beaverwolt/.claude/projects/-pond/session.jsonl",
        "raccoonwolt/.codex/sessions/2026/10/rollout.jsonl",
        "raccoonwolt/.codex/state_5.sqlite",
        "raccoonwolt/.local/share/opencode/opencode.db",
        "beaverwolt/wolt/.git/objects/pack/pack-1.pack",
        "beaverwolt/.git/worktrees/feature-y/HEAD",
        "beaverwolt/wolt/memory/photo.jpg",
        ".space/platform/config.json",
    ):
        assert f"{ARCHIVE_ROOT}/{kept}" in names, kept

    left = {
        "beaverwolt/wolt/drafts/talk/render.mp4",
        "beaverwolt/wolt/site/hero.png",
        "beaverwolt/wolt/drafts/huge-notes.md",
        "beaverwolt/worktrees",
        "beaverwolt/wolt/sparks/tool/lib/python3.13",
        "beaverwolt/wolt/sparks/npm/_cacache",
        ".space/logs/control-plane.log",
    }
    assert not {f"{ARCHIVE_ROOT}/{rel}" for rel in left} & names
    assert not [n for n in names if n.startswith(f"{ARCHIVE_ROOT}/beaverwolt/worktrees/")]

    manifest = result.manifest
    assert manifest["mode"] == "slim"
    with tarfile.open(result.archive, "r:gz") as tar:
        listed = tar.extractfile(backup_mod.LEFT_OUT_NAME).read().decode().splitlines()
    rows = {line.split("\t")[0]: line.split("\t") for line in listed[1:]}
    assert left <= set(rows)
    assert rows["beaverwolt/worktrees"][1] == "dir"
    assert int(rows["beaverwolt/wolt/drafts/talk/render.mp4"][2]) == 40_000
    assert manifest["left_out"]["files"] == sum(int(r[3]) for r in rows.values())
    verify_archive(result.archive)


def test_full_keeps_media_and_writes_no_left_out_list(lived_in, tmp_path):
    result = create_backup(lived_in, out_dir=tmp_path / "out", full=True)
    names = set(_members(result.archive))
    assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/drafts/talk/render.mp4" in names
    assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/drafts/huge-notes.md" in names
    assert backup_mod.LEFT_OUT_NAME not in names
    assert result.manifest["mode"] == "full"
    assert "left_out" not in result.manifest


def test_slim_round_trip_restores_what_it_kept_and_says_what_it_did_not(lived_in, tmp_path):
    result = create_backup(lived_in, out_dir=tmp_path / "out")
    restored = restore_backup(result.archive, to=tmp_path / "back")
    assert restored.entries == result.manifest["entries"]
    root = restored.wolts_dir
    session = root / "beaverwolt" / ".claude" / "projects" / "-pond" / "session.jsonl"
    assert session.read_bytes() == (lived_in / "beaverwolt/.claude/projects/-pond/session.jsonl").read_bytes()
    assert not (root / "beaverwolt" / "wolt" / "drafts" / "talk" / "render.mp4").exists()
    assert (restored.target / backup_mod.LEFT_OUT_NAME).is_file()
    lines = backup_mod.restore_lines(restored)
    assert any(line.startswith("left out:") for line in lines)


def test_a_wolt_at_the_root_is_never_slim_dropped_by_name(wolts, tmp_path):
    (wolts / "cache" / "wolt").mkdir(parents=True)
    (wolts / "cache" / "wolt" / "wolt.json").write_text(json.dumps({"name": "cache"}))
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert f"{ARCHIVE_ROOT}/cache/wolt/wolt.json" in _members(result.archive)


def test_cli_full_flag(lived_in, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(lived_in))
    assert cli_main(["backup", "--tag", "slim", "--out", str(tmp_path / "out")]) == 0
    assert "mode: slim" in capsys.readouterr().out
    assert cli_main(["backup", "--full", "--tag", "full", "--out", str(tmp_path / "out"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["manifest"]["mode"] == "full"


def test_slim_keeps_every_transcript_even_inside_dropped_dirs(wolts, tmp_path):
    """Reproducibility: no session transcript is ever left out of a slim backup."""
    stores = (
        "beaverwolt/worktrees/feature-z/.claude/projects/-z/a.jsonl",
        "beaverwolt/wolt/worktrees/old/.codex/sessions/2026/r.jsonl",
        "beaverwolt/wolt/sparks/.tmp/lodge/wolts/w/.claude/projects/-w/b.jsonl",
        "beaverwolt/wolt/apps/demo/node_modules/x/.codex/archived_sessions/c.jsonl",
        "beaverwolt/wolt/drafts/cache/home/.local/share/opencode/opencode.db",
    )
    for rel in stores:
        path = wolts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 10)
    (wolts / "beaverwolt/worktrees/feature-z/big.mp4").write_bytes(b"\0" * 1000)
    (wolts / "beaverwolt/worktrees/feature-z/src.py").write_text("x = 1\n")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = set(_members(result.archive))
    for rel in stores:
        assert f"{ARCHIVE_ROOT}/{rel}" in names, rel
    # Only the transcripts come out of a dropped directory; the rest is listed.
    assert f"{ARCHIVE_ROOT}/beaverwolt/worktrees/feature-z/src.py" not in names
    with tarfile.open(result.archive, "r:gz") as tar:
        listed = tar.extractfile(backup_mod.LEFT_OUT_NAME).read().decode()
    assert "beaverwolt/worktrees/feature-z/big.mp4" in listed
    assert "beaverwolt/worktrees/feature-z/src.py" in listed
    assert "beaverwolt/wolt/apps/demo/node_modules/left-pad" in listed


# Slim leaves databases out unless a manifest opts them in


def test_slim_leaves_databases_out_unless_a_manifest_opts_them_in(wolts, tmp_path):
    def write(rel: str, data: bytes) -> None:
        path = wolts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    app = "beaverwolt/wolt/apps/demo"
    write(f"{app}/woltspace.json", json.dumps({"name": "demo", "backup": {"keep": ["data/app.db"]}}).encode())
    write(f"{app}/data/app.db", b"\0" * (backup_mod.SLIM_TEXT_MAX + 1))
    write(f"{app}/data/app.db-wal", b"\0" * 10)
    write(f"{app}/data/scratch.db", b"\0" * 10)
    # A wolt opts in from its own wolt.json; a manifest never reaches outside itself.
    write("raccoonwolt/wolt/wolt.json", json.dumps({
        "name": "raccoonwolt", "backup": {"keep": ["notes/*.sqlite", "../beaverwolt/x.db", "/etc/hosts"]},
    }).encode())
    write("raccoonwolt/notes/index.sqlite", b"\0" * 10)
    write("beaverwolt/x.db", b"\0" * 10)
    write("raccoonwolt/.codex/logs_2.sqlite", b"\0" * 10)
    write(f"{app}/Makefile", b"all:\n" * (backup_mod.SLIM_NO_SUFFIX_MAX // 5 + 1))

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = set(_members(result.archive))
    for kept in (f"{app}/data/app.db", f"{app}/data/app.db-wal", f"{app}/Makefile",
                 "raccoonwolt/notes/index.sqlite"):
        assert f"{ARCHIVE_ROOT}/{kept}" in names, kept
    for left in (f"{app}/data/scratch.db", "beaverwolt/x.db", "raccoonwolt/.codex/logs_2.sqlite"):
        assert f"{ARCHIVE_ROOT}/{left}" not in names, left
    with tarfile.open(result.archive, "r:gz") as tar:
        listed = tar.extractfile(backup_mod.LEFT_OUT_NAME).read().decode()
    assert f"{app}/data/scratch.db" in listed
    assert result.manifest["left_out"]["rules"]["opted_in"] == {
        app: ["data/app.db"], "raccoonwolt": ["notes/*.sqlite"],
    }


# A restored copy keeps its hands off the lodge it came from


def _lodge_runtime(wolts: Path, live_pid: int) -> None:
    """Runtime records as a running lodge leaves them, pointing at live processes."""
    source = str(wolts.resolve())

    def write(rel: str, data) -> None:
        path = wolts / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    write(".space/platform/control-plane.json", {"instance_id": "abc", "pid": live_pid})
    write(".space/platform/tunnel.json", {"pid": live_pid, "url": "https://example.test"})
    write(".space/platform/connectors.json", {"connectors": [{"name": "wolf", "pid": live_pid}]})
    write(".space/apps/demo.json", {"name": "demo", "pid": live_pid, "port": 4001, "tunnel_pid": None})
    session = {
        "name": "beaverwolt-s1", "wolt": "beaverwolt", "status": "running",
        "workdir": f"{source}/beaverwolt", "dir": f"{source}/beaverwolt",
        "target": {"wolt_id": "beaverwolt", "canonical_workdir": f"{source}/beaverwolt"},
        "runtime": {"tmux_session_name": "beaverwolt-s1", "pane_id": "%7", "kind": "tmux"},
        "harness_session_id": "resume-me",
    }
    write("beaverwolt/.state/sessions/beaverwolt-s1.json", session)
    write("beaverwolt/.state/sessions/beaverwolt-repo.json", {
        **session, "name": "beaverwolt-repo", "workdir": "/elsewhere/repo", "dir": "/elsewhere/repo",
        "target": {"wolt_id": "beaverwolt", "canonical_workdir": "/elsewhere/repo"},
    })
    write("beaverwolt/.state/sessions/beaverwolt-old.json", {
        **session, "name": "beaverwolt-old", "status": "stopped",
        "workdir": "/workspace/wolts/beaverwolt", "dir": "/workspace/wolts/beaverwolt",
        "target": {"wolt_id": "beaverwolt", "canonical_workdir": "/workspace/wolts/beaverwolt"},
    })
    write(".space/auto-grants.json", {"grants": [
        {"wolt_id": "beaverwolt", "canonical_workdir": f"{source}/beaverwolt", "policy_version": 1},
        {"wolt_id": "beaverwolt", "canonical_workdir": "/elsewhere/repo", "policy_version": 1},
    ]})


def test_restore_disarms_the_copy(wolts, tmp_path):
    _lodge_runtime(wolts, os.getpid())
    archive = create_backup(wolts, out_dir=tmp_path / "out").archive
    restored = restore_backup(archive, to=tmp_path / "back")
    copy = restored.wolts_dir

    for rel in (".space/platform/control-plane.json", ".space/platform/tunnel.json",
                ".space/platform/connectors.json", ".space/apps/demo.json"):
        assert not (copy / rel).exists(), rel
    session = json.loads((copy / "beaverwolt/.state/sessions/beaverwolt-s1.json").read_text())
    assert session["status"] == "stopped"
    assert "runtime" not in session
    assert session["workdir"] == session["dir"] == str(copy / "beaverwolt")
    assert session["target"]["canonical_workdir"] == str(copy / "beaverwolt")
    assert session["harness_session_id"] == "resume-me"
    # The container era saw the same lodge at /workspace/wolts: re-pointed, not set aside.
    old = json.loads((copy / "beaverwolt/.state/sessions/beaverwolt-old.json").read_text())
    assert old["dir"] == old["target"]["canonical_workdir"] == str(copy / "beaverwolt")
    # Worked outside the lodge: set aside, not re-pointed, not deleted.
    assert not (copy / "beaverwolt/.state/sessions/beaverwolt-repo.json").exists()
    aside = restored.target / backup_mod.QUARANTINE_NAME / "beaverwolt/.state/sessions/beaverwolt-repo.json"
    assert json.loads(aside.read_text())["harness_session_id"] == "resume-me"
    grants = json.loads((copy / ".space/auto-grants.json").read_text())["grants"]
    assert [g["canonical_workdir"] for g in grants] == [str(copy / "beaverwolt")]
    # The source is untouched.
    assert json.loads((wolts / ".space/apps/demo.json").read_text())["pid"] == os.getpid()
    lines = backup_mod.restore_lines(restored)
    assert any(line.startswith("disarmed: 1 app(s) and 2 session(s)") for line in lines)
    assert any(line.startswith("set aside: 1 session(s)") for line in lines)
    assert any("WOLTSPACE_PORT=" in line for line in lines)
    assert backup_mod.restored_tmux_dir(copy) == restored.target / "tmux"
    assert backup_mod.restored_tmux_dir(wolts) is None


def test_restore_refuses_a_link_where_it_must_disarm(tmp_path):
    outside = tmp_path / "live" / ".space"
    (outside / "apps").mkdir(parents=True)
    (outside / "apps" / "demo.json").write_text('{"pid": 1}')

    def build(tar):
        _add_bytes(tar, f"{ARCHIVE_ROOT}/w/wolt/wolt.json", b"{}")
        _add_symlink(tar, f"{ARCHIVE_ROOT}/.space", str(outside))
        _add_bytes(tar, MANIFEST_NAME, json.dumps({"entries": 2, "source": "/src"}).encode())

    archive = _write_archive(tmp_path / "evil.tar.gz", build)
    with pytest.raises(ValueError, match="symlink where a restore must disarm"):
        restore_backup(archive, to=tmp_path / "back")
    assert (outside / "apps" / "demo.json").is_file()
    assert not (tmp_path / "back" / ARCHIVE_ROOT).exists()


def test_disarm_fails_closed_through_a_link(tmp_path):
    outside = tmp_path / "live"
    (outside / "apps").mkdir(parents=True)
    (outside / "apps" / "demo.json").write_text('{"pid": 1}')
    copy = tmp_path / "back" / "wolts"
    copy.mkdir(parents=True)
    (copy / ".space").symlink_to(outside)
    with pytest.raises(ValueError, match="through a symlink"):
        backup_mod.disarm(copy, "/source", tmp_path / "back")
    assert (outside / "apps" / "demo.json").is_file()


@pytest.mark.skipif(not __import__("shutil").which("tmux"), reason="needs tmux")
def test_a_copy_beside_its_source_only_touches_itself(wolts, tmp_path):
    """Same machine, same session names, started with the source's tmux in its
    environment: the copy's boot, wake and stop still stay in the copy."""
    import shutil
    import subprocess
    import tempfile

    app = subprocess.Popen(["sleep", "300"])  # the source's live app
    short = Path(tempfile.mkdtemp(prefix="wsb-", dir="/tmp"))  # socket paths are capped
    source_tmux = short / "src"
    source_tmux.mkdir()
    source_socket = source_tmux / f"tmux-{os.getuid()}" / "default"

    def tmux(*args: str, where: Path = source_tmux) -> subprocess.CompletedProcess:
        env = {**os.environ, "TMUX_TMPDIR": str(where)}
        env.pop("TMUX", None)
        return subprocess.run(["tmux", *args], env=env, capture_output=True, text=True)

    def in_copy(code: str) -> str:
        # The worst case: launched from a pane of the source's tmux server.
        env = {**os.environ, "TMUX_TMPDIR": str(source_tmux), "TMUX": f"{source_socket},1,0",
               "WOLTSPACE_WOLTS_DIR": str(copy)}
        lib = ROOT / "container" / "lib"
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path[:0] = [{str(lib)!r}]\n{code}"],
                             env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        return out.stdout

    try:
        _lodge_runtime(wolts, app.pid)
        assert tmux("new-session", "-d", "-s", "beaverwolt-s1", "sleep 300").returncode == 0
        archive = create_backup(wolts, out_dir=tmp_path / "out").archive
        restored = restore_backup(archive, to=short / "back")
        copy = restored.wolts_dir
        own_tmux = restored.target / "tmux"

        # Boot: the copy adopts nothing of the source's.
        report = json.loads(in_copy(
            "import json, sessions\nprint(json.dumps(sessions.SessionRegistry().adopt_runtime_sessions()))"
        ))
        assert not report["adopted"]
        # Stop in the copy: its app has no state, its session has no tmux of its own.
        out = in_copy("import apps, sessions\nprint(apps.stop_app('demo'))\n"
                      "print(sessions.stop_session('beaverwolt-s1')['was_alive'])")
        assert out.split() == ["False", "False"]
        # Wake in the copy (same name), then stop it: it lands on, and leaves, the copy's own server.
        in_copy("import subprocess, paths\n"
                "subprocess.run(['tmux', 'new-session', '-d', '-s', 'beaverwolt-s1', 'sleep 300'], check=True)")
        assert tmux("has-session", "-t", "=beaverwolt-s1", where=own_tmux).returncode == 0
        assert in_copy("import sessions\nprint(sessions.stop_session('beaverwolt-s1')['was_alive'])").split() == ["True"]
        assert tmux("has-session", "-t", "=beaverwolt-s1", where=own_tmux).returncode != 0

        # The source never noticed.
        assert tmux("has-session", "-t", "=beaverwolt-s1").returncode == 0
        assert app.poll() is None
    finally:
        tmux("kill-session", "-t", "=beaverwolt-s1")
        tmux("kill-session", "-t", "=beaverwolt-s1", where=short / "back" / "tmux")
        app.kill()
        app.wait()
        shutil.rmtree(short, ignore_errors=True)

# ---------------------------------------------------------------------------
# The lodge database: one consistent snapshot, never the live sidecars
# ---------------------------------------------------------------------------

def _live_lodge_db(wolts: Path, rows: int = 3):
    """A lodge.db as a running lodge leaves it: WAL mode, rows still in the -wal."""
    import sqlite3

    db = wolts / ".space" / "lodge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE sessions (name TEXT PRIMARY KEY, record TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO sessions VALUES (?, '{}')", (f"pal-{i}",))
    assert (wolts / ".space" / "lodge.db-wal").stat().st_size > 0
    return conn


def test_the_lodge_db_goes_in_as_one_consistent_snapshot(wolts, tmp_path):
    import sqlite3

    live = _live_lodge_db(wolts)
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out")
    finally:
        live.close()
    names = _members(result.archive)
    assert f"{ARCHIVE_ROOT}/.space/lodge.db" in names
    assert f"{ARCHIVE_ROOT}/.space/lodge.db-wal" not in names
    assert f"{ARCHIVE_ROOT}/.space/lodge.db-shm" not in names
    assert result.manifest["lodge_db"]["tables"] == {"meta": 0, "sessions": 3}

    restored = restore_backup(result.archive, to=tmp_path / "back")
    db = restored.wolts_dir / ".space" / "lodge.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert conn.execute("SELECT count(*) FROM sessions").fetchone() == (3,)
    finally:
        conn.close()


def test_the_backup_folds_the_wal_and_still_restores(wolts, tmp_path):
    import sqlite3

    live = _live_lodge_db(wolts, rows=500)
    wal = wolts / ".space" / "lodge.db-wal"
    try:
        assert wal.stat().st_size > 100_000
        result = create_backup(wolts, out_dir=tmp_path / "out")
        assert wal.stat().st_size == 0
    finally:
        live.close()
    db = restore_backup(result.archive, to=tmp_path / "back").wolts_dir / ".space" / "lodge.db"
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone() == (500,)
    finally:
        conn.close()


def test_a_busy_checkpoint_is_one_line_and_the_backup_goes_on(wolts, tmp_path, capsys):
    live = _live_lodge_db(wolts)
    try:
        live.execute("BEGIN")
        live.execute("SELECT count(*) FROM sessions").fetchone()  # a reader pins the WAL
        result = create_backup(wolts, out_dir=tmp_path / "out")
    finally:
        live.close()
    assert result.manifest["lodge_db"]["tables"]["sessions"] == 3
    lines = [line for line in capsys.readouterr().err.splitlines() if "checkpoint" in line]
    assert len(lines) == 1, lines


def test_no_lodge_db_means_no_lodge_db_in_the_manifest(wolts, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert "lodge_db" not in result.manifest
    assert result.verified


def test_an_apps_own_sqlite_wal_is_kept(wolts, tmp_path):
    """Only the lodge's sidecars go: an app's -wal can hold committed rows."""
    app = wolts / "beaverwolt" / "wolt" / "apps" / "shop"
    app.mkdir(parents=True)
    for name in ("shop.db", "shop.db-wal", "shop.db-shm"):
        (app / name).write_bytes(b"rows")
    names = _members(create_backup(wolts, out_dir=tmp_path / "out").archive)
    for name in ("shop.db", "shop.db-wal", "shop.db-shm"):
        assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/apps/shop/{name}" in names


def test_a_lodge_db_that_cannot_be_snapshotted_fails_the_backup(wolts, tmp_path):
    """A backup without the lodge's state in it is not a backup."""
    (wolts / ".space" / "lodge.db").write_bytes(b"not a database at all" * 100)
    with pytest.raises(ValueError, match="lodge.db"):
        create_backup(wolts, out_dir=tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_cli_reports_an_unsnapshottable_lodge_db_without_traceback(wolts, tmp_path, capsys):
    (wolts / ".space" / "lodge.db").write_bytes(b"not a database at all" * 100)
    code = cli_main(["backup", "--wolts-dir", str(wolts), "--out", str(tmp_path / "out")])
    assert code == 1
    captured = capsys.readouterr()
    assert "backup failed" in captured.out
    assert "lodge.db" in captured.out
    assert "Traceback" not in captured.out + captured.err


def test_a_lodge_db_the_scan_cannot_read_fails_before_writing(wolts, tmp_path):
    os.mkfifo(wolts / ".space" / "lodge.db")
    with pytest.raises(ValueError, match="lodge.db"):
        create_backup(wolts, out_dir=tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_an_unlistable_space_dir_fails_before_writing(wolts, tmp_path, monkeypatch):
    """`.space` unreadable: the scan can't say whether a lodge.db is there."""
    space = (wolts / ".space").resolve()  # create_backup walks the resolved path
    real_scandir = os.scandir

    def scandir(path="."):
        if Path(path) == space:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    with pytest.raises(ValueError, match="lodge.db"):
        create_backup(wolts, out_dir=tmp_path / "out")
    assert list((tmp_path / "out").iterdir()) == []


def test_an_ordinary_file_vanishing_mid_write_stays_a_warning(wolts, tmp_path, monkeypatch):
    gone = wolts / "beaverwolt" / "wolt" / "memory" / "identity.md"
    real_add = backup_mod._add_entry

    def add_entry(tar, entry):
        if entry.path == gone:
            gone.unlink()
        return real_add(tar, entry)

    monkeypatch.setattr(backup_mod, "_add_entry", add_entry)
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert result.verified
    assert "beaverwolt/wolt/memory/identity.md" in {
        item["path"] for item in result.manifest["unreadable"]
    }
    assert f"{ARCHIVE_ROOT}/beaverwolt/wolt/memory/identity.md" not in _members(result.archive)


@pytest.mark.parametrize("rel", [".space/pal.pipe", "beaverwolt/.space/lodge.db"])
def test_only_the_lodges_own_db_is_required(wolts, tmp_path, rel):
    (wolts / rel).parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(wolts / rel)
    result = create_backup(wolts, out_dir=tmp_path / "out")
    assert rel in {item["path"] for item in result.manifest["unreadable"]}
    assert result.verified


# ---------------------------------------------------------------------------
# A recipient set: the whole archive goes through age, secrets inside
# ---------------------------------------------------------------------------

RECIPIENT = "age1pal0000000000000000000000000000000000000000000000000000000"
FAKE_KEY = b"AGE-SECRET-KEY-1PALFAKE\n"
AGE_HEADER = b"age-encryption.org/v1\n"


@pytest.fixture
def fake_age(tmp_path, monkeypatch):
    """An `age` on PATH that base64s instead of encrypting, and decrypts back.

    Every call is logged to `calls` (with the mode of the file it writes to),
    so a test can see how age was driven. `swap` makes the second decryption
    hand back a different archive. The real round trip is a test of its own.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "age"
    script.write_text(
        f"#!{sys.executable}\n"
        "import base64, json, os, sys\n"
        f"HEADER, KEY, LOG = {AGE_HEADER!r}, {FAKE_KEY!r}, {str(bin_dir / 'calls')!r}\n"
        "args = sys.argv[1:]\n"
        "call = {'args': args, 'out_mode': oct(os.fstat(1).st_mode & 0o777)}\n"
        "open(LOG, 'a').write(json.dumps(call) + '\\n')\n"
        "if '-d' in args:\n"
        "    if sys.stdin.buffer.read() != KEY:\n"
        "        sys.exit('age: error: no identity matched any of the recipients')\n"
        "    data = open(args[-1], 'rb').read()\n"
        "    swap = args[-1] + '.swap'\n"
        "    if os.path.exists(swap) and sum('-d' in l for l in open(LOG)) > 1:\n"
        "        data = open(swap, 'rb').read()\n"
        "    sys.stdout.buffer.write(base64.b64decode(data[len(HEADER):]))\n"
        "    sys.exit(0)\n"
        "if args[args.index('-r') + 1] == 'age1broken':\n"
        "    sys.exit('age: error: malformed recipient')\n"
        "sys.stdout.buffer.write(HEADER + base64.b64encode(sys.stdin.buffer.read()))\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("WOLTSPACE_BACKUP_AGE_RECIPIENT", RECIPIENT)
    return bin_dir


def _age_calls(fake_age: Path) -> list[dict]:
    log = fake_age / "calls"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def _fake_encrypt(plain: bytes) -> bytes:
    import base64
    return AGE_HEADER + base64.b64encode(plain)


def _fake_open(path: Path) -> dict[str, bytes]:
    """The members of a fake-encrypted archive, by name."""
    import base64
    import io as io_mod

    data = path.read_bytes()
    assert data.startswith(AGE_HEADER)
    files = {}
    with tarfile.open(fileobj=io_mod.BytesIO(base64.b64decode(data[len(AGE_HEADER):]))) as tar:
        for member in tar.getmembers():
            files[member.name] = tar.extractfile(member).read() if member.isreg() else b""
    return files


def test_a_recipient_encrypts_the_whole_archive_secrets_inside(
    wolts_with_credentials, fake_age, tmp_path,
):
    (wolts_with_credentials / ".space" / "platform" / "config.json").write_text(
        json.dumps({"telegram": {"token": SECRET}})
    )
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out", tag="t1")
    assert result.archive == tmp_path / "out" / "woltspace-backup-t1.tar.gz.age"
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [result.archive.name]
    assert SECRET.encode() not in result.archive.read_bytes()

    (call,) = _age_calls(fake_age)
    assert call["args"] == ["-r", RECIPIENT]  # no -o: age writes into our 0600 fd
    assert call["out_mode"] == "0o600"

    files = _fake_open(result.archive)
    for rel in (".env", ".space/platform/config.json", "beaverwolt/wolt/apps/shop/.env",
                "beaverwolt/wolt/apps/shop/.env.local", "beaverwolt/wolt/apps/shop/.env.production"):
        assert SECRET.encode() in files[f"{ARCHIVE_ROOT}/{rel}"], rel
    # OAuth chains stay withheld forever, encrypted or not.
    assert not [name for name in files if "credentials.json" in name or "auth.json" in name]
    manifest = json.loads(files[MANIFEST_NAME])
    assert manifest["secrets"] == {"present": True, "recipient": RECIPIENT}
    assert manifest["entries"] == len(files) - 1
    report = "\n".join(backup_mod.summary_lines(result))
    assert f"encrypted to {RECIPIENT}" in report


def test_no_plaintext_touches_the_disk(wolts, fake_age, tmp_path, monkeypatch):
    import tempfile

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    live = _live_lodge_db(wolts)
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out", tag="t2")
    finally:
        live.close()
    assert list(scratch.iterdir()) == []
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [result.archive.name]
    assert result.manifest["lodge_db"]["tables"] == {"meta": 0, "sessions": 3}


def test_without_a_recipient_the_archive_is_plain_and_secrets_withheld(
    wolts, fake_age, tmp_path, monkeypatch,
):
    monkeypatch.delenv("WOLTSPACE_BACKUP_AGE_RECIPIENT")
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="t3")
    assert result.archive.name == "woltspace-backup-t3.tar.gz"
    assert _age_calls(fake_age) == []
    secrets = result.manifest["secrets"]
    assert secrets["present"] is False and "not set" in secrets["reason"]
    assert ".env" in {item["path"] for item in result.manifest["withheld"]["paths"]}
    assert f"{ARCHIVE_ROOT}/.env" not in _members(result.archive)


def test_a_recipient_without_age_fails_rather_than_write_in_the_clear(
    wolts, fake_age, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    code = cli_main([
        "backup", "--wolts-dir", str(wolts), "--out", str(tmp_path / "out"), "--tag", "t4",
    ])
    assert code == 1
    assert "age is not on PATH" in capsys.readouterr().out
    assert not list((tmp_path / "out").glob("*t4*"))


def test_an_age_failure_fails_the_backup_and_leaves_nothing(
    wolts, fake_age, tmp_path, monkeypatch, capsys,
):
    monkeypatch.setenv("WOLTSPACE_BACKUP_AGE_RECIPIENT", "age1broken")
    code = cli_main([
        "backup", "--wolts-dir", str(wolts), "--out", str(tmp_path / "out"), "--tag", "t5",
    ])
    assert code == 1
    out = capsys.readouterr().out
    assert "backup failed" in out and "malformed recipient" in out
    assert not list((tmp_path / "out").glob("*t5*"))


@pytest.mark.parametrize("recipient", [True, False])
def test_backup_files_are_owner_only(wolts, fake_age, tmp_path, monkeypatch, recipient):
    """Whatever the umask or an older file's mode, nobody but the owner reads a snapshot."""
    if not recipient:
        monkeypatch.delenv("WOLTSPACE_BACKUP_AGE_RECIPIENT")
    suffix = ".tar.gz.age" if recipient else ".tar.gz"
    stale = tmp_path / "out" / f"woltspace-backup-t7{suffix}"
    stale.parent.mkdir()
    stale.write_bytes(b"older run")
    stale.chmod(0o644)
    old = os.umask(0o022)
    try:
        create_backup(wolts, out_dir=tmp_path / "out", tag="t6")
        create_backup(wolts, out_dir=tmp_path / "out", tag="t7")
    finally:
        os.umask(old)
    for tag in ("t6", "t7"):
        path = tmp_path / "out" / f"woltspace-backup-{tag}{suffix}"
        assert (path.stat().st_mode & 0o777) == 0o600, path.name


def test_a_symlinked_env_is_not_followed_into_the_archive(wolts, fake_age, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "id_key"
    elsewhere.parent.mkdir()
    elsewhere.write_text("not-a-dotenv-0123456789")
    (wolts / "beaverwolt" / ".env").symlink_to(elsewhere)
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="t8")

    files = _fake_open(result.archive)
    assert f"{ARCHIVE_ROOT}/.env" in files
    assert f"{ARCHIVE_ROOT}/beaverwolt/.env" not in files
    assert all(b"not-a-dotenv" not in data for data in files.values())
    skipped = {item["path"]: item["error"] for item in result.manifest["unreadable"]}
    assert "symlink" in skipped["beaverwolt/.env"]
    restored = restore_backup(result.archive, to=tmp_path / "back", identity=FAKE_KEY)
    assert "beaverwolt/.env" in "\n".join(backup_mod.restore_lines(restored))


def test_an_encrypted_restore_brings_the_secrets_back(wolts_with_credentials, fake_age, tmp_path):
    result = create_backup(wolts_with_credentials, out_dir=tmp_path / "out", tag="t10")
    restored = restore_backup(result.archive, to=tmp_path / "back", identity=FAKE_KEY)
    assert SECRET in (restored.wolts_dir / ".env").read_text()
    assert SECRET in (restored.wolts_dir / "beaverwolt/wolt/apps/shop/.env.local").read_text()
    assert not (restored.wolts_dir / ".claude" / ".credentials.json").exists()
    assert (restored.wolts_dir / "beaverwolt" / "wolt" / "wolt.json").is_file()
    assert restored.target == tmp_path / "back"
    out = "\n".join(backup_mod.restore_lines(restored))
    assert "claude login" in out and "copy .env.example" not in out


def test_restore_of_an_encrypted_archive_wants_the_key(wolts, fake_age, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="t11")
    with pytest.raises(ValueError, match="--identity"):
        restore_backup(result.archive, to=tmp_path / "back")
    assert not (tmp_path / "back").exists()
    with pytest.raises(OSError, match="no identity matched"):
        restore_backup(result.archive, to=tmp_path / "back", identity=b"AGE-SECRET-KEY-1WRONG\n")
    assert list((tmp_path / "back").iterdir()) == []


def test_an_encrypted_archive_is_audited_before_a_byte_lands(fake_age, tmp_path):
    plain = tmp_path / "plain.tar.gz"
    _write_archive(plain, lambda tar: (
        _add_bytes(tar, f"{ARCHIVE_ROOT}/fine.txt"),
        _add_bytes(tar, "../escaped.txt"),
    ))
    sealed = tmp_path / "evil.tar.gz.age"
    sealed.write_bytes(_fake_encrypt(plain.read_bytes()))
    with pytest.raises(Exception):
        restore_backup(sealed, to=tmp_path / "back", identity=FAKE_KEY)
    assert list((tmp_path / "back").iterdir()) == []
    assert not (tmp_path / "escaped.txt").exists()
    assert sum("-d" in call["args"] for call in _age_calls(fake_age)) == 1  # no extraction pass


def test_an_archive_that_changes_between_the_passes_is_refused(wolts, fake_age, tmp_path):
    result = create_backup(wolts, out_dir=tmp_path / "out", tag="t12")
    other = tmp_path / "other.tar.gz"
    _write_archive(other, lambda tar: _add_bytes(tar, f"{ARCHIVE_ROOT}/planted.txt"))
    Path(f"{result.archive}.swap").write_bytes(_fake_encrypt(other.read_bytes()))
    with pytest.raises(ValueError, match="changed between"):
        restore_backup(result.archive, to=tmp_path / "back", identity=FAKE_KEY)
    assert not (tmp_path / "back" / ARCHIVE_ROOT / "planted.txt").exists()


@pytest.mark.parametrize("from_stdin", [False, True])
def test_cli_restore_takes_the_key_from_a_file_or_stdin(
    wolts, fake_age, tmp_path, monkeypatch, capsys, from_stdin,
):
    import io as io_mod

    result = create_backup(wolts, out_dir=tmp_path / "out", tag="t13")
    if from_stdin:
        monkeypatch.setattr(sys, "stdin", io_mod.TextIOWrapper(io_mod.BytesIO(FAKE_KEY)))
        identity = "-"
    else:
        (tmp_path / "key.txt").write_bytes(FAKE_KEY)
        identity = str(tmp_path / "key.txt")
    code = cli_main(["restore", str(result.archive), "--to", str(tmp_path / "back"),
                     "--identity", identity])
    assert code == 0
    assert SECRET in (tmp_path / "back" / ARCHIVE_ROOT / ".env").read_text()
    assert "WOLTSPACE_WOLTS_DIR=" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["no recipient", "plaintext archive", "encrypted"])
def test_the_documented_upload_never_sends_plaintext(tmp_path, case):
    """The upload script in docs/backup.md, run against stand-in woltspace and rclone."""
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    suffix = ".tar.gz" if case == "plaintext archive" else ".tar.gz.age"
    (bin_dir / "woltspace").write_text(
        "#!/bin/sh\n"
        'if [ "$1" = --version ]; then echo "woltspace 0.0.0"; exit 0; fi\n'
        f'mkdir -p "$5" && printf x > "$5/woltspace-backup-$3{suffix}"\n'
    )
    (bin_dir / "rclone").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path / "rclone.log"}\n')
    for tool in ("woltspace", "rclone"):
        (bin_dir / tool).chmod(0o755)
    env = {"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin"}
    if case != "no recipient":
        env["WOLTSPACE_BACKUP_AGE_RECIPIENT"] = RECIPIENT
    run = subprocess.run(["bash", "-c", _doc_script("**The daily upload.**")], env=env,
                         capture_output=True, text=True)
    uploads = (tmp_path / "rclone.log").read_text() if (tmp_path / "rclone.log").exists() else ""
    if case == "encrypted":
        assert run.returncode == 0, run.stderr
        assert ".tar.gz.age" in uploads and "latest.json" in uploads
        assert not [line for line in uploads.splitlines() if line.split()[1].endswith(".tar.gz")]
        latest = json.loads((tmp_path / ".woltspace" / "backups" / "latest.json").read_text())
        assert sorted(latest) == ["sha256", "tag", "taken_at", "version"]
    else:
        assert run.returncode != 0
        assert uploads == ""
        assert "unencrypted" in run.stderr or "WOLTSPACE_BACKUP_AGE_RECIPIENT" in run.stderr


def _doc_script(after: str) -> str:
    text = (ROOT / "docs" / "backup.md").read_text()
    return text.split(after, 1)[1].split("```bash\n", 1)[1].split("```", 1)[0]


@pytest.mark.parametrize("case", ["ok", "latest points back", "sha mismatch", "plaintext", "stale"])
def test_the_documented_weekly_check_needs_no_key(tmp_path, case):
    """The on-lodge check in docs/backup.md, against a directory standing in for the bucket."""
    import hashlib
    import subprocess
    from datetime import datetime, timedelta, timezone

    bucket = tmp_path / "bucket" / "lodges" / "<lodge>"
    (bucket / "snapshots").mkdir(parents=True)
    for tag in ("20260101-000000", "20260102-000000"):
        body = (b"PK not age\n" if case == "plaintext" else AGE_HEADER) + os.urandom(1_000_000)
        (bucket / "snapshots" / f"woltspace-backup-{tag}.tar.gz.age").write_bytes(body)
    newest = bucket / "snapshots" / "woltspace-backup-20260102-000000.tar.gz.age"
    hours = 72 if case == "stale" else 1
    taken = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    latest = {"tag": "20260102-000000", "taken_at": taken,
              "sha256": hashlib.sha256(newest.read_bytes()).hexdigest()}
    if case == "latest points back":
        latest["tag"] = "20260101-000000"
    if case == "sha mismatch":
        latest["sha256"] = "0" * 64
    (bucket / "latest.json").write_text(json.dumps(latest))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(
        f"#!{sys.executable}\n"
        "import fnmatch, os, shutil, sys\n"
        f"ROOT = {str(tmp_path / 'bucket')!r}\n"
        "local = lambda r: os.path.join(ROOT, r.split('/', 1)[1])\n"
        "verb, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "if verb == 'copyto':\n"
        "    shutil.copy(local(src), dst)\n"
        "else:\n"
        "    pattern = sys.argv[sys.argv.index('--include') + 1]\n"
        "    print('\\n'.join(n for n in sorted(os.listdir(local(src))) if fnmatch.fnmatch(n, pattern)))\n"
    )
    (bin_dir / "rclone").chmod(0o755)
    run = subprocess.run(
        ["bash", "-c", _doc_script("**Weekly, on the lodge, no key.**")],
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}:/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        capture_output=True, text=True,
    )
    if case == "ok":
        assert run.returncode == 0, run.stderr
        assert "backup check ok" in run.stdout
    else:
        assert run.returncode != 0
        assert "backup check FAILED" in run.stderr
        if case == "stale":
            assert "72 hours old" in run.stderr


def test_the_documented_monthly_check_boots_without_secrets_or_crons(tmp_path):
    """The by-hand restore check: every secret gone, then a lodge with nothing live."""
    import subprocess

    wolts = tmp_path / "restore-check" / "wolts"
    secrets = [".env", ".env.local", "pal/wolt/apps/shop/.env", "pal/wolt/apps/shop/.env.production",
               ".space/platform/config.json"]
    kept = [".env.example", "pal/wolt/apps/shop/.env.sample", "pal/wolt/memory/identity.md"]
    for rel in secrets + kept:
        (wolts / rel).parent.mkdir(parents=True, exist_ok=True)
        (wolts / rel).write_text(SECRET)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "woltspace").write_text(f'#!/bin/sh\nenv > {tmp_path / "boot.env"}\necho "$@" >> {tmp_path / "boot.env"}\n')
    (bin_dir / "woltspace").chmod(0o755)
    block = _doc_script("**Delete every restored secret").replace("/tmp/restore-check", str(tmp_path / "restore-check"))
    run = subprocess.run(["bash", "-c", block], env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert [rel for rel in secrets if (wolts / rel).exists()] == []
    assert all((wolts / rel).exists() for rel in kept)
    boot = (tmp_path / "boot.env").read_text()
    for switch in ("ENABLE_TELEGRAM_BOT=false", "ENABLE_SLACK_BOT=false", "WOLTSPACE_WOLF=false",
                   "WOLTSPACE_VULTURE=false", "WOLTSPACE_PUBLIC_TUNNEL=false"):
        assert switch in boot.splitlines()
    assert "serve --port 7778" in boot


@pytest.mark.skipif(
    not (__import__("shutil").which("age") and __import__("shutil").which("age-keygen")),
    reason="age is not installed",
)
def test_real_age_round_trip(wolts, tmp_path, monkeypatch):
    import subprocess

    key = tmp_path / "key.txt"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = subprocess.run(
        ["age-keygen", "-y", str(key)], check=True, capture_output=True, text=True,
    ).stdout.strip()
    monkeypatch.setenv("WOLTSPACE_BACKUP_AGE_RECIPIENT", recipient)
    live = _live_lodge_db(wolts)
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out", tag="t14")
    finally:
        live.close()
    raw = result.archive.read_bytes()
    assert raw.startswith(AGE_HEADER)
    assert SECRET.encode() not in raw and b"beaverwolt" not in raw

    restored = restore_backup(result.archive, to=tmp_path / "back", identity=key.read_bytes())
    assert SECRET in (restored.wolts_dir / ".env").read_text()
    assert (restored.wolts_dir / ".space" / "lodge.db").is_file()
    assert restored.manifest["secrets"]["present"] is True


def test_every_lodge_db_row_goes_into_the_snapshot(wolts, tmp_path):
    """Chat routing, the Slack queue, running apps and consents travel too."""
    live = _live_lodge_db(wolts)
    marker = "pal-said-0xMARKER"
    live.execute("CREATE TABLE pending_messages (id TEXT PRIMARY KEY, text TEXT)")
    live.execute("CREATE TABLE chat_state (chat_id TEXT PRIMARY KEY, record TEXT)")
    live.execute("CREATE TABLE grants (scope TEXT PRIMARY KEY, record TEXT)")
    live.execute("CREATE TABLE apps_running (name TEXT PRIMARY KEY, pid INTEGER,"
                 " tunnel_pid INTEGER, tunnel_url TEXT, record TEXT)")
    for table, row in (("pending_messages", ("m1", marker)),
                       ("chat_state", ("c1", "{}")),
                       ("grants", ("/home/pal/nunu", "{}")),
                       ("apps_running", ("shop", 1, None, None, "{}"))):
        live.execute(f"INSERT INTO {table} VALUES ({', '.join('?' * len(row))})", row)
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out")
    finally:
        live.close()

    assert result.manifest["lodge_db"] == {"tables": {
        "apps_running": 1, "chat_state": 1, "grants": 1, "meta": 0,
        "pending_messages": 1, "sessions": 3,
    }}
    with tarfile.open(result.archive, "r:gz") as tar:
        snapshot = tar.extractfile(f"{ARCHIVE_ROOT}/.space/lodge.db").read()
    assert marker.encode() in snapshot

    restored = restore_backup(result.archive, to=tmp_path / "back")
    assert ("consents came back with the lodge: review them with woltspace auto list"
            in "\n".join(backup_mod.restore_lines(restored)))


def test_a_restored_app_row_forgets_its_process_ids(wolts, tmp_path):
    """On another machine an old pid may be any live process: the app must be
    started again, never taken as running, never signalled through it."""
    import sqlite3

    live = _live_lodge_db(wolts)
    live.execute("CREATE TABLE apps_running (name TEXT PRIMARY KEY, pid INTEGER, port INTEGER,"
                 " tunnel_pid INTEGER, tunnel_url TEXT, record TEXT)")
    state = {"pid": os.getpid(), "port": 8100, "keeper": "pal", "tunnel_pid": os.getpid(),
             "tunnel_url": "https://x.example"}
    live.execute("INSERT INTO apps_running VALUES ('shop', ?, 8100, ?, 'https://x.example', ?)",
                 (os.getpid(), os.getpid(), json.dumps(state)))
    try:
        result = create_backup(wolts, out_dir=tmp_path / "out")
    finally:
        live.close()

    restored = restore_backup(result.archive, to=tmp_path / "back")
    conn = sqlite3.connect(restored.wolts_dir / ".space" / "lodge.db")
    try:
        row = conn.execute("SELECT pid, port, tunnel_pid, tunnel_url, record FROM apps_running").fetchone()
    finally:
        conn.close()
    # The lodge reads its app state from the record, so the pids go there too.
    assert row[:4] == (None, 8100, None, None)
    assert json.loads(row[4]) == {"port": 8100, "keeper": "pal"}


def test_runtime_state_goes_into_the_archive(wolts, tmp_path):
    """Logs, chat history and routing, spools, consents, and the *.imported
    copies lodge.db's importers leave behind: all of it is data now."""
    paths = (
        ".space/logs/control-plane.log",
        ".space/outbox/abc123",
        ".space/telegram/history/42.jsonl",
        ".space/telegram/chats.imported/42.json",
        ".space/auto-grants.json.imported",
        ".space/apps/shop.json",
        "pal/.state/chat/slack/_pending_messages.json",
    )
    for rel in paths:
        (wolts / rel).parent.mkdir(parents=True, exist_ok=True)
        (wolts / rel).write_text("x")

    result = create_backup(wolts, out_dir=tmp_path / "out")
    names = set(_members(result.archive))
    assert [rel for rel in paths if f"{ARCHIVE_ROOT}/{rel}" not in names] == []
    assert "ephemeral" not in result.manifest["excludes"]
