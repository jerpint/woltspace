"""Focused checks for B1's specified archive/restore guarantees; synthetic data only."""
import json
import os

from harnesses import HARNESSES

from woltspace import session_archive as sa

REL = 'wolt-a/.claude/projects/-w/session.jsonl'


def lodge(tmp_path):
    root = tmp_path / 'wolts'
    (root / 'wolt-a').mkdir(parents=True)
    (root / 'woltspace.json').write_text(json.dumps({'sessions': {'keep_days': None}}))
    return root


def source(root, rel=REL, data=b'{"turn":1}\n'):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_repeated_append_in_same_mtime_second_is_not_lost(tmp_path):
    root = lodge(tmp_path)
    p = source(root)
    os.utime(p, (1700000000, 1700000000))
    sa.refresh(root, HARNESSES)
    p.write_bytes(p.read_bytes() * 2)
    os.utime(p, (1700000000, 1700000000))
    sa.refresh(root, HARNESSES)
    _, refused = sa.restore(root / sa.ARCHIVE, tmp_path / 'restore')
    assert not refused
    assert (tmp_path / 'restore' / 'root' / REL).read_bytes() == p.read_bytes()


def test_corrupt_latest_metadata_refuses_instead_of_restoring_a_prefix(tmp_path):
    root = lodge(tmp_path)
    p = source(root)
    sa.refresh(root, HARNESSES)
    p.write_bytes(p.read_bytes() + b'{"turn":2}\n')
    sa.refresh(root, HARNESSES)
    metas = sorted((root / sa.ARCHIVE / sa.FORMAT).rglob('*.json'), key=lambda p: json.loads(p.read_text())['captured_at'])
    metas[-1].write_text('{')
    restored, refused = sa.restore(root / sa.ARCHIVE, tmp_path / 'restore')
    assert refused and not restored
    assert not (tmp_path / 'restore' / 'root' / REL).exists()


def test_source_prefix_symlink_does_not_capture_outside_data(tmp_path):
    root = lodge(tmp_path)
    outside = tmp_path / 'outside'
    source(outside, 'private.jsonl')
    prefix = root / 'wolt-a/.claude/projects'
    prefix.parent.mkdir(parents=True)
    prefix.symlink_to(outside, target_is_directory=True)
    report = sa.refresh(root, HARNESSES)
    assert not report.stored


def test_native_relative_cwd_is_unsure_and_not_archived(tmp_path, monkeypatch):
    root = lodge(tmp_path)
    home = tmp_path / 'home'
    source(home, '.claude/projects/-w/session.jsonl', b'{"cwd":"."}\n')
    monkeypatch.chdir(root / 'wolt-a')
    report = sa.refresh(root, HARNESSES, home=home)
    assert not report.stored


def test_json_backup_still_refreshes_the_opted_in_archive(tmp_path, monkeypatch):
    from woltspace.cli import main
    root = lodge(tmp_path)
    source(root)
    monkeypatch.setenv('WOLTSPACE_WOLTS_DIR', str(root))
    assert main(['backup', '--json', '--tag', 'review', '--out', str(tmp_path / 'out')]) == 0
    assert (root / sa.ARCHIVE / sa.FORMAT).is_dir()


def test_invalid_deflate_refuses_only_the_damaged_source(tmp_path):
    root = lodge(tmp_path)
    source(root)
    sa.refresh(root, HARNESSES)
    part, = (root / sa.ARCHIVE / sa.FORMAT).rglob('*.gz')
    # Valid gzip header, invalid DEFLATE block type, plus trailer.
    part.write_bytes(bytes.fromhex('1f8b0800000000000203') + b'\x07' + b'\x00' * 8)
    source(root, 'wolt-a/.claude/projects/-w/other.jsonl', b'{"turn":2}\n')
    sa.refresh(root, HARNESSES)
    restored, refused = sa.restore(root / sa.ARCHIVE, tmp_path / 'back')
    assert len(refused) == 1 and len(restored) == 1
    assert (tmp_path / 'back/root/wolt-a/.claude/projects/-w/other.jsonl').is_file()


def test_earlier_version_does_not_overwrite_a_real_dot_v1_source(tmp_path):
    root = lodge(tmp_path)
    rel = 'wolt-a/.claude/projects/-w/s1/tool-results/r.txt'
    source(root, rel, b'old bytes')
    source(root, rel + '.v1', b'independent named file')
    sa.refresh(root, HARNESSES)
    source(root, rel, b'new bytes')
    sa.refresh(root, HARNESSES)
    restored, refused = sa.restore(root / sa.ARCHIVE, tmp_path / 'back')
    assert not refused and len(restored) == 3
    # Three independent retained values must survive in three files.
    files = [p.read_bytes() for p in (tmp_path / 'back').rglob('*') if p.is_file()]
    assert sorted(files) == sorted([b'old bytes', b'new bytes', b'independent named file'])
