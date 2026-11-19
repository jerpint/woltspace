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
