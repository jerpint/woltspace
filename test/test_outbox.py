"""The outbox: what may be staged, what the lodge will open, and what is swept."""

import os
import shutil
import time
from pathlib import Path

import pytest

import outbox


def _reason(call, *args) -> outbox.OutboxError:
    with pytest.raises(outbox.OutboxError) as error:
        call(*args)
    return error.value


# --- stage and resolve ------------------------------------------------------

def test_stage_then_resolve_round_trips_bytes_and_modes(tmp_path):
    source = tmp_path / "weekly update.html"
    source.write_bytes(b"<h1>hi</h1>")
    box = tmp_path / "outbox"
    entry = outbox.stage(source, box)
    assert outbox.ENTRY_ID_RE.fullmatch(entry)
    path, size = outbox.resolve(box, entry)
    assert path.read_bytes() == b"<h1>hi</h1>" and size == 11
    assert (box.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600
    assert [p.name for p in box.iterdir()] == [entry]      # no temp file left


def test_stage_leaves_nothing_behind_when_the_copy_fails(tmp_path, monkeypatch):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    box = tmp_path / "outbox"

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shutil, "copyfileobj", fail)
    with pytest.raises(OSError, match="No space left"):
        outbox.stage(source, box)
    assert list(box.iterdir()) == []


def test_stage_checks_the_source_before_it_creates_anything(tmp_path):
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    box = tmp_path / "outbox"
    assert _reason(outbox.stage, source, box).reason == "empty"
    assert not box.exists()


def test_stage_tightens_an_outbox_that_already_exists_with_a_wider_mode(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    box = tmp_path / "outbox"
    box.mkdir()
    box.chmod(0o755)
    outbox.stage(source, box)
    assert (box.stat().st_mode & 0o777) == 0o700


def test_stage_gives_every_copy_its_own_entry(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF")
    box = tmp_path / "outbox"
    first, second = outbox.stage(source, box), outbox.stage(source, box)
    assert first != second
    assert sorted(p.name for p in box.iterdir()) == sorted([first, second])


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "A" * 32, "0" * 31, "0" * 33, None, 7])
def test_resolve_rejects_anything_that_is_not_a_32_hex_id(tmp_path, bad):
    with pytest.raises(outbox.OutboxError) as error:
        outbox.resolve(tmp_path, bad)
    assert error.value.reason == "attachment_invalid"


def test_resolve_names_an_invalid_id_without_echoing_it(tmp_path):
    error = _reason(outbox.resolve, tmp_path, "../../etc/passwd")
    assert str(error) == "invalid attachment id"


