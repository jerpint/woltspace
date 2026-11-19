"""Session parts off the machine: the kept (gzipped) files under age, opaque names, sent only once the
upload is confirmed, a full round trip back, and the bucket lock check."""

import base64
import json
import os
import sys
from pathlib import Path

import pytest
from harnesses import HARNESSES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from woltspace import session_archive as sa
from woltspace.cli import main as cli_main

CLAUDE = "wolt-a/.claude/projects/-w/s1.jsonl"
AGE_HEADER = b"age-encryption.org/v1\n"


@pytest.fixture
def fake_age(tmp_path, monkeypatch):
    """An age on PATH that base64s stdin behind the real header instead of encrypting."""
    bin_dir = tmp_path / "age-bin"
    bin_dir.mkdir()
    (bin_dir / "age").write_text(f"""#!{sys.executable}
import base64, sys
sys.stdout.buffer.write({AGE_HEADER!r} + base64.b64encode(sys.stdin.buffer.read()))
""")
    (bin_dir / "age").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("WOLTSPACE_BACKUP_AGE_RECIPIENT", "age1pal")
    return bin_dir


def _lodge(tmp_path):
    root = tmp_path / "wolts"
    (root / "wolt-a" / "wolt").mkdir(parents=True)
    (root / "wolt-a" / "wolt" / "wolt.json").write_text("{}")
    (root / "woltspace.json").write_text(json.dumps({"sessions": {"keep_days": None}}))
    return root


def _grow(root, lines):
    path = root / CLAUDE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        handle.writelines(json.dumps({"turn": i}) + "\n" for i in lines)
    os.utime(path, (lines[-1], 1_700_000_000 + lines[-1]))
    sa.refresh(root, HARNESSES)
    return path


def _open(path: Path) -> bytes:
    data = path.read_bytes()
    assert data.startswith(AGE_HEADER)  # never plaintext on disk
    return base64.b64decode(data[len(AGE_HEADER):])


def test_without_a_recipient_nothing_is_written_out(tmp_path, monkeypatch):
    root = _lodge(tmp_path)
    _grow(root, [0])
    monkeypatch.delenv("WOLTSPACE_BACKUP_AGE_RECIPIENT", raising=False)
    with pytest.raises(ValueError, match="never written out in plaintext"):
        sa.write_out(root, tmp_path / "out")
    assert not (tmp_path / "out").exists()


def test_the_round_trip_archive_encrypt_decrypt_restore_is_byte_equal(tmp_path, fake_age):
    root = _lodge(tmp_path)
    for i in range(3):
        path = _grow(root, [i])
    out = tmp_path / "out"
    assert len(sa.write_out(root, out)) == 3
    names = [p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()]
    assert len(names) == 6 and all(len(n) < 200 and "wolt-a" not in n and "claude" not in n for n in names)
    plain = tmp_path / "plain"  # what the documented decrypt loop makes on the owner's machine
    for sealed in out.rglob("*.age"):  # decrypting each file gives back the archive's own layout
        target = plain / sealed.relative_to(out).as_posix().removesuffix(".age")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_open(sealed))
    _, refused = sa.restore(plain, tmp_path / "back")
    assert refused == [] and (tmp_path / "back" / "root" / CLAUDE).read_bytes() == path.read_bytes()


