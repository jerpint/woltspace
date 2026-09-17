"""Shared skills reach wolts without copying or destroying owner data."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'container' / 'lib'))
from lodge_skills import shared_skills_dir, sync_lodge_skills
from skills_sync import seed_wolt_skills, sync_all_wolt_skills


def skill(wolts, name='lodge-example', front_name=None):
    p = shared_skills_dir(wolts) / name
    p.mkdir(parents=True)
    (p / 'SKILL.md').write_text(f'---\nname: {front_name or name}\ndescription: Example\n---\nOriginal\n')
    return p


def wolt(wolts, name='nw', **config):
    p = wolts / name
    (p / 'wolt').mkdir(parents=True)
    (p / 'wolt' / 'wolt.json').write_text(json.dumps({'name': name, **config}))
    return p


def test_all_harnesses_and_platform_delivery_modes_reach_local_source(tmp_path):
    wolts = tmp_path / 'wolts'
    source = skill(wolts)
    (source / 'references').mkdir()
    (source / 'references' / 'guide.md').write_text('Details')
    for harness in ('claude', 'codex', 'opencode'):
        for delivery in ('copy', 'plugin'):
            p = wolt(wolts, harness + delivery, harness=harness, skills_delivery=delivery)
            sync_lodge_skills(wolts, p)
            for folder in ('.claude/skills', '.agents/skills'):
                link = p / folder / source.name
                assert link.resolve() == source.resolve()
                assert (link / 'references/guide.md').read_text() == 'Details'
    (source / 'SKILL.md').write_text((source / 'SKILL.md').read_text() + 'Edited\n')
    assert 'Edited' in (p / '.claude/skills/lodge-example/SKILL.md').read_text()


@pytest.mark.parametrize('front_name', ['example', 'lodge-other', '"lodge-example"', "'lodge-example'"])
def test_matching_prefixed_name_required(tmp_path, front_name):
    wolts = tmp_path / 'wolts';source = skill(wolts, front_name=front_name);p = wolt(wolts)
    sync_lodge_skills(wolts, p)
    assert (p / '.claude/skills/lodge-example').is_symlink() == ('lodge-example' in front_name)
    assert front_name in (source / 'SKILL.md').read_text()


@pytest.mark.parametrize('name', ['example', 'lodge-', 'lodge-Upper', 'lodge--example'])
def test_invalid_directory_is_not_delivered(tmp_path, name):
    wolts = tmp_path / 'wolts';skill(wolts, name);p = wolt(wolts)
    sync_lodge_skills(wolts, p)
    assert not (p / '.claude/skills' / name).exists()


def test_local_overrides_and_unrelated_symlinks_survive_removal(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    own = p / '.claude/skills/lodge-example';own.mkdir(parents=True)
    (own / 'SKILL.md').write_text('My override')
    other = tmp_path / 'other';other.mkdir()
    unrelated = own.parent / 'lodge-unrelated';unrelated.symlink_to(other)
    sync_lodge_skills(wolts, p)
    assert (own / 'SKILL.md').read_text() == 'My override'
    (source / 'SKILL.md').unlink();source.rmdir()
    sync_lodge_skills(wolts, p)
    assert own.is_dir() and not own.is_symlink()
    assert unrelated.resolve() == other.resolve()


def test_deleted_or_renamed_source_removes_only_managed_links(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    sync_lodge_skills(wolts, p)
    new = source.with_name('lodge-renamed');source.rename(new)
    (new / 'SKILL.md').write_text('---\nname: lodge-renamed\ndescription: Example\n---\n')
    sync_lodge_skills(wolts, p)
    assert not (p / '.claude/skills/lodge-example').is_symlink()
    assert (p / '.agents/skills/lodge-renamed').resolve() == new.resolve()
    (new / 'SKILL.md').unlink();new.rmdir()
    sync_lodge_skills(wolts, p)
    assert not (p / '.claude/skills/lodge-renamed').is_symlink()


def test_no_external_source_symlinks(tmp_path):
    wolts = tmp_path / 'wolts';outside = tmp_path / 'outside';outside.mkdir()
    root = shared_skills_dir(wolts);root.mkdir(parents=True)
    (root / 'lodge-external').symlink_to(outside)
    p = wolt(wolts);sync_lodge_skills(wolts, p)
    assert not (p / '.claude/skills/lodge-external').exists()


def test_user_owned_agents_directory_receives_links_and_keeps_overrides(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    agents = p / '.agents/skills';agents.mkdir(parents=True)
    (agents / 'private').mkdir()
    sync_lodge_skills(wolts, p)
    assert not agents.is_symlink()
    assert (agents / source.name).resolve() == source.resolve()
    assert (agents / 'private').is_dir()


def test_boot_and_new_wolt_deliver_without_platform_sources(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    install = tmp_path / 'missing-install'
    sync_all_wolt_skills(install, wolts)
    assert (p / '.claude/skills/lodge-example').resolve() == source.resolve()
    new = wolt(wolts, 'new');seed_wolt_skills(install, new)
    assert (new / '.agents/skills/lodge-example').resolve() == source.resolve()
    assert not (wolts / '.space/.claude').exists()


def test_launch_refreshes_skills_added_after_boot(tmp_path, monkeypatch):
    import sessions
    wolts = tmp_path / 'wolts';p = wolt(wolts)
    sync_lodge_skills(wolts, p)
    source = skill(wolts)
    record = {'wolt': 'nw', 'harness': 'claude', 'model': 'opus', 'adapter': 'lodge'}
    class Registry:
        def get(self, *args, **kwargs): return record
        def update(self, *args, **kwargs): pass
    monkeypatch.setattr(sessions, 'WOLTS_DIR', wolts)
    monkeypatch.setattr(sessions, 'SessionRegistry', Registry)
    monkeypatch.setattr(sessions, 'ensure_claude_dir_trusted', lambda *args: None)
    monkeypatch.setattr(sessions, '_assemble_spawn_prompt', lambda *args: 'hello')
    sessions.prepare_session_command('nw-example', 'spawn', 'hello')
    assert (p / '.claude/skills/lodge-example').resolve() == source.resolve()


def test_platform_refresh_preserves_shared_sources(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    install = tmp_path / 'install';platform = install / 'container/skills/notify'
    platform.mkdir(parents=True);(platform / 'SKILL.md').write_text('---\nname: notify\n---\n')
    sync_all_wolt_skills(install, wolts)
    (platform / 'SKILL.md').write_text('Updated platform')
    sync_all_wolt_skills(install, wolts)
    assert (p / '.claude/skills/lodge-example').resolve() == source.resolve()
    assert (source / 'SKILL.md').read_text().endswith('Original\n')
    assert (p / '.claude/skills/woltspace-notify/SKILL.md').read_text() == 'Updated platform'


def test_platform_helper_is_delivered_separately_from_shared_skills(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    sync_all_wolt_skills(repo, wolts)
    helper = p / '.claude/skills/woltspace-lodge-skills/SKILL.md'
    assert 'name: woltspace-lodge-skills\n' in helper.read_text()
    assert not helper.parent.is_symlink()
    assert (p / '.agents/skills/lodge-example').resolve() == source.resolve()
    original = repo / 'container/skills/lodge-skills/SKILL.md'
    assert 'name: lodge-skills\n' in original.read_text()


def test_notice_is_shared_idempotent_and_does_not_touch_private_skills(tmp_path):
    from lodge_skills import NOTICE_BEGIN
    wolts = tmp_path / 'wolts';source = skill(wolts);first = wolt(wolts, 'first');second = wolt(wolts, 'second')
    original = (source / 'SKILL.md').read_text()
    private = first / '.claude/skills/private/SKILL.md';private.parent.mkdir(parents=True)
    private.write_text('Personal instructions')
    sync_lodge_skills(wolts, first)
    decorated = (source / 'SKILL.md').read_text()
    assert decorated.startswith(original.split('---\nOriginal')[0] + '---\n')
    assert decorated.endswith('\nOriginal\n')
    assert decorated.count(NOTICE_BEGIN) == 1
    assert '.space/shared-skills/lodge-example/SKILL.md' in decorated
    stamp = (source / 'SKILL.md').stat().st_mtime_ns
    sync_lodge_skills(wolts, second)
    assert (source / 'SKILL.md').stat().st_mtime_ns == stamp
    assert (first / '.claude/skills/lodge-example/SKILL.md').read_text() == decorated
    assert (second / '.agents/skills/lodge-example/SKILL.md').read_text() == decorated
    assert private.read_text() == 'Personal instructions'
    (source / 'SKILL.md').write_text(decorated.replace('Original', 'New workflow'))
    sync_lodge_skills(wolts, first)
    assert 'New workflow' in (second / '.agents/skills/lodge-example/SKILL.md').read_text()


def test_renaming_updates_only_generated_notice_and_preserves_mode(tmp_path):
    wolts = tmp_path / 'wolts';source = skill(wolts);p = wolt(wolts)
    (source / 'SKILL.md').chmod(0o640)
    sync_lodge_skills(wolts, p)
    new = source.with_name('lodge-new');source.rename(new)
    md = new / 'SKILL.md';md.write_text(md.read_text().replace('name: lodge-example', 'name: lodge-new'))
    sync_lodge_skills(wolts, p)
    assert '.space/shared-skills/lodge-new/SKILL.md' in md.read_text()
    assert '.space/shared-skills/lodge-example/SKILL.md' not in md.read_text()
    assert md.read_text().endswith('Original\n')
    assert md.stat().st_mode & 0o777 == 0o640