def test_resolve_refuses_a_symlink_planted_in_the_outbox(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s3cret")
    box = tmp_path / "outbox"
    box.mkdir()
    (box / ("a" * 32)).symlink_to(secret)
    with pytest.raises(outbox.OutboxError) as error:
        outbox.resolve(box, "a" * 32)
    assert error.value.reason == "attachment_invalid"


def test_resolve_reports_a_missing_entry(tmp_path):
    error = _reason(outbox.resolve, tmp_path, "a" * 32)
    assert error.reason == "attachment_not_found"
    assert str(error) == "attachment is no longer in the outbox"


def test_resolve_reports_a_missing_outbox_the_same_way(tmp_path):
    error = _reason(outbox.resolve, tmp_path / "never-created", "a" * 32)
    assert error.reason == "attachment_not_found"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_resolve_refuses_an_entry_that_is_not_a_regular_file(tmp_path, kind):
    entry = tmp_path / ("a" * 32)
    if kind == "directory":
        entry.mkdir()
    else:
        os.mkfifo(entry)
    error = _reason(outbox.resolve, tmp_path, "a" * 32)     # a FIFO must not block
    assert error.reason == "attachment_invalid"
    assert str(error) == "attachment is not a regular file"


def test_resolve_refuses_a_socket_planted_in_the_outbox(tmp_path, monkeypatch):
    import socket

    monkeypatch.chdir(tmp_path)                             # a socket path must be short
    planted = socket.socket(socket.AF_UNIX)
    try:
        planted.bind("a" * 32)
        error = _reason(outbox.resolve, tmp_path, "a" * 32)
    finally:
        planted.close()
    assert error.reason == "attachment_invalid"
    assert str(error) == "attachment is not a regular file"


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens every file")
def test_resolve_reports_an_entry_it_may_not_open_without_the_os_text(tmp_path):
    entry = tmp_path / ("a" * 32)
    entry.write_bytes(b"x")
    entry.chmod(0)
    error = _reason(outbox.resolve, tmp_path, "a" * 32)
    assert error.reason == "attachment_invalid"
    assert str(error) == "attachment cannot be opened"      # no path, no errno


def test_resolve_measures_the_entry_itself(tmp_path):
    (tmp_path / ("a" * 32)).write_bytes(b"12345")
    assert outbox.resolve(tmp_path, "a" * 32) == (tmp_path / ("a" * 32), 5)


# --- check_source -----------------------------------------------------------

def test_check_source_returns_the_size(tmp_path):
    source = tmp_path / "report.pdf"
    source.write_bytes(b"%PDF-1.7")
    assert outbox.check_source(source) == 8


def test_check_source_follows_a_symlink_to_a_regular_file(tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.7")
    link = tmp_path / "latest.pdf"
    link.symlink_to(target)
    assert outbox.check_source(link) == 8


def test_check_source_missing_file(tmp_path):
    path = tmp_path / "nope.pdf"
    error = _reason(outbox.check_source, path)
    assert error.reason == "not_found"
    assert str(error) == f"file not found: {path}"


def test_check_source_missing_parent_is_also_not_found(tmp_path):
    (tmp_path / "plain").write_text("x")
    path = tmp_path / "plain" / "nope.pdf"                  # ENOTDIR, not ENOENT
    assert _reason(outbox.check_source, path).reason == "not_found"


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_check_source_not_a_regular_file(tmp_path, kind):
    path = tmp_path / "thing"
    if kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    error = _reason(outbox.check_source, path)
    assert error.reason == "not_a_file"
    assert str(error) == f"not a regular file: {path}"


def test_check_source_empty_file(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    error = _reason(outbox.check_source, path)
    assert error.reason == "empty"
    assert str(error) == f"file is empty: {path}"


def test_check_source_accepts_exactly_the_limit_and_refuses_one_byte_more(tmp_path):
    path = tmp_path / "big.bin"
    with open(path, "wb") as handle:
        handle.truncate(outbox.MAX_FILE_BYTES)              # sparse: no real 50 MB
    assert outbox.check_source(path) == 50 * 1024 * 1024
    with open(path, "ab") as handle:
        handle.truncate(outbox.MAX_FILE_BYTES + 1)
    error = _reason(outbox.check_source, path)
    assert error.reason == "too_large"
    assert str(error) == f"{path} is 52428801 bytes; the limit is 52428800"


def test_check_source_credential_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("TOKEN=1")
    error = _reason(outbox.check_source, path)
    assert error.reason == "credential"
    assert str(error) == "refusing to send a credential file: .env"


def test_check_source_sees_the_directory_behind_a_relative_name(tmp_path, monkeypatch):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")
    monkeypatch.chdir(tmp_path / ".codex")
    error = _reason(outbox.check_source, Path("auth.json"))
    assert error.reason == "credential"
    assert str(error) == "refusing to send a credential file: auth.json"


def test_check_source_sees_a_credential_file_behind_an_innocent_symlink(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")
    link = tmp_path / "notes.json"
    link.symlink_to(tmp_path / ".claude" / ".credentials.json")
    error = _reason(outbox.check_source, link)
    assert error.reason == "credential"
    assert str(error) == "refusing to send a credential file: notes.json"


def test_check_source_refuses_a_credential_name_whatever_it_points_at(tmp_path):
    (tmp_path / "plain.txt").write_text("nothing secret")
    link = tmp_path / ".env"
    link.symlink_to(tmp_path / "plain.txt")
    assert _reason(outbox.check_source, link).reason == "credential"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
def test_check_source_unreadable_file(tmp_path):
    path = tmp_path / "locked.log"
    path.write_text("x")
    path.chmod(0)
    error = _reason(outbox.check_source, path)
    assert error.reason == "unreadable"
    assert str(error) == f"cannot read {path}: permission denied"


@pytest.mark.skipif(os.geteuid() == 0, reason="root searches every directory")
def test_check_source_file_in_a_directory_it_may_not_search(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    path = locked / "report.pdf"
    path.write_text("x")
    locked.chmod(0)
    try:
        error = _reason(outbox.check_source, path)
    finally:
        locked.chmod(0o700)                                 # so tmp_path can be removed
    assert error.reason == "unreadable"
    assert str(error) == f"cannot read {path}: permission denied"


@pytest.mark.parametrize("rel", [
    ".env", "app/.env.local", ".env.example", ".env.sample", "notes.env",
    ".claude/.credentials.json", ".claude/.credentials.json.bak", "x/.credentials.json",
    ".codex/auth.json", "auth.json", "report.pdf",
])
def test_credential_rule_matches_the_backup_rules(rel):
    from woltspace.backup import secret_rule
    assert outbox.credential_rule(Path(rel)) == secret_rule(rel)


# --- discard and sweep ------------------------------------------------------

def test_discard_removes_the_entry(tmp_path):
    entry = tmp_path / ("a" * 32)
    entry.write_bytes(b"x")
    outbox.discard(tmp_path, "a" * 32)
    assert not entry.exists()


@pytest.mark.parametrize("entry_id", ["a" * 32, "", "../victim", "A" * 32, None, 7, ["a" * 32]])
def test_discard_is_silent_for_a_missing_entry_and_a_malformed_id(tmp_path, entry_id):
    box = tmp_path / "outbox"
    box.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    outbox.discard(box, entry_id)
    assert victim.read_text() == "keep me"


def test_discard_never_raises(tmp_path):
    (tmp_path / ("a" * 32)).mkdir()                         # unlink would raise
    outbox.discard(tmp_path, "a" * 32)
    outbox.discard(tmp_path / "never-created", "a" * 32)


def test_sweep_removes_only_old_entries(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    old, fresh, stale_tmp = box / ("a" * 32), box / ("b" * 32), box / f".{'c' * 32}.tmp"
    for p in (old, fresh, stale_tmp):
        p.write_bytes(b"x")
    now = time.time()
    for p in (old, stale_tmp):
        os.utime(p, (now - 7200, now - 7200))
    assert outbox.sweep(box, now=now) == 2
    assert [p.name for p in box.iterdir()] == ["b" * 32]


def test_sweep_on_a_missing_directory_returns_zero(tmp_path):
    assert outbox.sweep(tmp_path / "never-created") == 0


def test_sweep_honours_max_age_and_defaults_now_to_the_clock(tmp_path):
    entry = tmp_path / ("a" * 32)
    entry.write_bytes(b"x")
    past = time.time() - 120
    os.utime(entry, (past, past))
    assert outbox.sweep(tmp_path) == 0                      # an hour is the default
    assert outbox.sweep(tmp_path, max_age=60) == 1
    assert not entry.exists()


def test_sweep_unlinks_an_old_symlink_and_leaves_its_target(tmp_path):
    box = tmp_path / "outbox"
    box.mkdir()
    target = tmp_path / "target"
    target.write_text("keep me")
    link = box / ("a" * 32)
    link.symlink_to(target)
    now = time.time()
    os.utime(link, (now - 7200, now - 7200), follow_symlinks=False)
    assert outbox.sweep(box, now=now) == 1
    assert not link.is_symlink() and target.read_text() == "keep me"


def test_sweep_ignores_what_is_not_a_file_directly_in_the_outbox(tmp_path):
    box = tmp_path / "outbox"
    (box / "old-dir").mkdir(parents=True)
    nested = box / "old-dir" / ("a" * 32)
    nested.write_bytes(b"x")
    now = time.time()
    for p in (nested, box / "old-dir"):
        os.utime(p, (now - 7200, now - 7200))
    assert outbox.sweep(box, now=now) == 0
    assert nested.exists()


# --- display_name and the path helper ---------------------------------------

@pytest.mark.parametrize("raw, shown", [
    ("weekly update.html", "weekly update.html"),
    ("../../etc/passwd", "passwd"),
    ("C:\\Users\\me\\report.pdf", "report.pdf"),
    ("r\u00e9sum\u00e9 \U0001F99D.pdf", "r\u00e9sum\u00e9 \U0001F99D.pdf"),
    ("bad\x00na\nme.txt", "badname.txt"),
    ("..", "file"), ("", "file"), (None, "file"),
    ("x" * 300 + ".html", "x" * 195 + ".html"),
])
def test_display_name_is_a_safe_basename(raw, shown):
    assert outbox.display_name(raw) == shown


@pytest.mark.parametrize("raw, shown", [
    ("  report.pdf  ", "report.pdf"),
    ("report.pdf/", "file"),                    # the last component is empty
    (" \t\n", "file"), ("...", "file"), ("\x7f\x01", "file"), (7, "file"),
    ("x" * 200, "x" * 200),                     # exactly the limit is untouched
    ("x" * 300, "x" * 200),                     # nothing to keep
    ("x" * 300 + "." + "y" * 40, "x" * 200),    # too long to be an extension
    ("." + "x" * 300, "." + "x" * 199),         # a dotfile has no extension
])
def test_display_name_edges(raw, shown):
    assert outbox.display_name(raw) == shown


def test_space_outbox_dir_is_under_the_space_root(tmp_path):
    import paths
    assert paths.space_outbox_dir(tmp_path) == tmp_path / ".space" / "outbox"