def test_only_unsent_parts_go_out_and_a_failed_upload_leaves_them_pending(tmp_path, fake_age):
    root = _lodge(tmp_path)
    _grow(root, [0])
    first = tmp_path / "night1"
    assert len(sa.write_out(root, first)) == 1
    before = {p: p.read_bytes() for p in first.rglob("*") if p.is_file()}
    assert len(sa.write_out(root, first)) == 1  # the upload failed: still pending, ...
    assert {p: p.read_bytes() for p in first.rglob("*") if p.is_file()} == before  # ... files already there untouched
    assert sa.mark_sent(root, first) == 1
    _grow(root, [1])
    assert len(sa.write_out(root, tmp_path / "night2")) == 1  # only the new part
    assert sa.write_out(root, tmp_path / "night3") != []  # night 2 not confirmed yet


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    """An rclone on PATH that plays one bucket answer, chosen by writing it to `mode`."""
    bin_dir = tmp_path / "rclone-bin"
    bin_dir.mkdir()
    (bin_dir / "rclone").write_text(f"""#!{sys.executable}
import sys
mode = open({str(bin_dir / 'mode')!r}).read().strip()
verb = sys.argv[1]
if verb == "lsf":
    if mode == "missing":
        sys.exit(0)
    if mode == "token":
        sys.exit("Failed to lsf: AccessDenied: Access Denied")
    print("lock-probe")
    sys.exit(0)
if mode == "on":
    sys.exit("Failed to deletefile: AccessDenied: Object Lock: the object is under a retention rule")
if mode == "account":
    sys.exit("Failed to deletefile: AccessDenied: account is locked; credentials rejected")
if mode == "named":  # rclone quotes the target: its name is not the storage's answer
    sys.exit(f"Failed to deletefile {{sys.argv[2]}}: AccessDenied: Access Denied")
if mode == "denied":  # a token that may list but not delete: not proof of a lock
    sys.exit("Failed to deletefile: AccessDenied: Access Denied")
if mode == "network":
    sys.exit("Failed to deletefile: dial tcp: i/o timeout")
sys.exit(0)
""")
    (bin_dir / "rclone").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bin_dir / "mode"


@pytest.mark.parametrize("mode, state, code", [
    ("on", "on", 0), ("off", "off", 1), ("missing", "unknown", 1), ("token", "unknown", 1), ("denied", "unknown", 1), ("account", "unknown", 1),
    ("network", "unknown", 1),
])
def test_check_lock_is_on_only_for_a_locked_refusal(tmp_path, fake_rclone, monkeypatch, mode, state, code):
    fake_rclone.write_text(mode)
    assert sa.check_lock("r2:bucket/lodges/pal")[0] == state
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(_lodge(tmp_path)))
    assert cli_main(["backup", "sessions", "check-lock", "--remote", "r2:bucket/lodges/pal"]) == code


def test_check_lock_without_rclone_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert sa.check_lock("r2:bucket/lodges/pal")[0] == "unknown"


def test_a_failed_age_leaves_no_final_named_file(tmp_path, fake_age):  # Gate B I2
    root = _lodge(tmp_path)
    _grow(root, [0])
    (fake_age / "age").write_text(f"#!{sys.executable}\nimport sys\nsys.stdin.buffer.read(10)\nsys.exit('age: error')\n")
    with pytest.raises(OSError, match="age"):
        sa.write_out(root, tmp_path / "out")
    assert [p.name for p in (tmp_path / "out").rglob("*") if p.is_file()] == []


def test_a_torn_file_from_a_killed_run_is_never_sent_as_is(tmp_path, fake_age):  # Gate B I2
    root = _lodge(tmp_path)
    _grow(root, [0])
    out = tmp_path / "out"
    meta, = (root / sa.ARCHIVE / sa.FORMAT).rglob("*.json")
    torn = out / sa.FORMAT / meta.parent.name / f".{meta.stem}.gz.age.part"
    torn.parent.mkdir(parents=True)
    torn.write_bytes(AGE_HEADER[:5])  # what a kill -9 mid-write leaves
    assert len(sa.write_out(root, out)) == 1
    assert not torn.exists() and _open(out / sa.FORMAT / meta.parent.name / f"{meta.stem}.gz.age")[:2] == b"\x1f\x8b"


def test_a_part_deleted_since_is_cleared_from_the_out_folder(tmp_path, fake_age):  # Gate B I3
    root = _lodge(tmp_path)
    _grow(root, [0])
    out = tmp_path / "out"
    assert len(sa.write_out(root, out)) == 1  # the upload then fails: out stays
    folder, = (root / sa.ARCHIVE / sa.FORMAT).iterdir()
    for kept in folder.iterdir():  # the session is deleted on purpose (what delete removes)
        kept.unlink()
    assert sa.write_out(root, out) == []
    assert [p for p in out.rglob("*") if p.is_file()] == []


def _stale_out(root, tmp_path):
    """An out folder left by a failed upload, holding a part deleted since."""
    _grow(root, [0])
    out = tmp_path / "out"
    sa.write_out(root, out)
    folder, = (root / sa.ARCHIVE / sa.FORMAT).iterdir()
    for kept in folder.iterdir():
        kept.unlink()
    return out


def test_the_clean_up_runs_even_without_a_recipient(tmp_path, fake_age, monkeypatch):  # Gate B N8
    root = _lodge(tmp_path)
    out = _stale_out(root, tmp_path)
    monkeypatch.delenv("WOLTSPACE_BACKUP_AGE_RECIPIENT")
    with pytest.raises(ValueError, match="plaintext"):
        sa.write_out(root, out)
    assert [p for p in out.rglob("*") if p.is_file()] == []


def test_the_clean_up_runs_even_when_the_refresh_raises(tmp_path, fake_age, monkeypatch, capsys):  # Gate B N8
    root = _lodge(tmp_path)
    out = _stale_out(root, tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    def broken(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sa, "refresh", broken)
    assert cli_main(["backup", "sessions", "--out", str(out)]) == 3  # a failure: the upload waits
    assert [p for p in out.rglob("*") if p.is_file()] == [] and "session archive failed" in capsys.readouterr().err


def test_a_skipped_file_is_exit_1_and_the_rest_still_goes_out_cleaned(tmp_path, fake_age, monkeypatch):
    root = _lodge(tmp_path)
    out = _stale_out(root, tmp_path)  # a deleted session's files are still there
    (root / "wolt-a/.claude/projects/-w/.env").write_text("x")  # skipped every night: looks like a credential
    _grow(root, [1])
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(root))
    assert cli_main(["backup", "sessions", "--out", str(out)]) == 10  # only skipped: the upload goes on
    folder, = (root / sa.ARCHIVE / sa.FORMAT).iterdir()
    assert sorted(p.name for p in out.rglob("*") if p.is_file()) == sorted(f"{p.name}.age" for p in folder.iterdir())


def _nightly(tmp_path, root, out, age=None, **env):
    """Run the nightly lines exactly as docs/backup.md has them, under bash -lc and
    set -e, with the real CLI and an rclone that only records that it ran."""
    import shlex
    import subprocess
    lines = (Path(__file__).resolve().parents[1] / "docs" / "backup.md").read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("rc=0; woltspace backup sessions --out"))
    gate = "\n".join(lines[start:lines.index("```", start)])
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "woltspace").write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -m woltspace \"$@\"\n")
    (bindir / "rclone").write_text(f"#!/bin/sh\nprintf upload > {shlex.quote(str(tmp_path / 'uploaded'))}\n")
    if age is not None:  # a login shell resets PATH: the fake age goes beside the others
        (bindir / "age").write_bytes((age / "age").read_bytes())
    for tool in bindir.iterdir():
        tool.chmod(0o755)
    script = f"set -e\nexport PATH={shlex.quote(str(bindir))}:$PATH\nsessions={shlex.quote(str(out))}\ndest=local:dummy\n{gate}\n"
    run = subprocess.run(["bash", "-lc", script], env={**os.environ, "WOLTSPACE_WOLTS_DIR": str(root), **env},
                         capture_output=True, text=True, timeout=60, check=False)
    return run, (tmp_path / "uploaded").exists()


def test_a_crash_before_the_clean_up_never_uploads(tmp_path):  # the reviewer's N8-R1 case
    root = _lodge(tmp_path)
    out = tmp_path / "out"
    stale = out / sa.FORMAT / "source" / "deleted.gz.age"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"synthetic stale ciphertext")
    run, uploaded = _nightly(tmp_path, root, out, WOLTSPACE_PORT="invalid-port")
    assert "invalid-port" in run.stderr and not uploaded


def test_a_night_that_only_skipped_files_still_uploads(tmp_path, fake_age):
    root = _lodge(tmp_path)
    (root / "wolt-a/.claude/projects/-w/.env").parent.mkdir(parents=True)
    (root / "wolt-a/.claude/projects/-w/.env").write_text("x")  # skipped: exit 10
    _grow(root, [0])
    run, uploaded = _nightly(tmp_path, root, tmp_path / "out", age=fake_age)
    assert uploaded, (run.returncode, run.stdout[-800:], run.stderr[-800:])


def test_a_remote_named_like_a_lock_is_not_read_as_on(fake_rclone):  # review 4
    fake_rclone.write_text("named")
    assert sa.check_lock("r2:retention-lock/lodges/object-lock")[0] == "unknown"
