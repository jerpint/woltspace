"""Update contracts without signaling a real control plane or installing tools."""
import copy
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from woltspace import update as planner
from woltspace import update_worker as worker
from woltspace.cli import build_parser
from woltspace.layout import RuntimeLayout


@pytest.fixture
def plan(tmp_path):
    return {'schema': 1, 'changed': True,
        'install': {'cli': 'fake-cli', 'uv': 'fake-uv', 'extras': ['connectors'],
                    'python': sys.executable, 'tool_root': str(tmp_path)},
        'layout': {'wolts_dir': str(tmp_path / 'wolts'), 'host': '127.0.0.1', 'port': 7788},
        'components': [{'name': 'woltspace', 'current': planner.__version__, 'target': '0.6.0',
                        'changed': True, 'tag_prefix': 'v'}],
        'notes': [], 'migrations': [],
        'instance': {'state': 'healthy', 'health': {'instance_id': 'before', 'connectors': [],
                       'adoption': {'adopted': [], 'orphaned': [], 'unchanged': []}}}}


def fake_stage(plan, directory, env):
    for component in plan['components']:
        if component['changed']:
            component['install_command'] = ['install', component['name']]


def setup_worker(monkeypatch, plan, fail=None):
    calls = []
    monkeypatch.setattr(worker, 'status', lambda *a: copy.deepcopy(plan['instance']))
    monkeypatch.setattr(worker, 'stage', fake_stage)
    monkeypatch.setattr(worker, 'verify', lambda *a, **kw: {'state': 'healthy'})
    def run(command, env, **kwargs):
        if '--version' not in command:
            calls.append(command)
        if fail and fail(command):
            raise worker.Failure('deliberate failure')
        return json.dumps({'state': 'healthy'})
    monkeypatch.setattr(worker, 'run', run)
    return calls


def test_apply_stages_then_stops_installs_starts(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan)
    result = worker.apply(plan, tmp_path, {})
    assert result['ok']
    assert calls == [['fake-cli', 'stop', '--json'], ['install', 'woltspace'],
                     ['fake-cli', 'start', '--json']]
    assert result['completed'] == [{'name': 'woltspace', 'version': '0.6.0'}]


def test_stage_failure_never_stops(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan)
    def broken(*a):
        raise worker.Failure('no cached dependency')
    monkeypatch.setattr(worker, 'stage', broken)
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and result['completed'] == [] and calls == []


def test_identity_changed_during_stage_never_stops(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan)
    states = iter([plan['instance'], {'state': 'healthy', 'health': {'instance_id': 'another'}}])
    monkeypatch.setattr(worker, 'status', lambda *a: next(states))
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and calls == []


def test_partial_install_restarts_and_reports_completed(monkeypatch, plan, tmp_path):
    plan['components'].append({'name': '@woltspace/tui', 'current': '0.5.1',
                              'target': '0.6.0', 'changed': True})
    calls = setup_worker(monkeypatch, plan, lambda cmd: cmd == ['install', '@woltspace/tui'])
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok']
    assert result['completed'] == [{'name': 'woltspace', 'version': '0.6.0'}]
    assert calls[-1] == ['fake-cli', 'start', '--json']
    assert result['recovery']['state'] == 'healthy'


def test_restart_failure_is_not_success(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan, lambda cmd: cmd[1] == 'start')
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and 'error' in result['recovery']
    assert result['completed']


@pytest.mark.parametrize('mode', ['stopped', 'tui-only'])
def test_no_control_plane_restart_needed(monkeypatch, plan, tmp_path, mode):
    if mode == 'stopped':
        plan['instance'] = {'state': 'stopped'}
    else:
        plan['components'][0]['changed'] = False
        plan['components'].append({'name': '@woltspace/tui', 'current': '0.5.1',
                                  'target': '0.6.0', 'changed': True})
    calls = setup_worker(monkeypatch, plan)
    assert worker.apply(plan, tmp_path, {})['ok']
    assert all('stop' not in cmd and 'start' not in cmd for cmd in calls)


def test_concurrent_update_refuses_before_apply(monkeypatch, plan, tmp_path):
    path = tmp_path / 'plan.json'; path.write_text(json.dumps(plan))
    monkeypatch.setattr(worker, 'apply', lambda *a: pytest.fail('concurrent apply'))
    with (tmp_path / '.woltspace-update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker.main(str(path)) == 1


def test_saved_plan_never_resolves_latest(monkeypatch, plan, tmp_path, capsys):
    path = tmp_path / 'plan.json'; path.write_text(json.dumps(plan))
    monkeypatch.setattr(planner, 'native_install', lambda: plan['install'])
    monkeypatch.setattr(planner, 'build_plan', lambda *a: pytest.fail('resolved latest'))
    monkeypatch.setattr(planner.RuntimeLayout, 'from_env', lambda: RuntimeLayout(
        Path(plan['layout']['wolts_dir']), tmp_path, port=7788))
    args = SimpleNamespace(apply_plan=str(path), check=True, yes=False, json=True, plan='')
    assert planner.command(args) == 0
    assert json.loads(capsys.readouterr().out)['components'][0]['target'] == '0.6.0'


def test_noninteractive_apply_needs_prior_authorization(monkeypatch, plan):
    monkeypatch.setattr(planner, 'build_plan', lambda *a: plan)
    monkeypatch.setattr(planner, 'print_plan', lambda *a: None)
    monkeypatch.setattr(planner.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(planner.os, 'execv', lambda *a: pytest.fail('unauthorized apply'))
    assert planner.command(SimpleNamespace(apply_plan='', check=False, yes=False, json=False, plan='')) == 1


def test_read_only_check_does_not_handoff(monkeypatch, plan):
    monkeypatch.setattr(planner, 'build_plan', lambda *a: plan)
    monkeypatch.setattr(planner, 'print_plan', lambda *a: None)
    monkeypatch.setattr(planner.os, 'execv', lambda *a: pytest.fail('check applied update'))
    assert planner.command(SimpleNamespace(apply_plan='', check=True, yes=False, json=False, plan='')) == 0


def test_external_native_commands_absent(monkeypatch):
    monkeypatch.setenv('WOLTSPACE_ISOLATION', 'external')
    parser = build_parser()
    for verb in ('update', 'start', 'stop', 'status', 'doctor', 'auto'):
        with pytest.raises(SystemExit):
            parser.parse_args([verb])
    assert parser.parse_args(['session', 'list']).command == 'session'


def test_release_notes_cover_both_streams_and_intermediate_releases(monkeypatch):
    components = [{'name': 'woltspace', 'current': '0.5.0', 'target': '0.5.2', 'tag_prefix': 'v', 'changed': True},
                  {'name': '@woltspace/tui', 'current': '0.4.0', 'target': '0.5.1', 'tag_prefix': 'tui-v', 'changed': True}]
    batch = [{'tag_name': tag, 'html_url': tag, 'body': tag} for tag in
             ('v0.5.0', 'v0.5.1', 'v0.5.2', 'v0.5.3', 'tui-v0.5.0', 'tui-v0.5.1')]
    monkeypatch.setattr(planner, 'get_json', lambda url: batch)
    notes = planner.release_notes(components)
    assert [(n['component'], n['version']) for n in notes] == [
        ('@woltspace/tui', '0.5.0'), ('@woltspace/tui', '0.5.1'),
        ('woltspace', '0.5.1'), ('woltspace', '0.5.2')]


def test_missing_release_notes_blocks_apply(monkeypatch):
    monkeypatch.setattr(planner, 'get_json', lambda url: [])
    with pytest.raises(planner.UpdateError, match='Release notes missing'):
        planner.release_notes([{'name': 'woltspace', 'current': '0.5.0', 'target': '0.5.1',
                               'tag_prefix': 'v', 'changed': True}])


def test_staged_wheel_migration_mismatch_blocks_install(monkeypatch, plan, tmp_path):
    plan['migrations'] = [{'name': 'v0.6.0.md', 'sha256': 'wrong-hash'}]
    folder = tmp_path / 'bundle' / 'container' / 'migrations'; folder.mkdir(parents=True)
    (folder / 'v0.6.0.md').write_text('Unreviewed instructions')
    calls = []
    def run(command, env, **kwargs):
        calls.append(command)
        if '-c' in command:
            return json.dumps({'version': '0.6.0', 'root': str(tmp_path / 'bundle')})
        return 'woltspace==0.6.0'
    monkeypatch.setattr(worker, 'run', run)
    with pytest.raises(worker.Failure, match='migration content differs'):
        worker.stage(plan, tmp_path, {})
    assert not any('tool' in cmd or '--offline' in cmd for cmd in calls)


def test_verification_waits_for_connectors_and_adoption(monkeypatch, plan):
    plan['instance']['health']['connectors'] = [{'name': 'telegram', 'state': 'running'}]
    waiting = {'state': 'healthy', 'health': {'connectors': [{'name': 'telegram', 'state': 'starting'}]}}
    healthy = {'state': 'healthy', 'health': {'connectors': [{'name': 'telegram', 'state': 'running'}],
                                         'adoption': {'adopted': [], 'orphaned': [], 'unchanged': []}}}
    states = iter([waiting, healthy])
    monkeypatch.setattr(worker, 'run', lambda *a: 'woltspace 0.6.0')
    monkeypatch.setattr(worker, 'status', lambda *a: next(states))
    monkeypatch.setattr(worker.time, 'sleep', lambda *a: None)
    assert worker.verify(plan, {}, running=True) == healthy


def test_verification_does_not_blame_preexisting_orphans(monkeypatch, plan):
    orphan = {'wolt': 'old', 'session': 'already-gone'}
    plan['instance']['health']['adoption']['orphaned'] = [orphan]
    state = copy.deepcopy(plan['instance'])
    monkeypatch.setattr(worker, 'run', lambda *a: 'woltspace 0.6.0')
    monkeypatch.setattr(worker, 'status', lambda *a: state)
    assert worker.verify(plan, {}, running=True) == state


def test_verification_reports_new_orphans(monkeypatch, plan):
    state = copy.deepcopy(plan['instance'])
    state['health']['adoption']['orphaned'] = [{'wolt': 'n00b', 'session': 'lost'}]
    monkeypatch.setattr(worker, 'run', lambda *a: 'woltspace 0.6.0')
    monkeypatch.setattr(worker, 'status', lambda *a: state)
    times = iter([0, 31])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(times))
    with pytest.raises(worker.Failure, match='session adoption incomplete'):
        worker.verify(plan, {}, running=True)


def test_changed_npm_root_blocks_before_install(monkeypatch, plan, tmp_path):
    plan['components'][0]['changed'] = False
    plan['components'].append({'name': '@woltspace/tui', 'current': '0.5.1', 'target': '0.6.0',
                              'changed': True, 'npm': 'npm', 'root': str(tmp_path / 'reviewed-root')})
    monkeypatch.setattr(worker, 'run', lambda *a: str(tmp_path / 'different-root'))
    with pytest.raises(worker.Failure, match='npm global root changed'):
        worker.stage(plan, tmp_path, {})


def test_missing_tui_notes_skips_only_tui_and_prints_reason(monkeypatch, capsys):
    components = [{'name': 'woltspace', 'current': '0.5.0', 'target': '0.5.1', 'tag_prefix': 'v', 'changed': True},
                  {'name': '@woltspace/tui', 'current': '0.5.1', 'target': '0.5.2', 'tag_prefix': 'tui-v', 'changed': True}]
    monkeypatch.setattr(planner, 'get_json', lambda url: [{'tag_name': 'v0.5.1', 'html_url': 'wheel-notes', 'body': 'Reviewed'}])
    notes = planner.release_notes(components)
    assert components[0]['changed'] and not components[1]['changed']
    assert 'tui-v0.5.2' in components[1]['skipped']
    serialized = json.loads(json.dumps(components))
    assert serialized[1]['skipped']
    planner.print_plan({'components': components, 'notes': notes, 'migrations': [], 'changed': True, 'impact': 'brief restart'})
    assert 'TUI 0.5.2 skipped' in capsys.readouterr().out


def test_skipped_tui_cannot_be_reenabled_without_review(plan):
    plan['components'].append({'name': '@woltspace/tui', 'current': '0.5.1', 'target': '0.5.2',
                               'changed': True, 'skipped': 'Missing release notes'})
    with pytest.raises(worker.Failure, match='newly reviewed'):
        worker.validate_versions(plan)


@pytest.mark.parametrize('value', ['1.2', '1.2.3.4', '1!1.2.3', '1.2.3+local', '1.2.3rc1', '1.2.3.dev1'])
def test_planner_rejects_versions_worker_cannot_apply(value):
    assert planner.stable(value) is None


@pytest.mark.parametrize('value', ['0.5.2', '1.2.3.post4'])
def test_planner_accepted_versions_are_accepted_by_worker(plan, value):
    version = planner.stable(value)
    assert version is not None
    plan['components'][0]['target'] = str(version)
    worker.validate_versions(plan)


def test_tui_native_scripts_rehearsed_in_isolated_global_prefix(monkeypatch, plan, tmp_path):
    import base64
    import hashlib
    import io
    plan['components'][0]['changed'] = False
    root = tmp_path / 'real-global'
    metadata = root / '@woltspace' / 'tui' / 'package.json'; metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({'version': '0.5.1'}))
    data = b'test-tarball'
    plan['components'].append({'name':'@woltspace/tui', 'current':'0.5.1', 'target':'0.5.2',
        'changed':True, 'npm':'npm', 'root':str(root), 'dist':{'tarball':'https://example.org/tui.tgz',
        'integrity':'sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()}})
    monkeypatch.setattr(worker.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(data))
    calls=[]
    def run(command, env, **kw):
        calls.append(command)
        return str(root) if command[1:]==['root','-g'] else ''
    monkeypatch.setattr(worker,'run',run)
    worker.stage(plan,tmp_path,{})
    rehearsal = next(c for c in calls if '--prefix' in c)
    assert '--global' in rehearsal and '--ignore-scripts' not in rehearsal
    assert rehearsal[rehearsal.index('--prefix')+1] == str(tmp_path/'npm-stage')
    assert str(root) not in rehearsal


def test_completed_handoff_cleans_staging_and_preserves_report(monkeypatch, plan, tmp_path):
    stage_dir=tmp_path/'stage';stage_dir.mkdir()
    (stage_dir/'.owned-update-stage').touch()
    path=stage_dir/'plan.json';path.write_text(json.dumps(plan))
    monkeypatch.setattr(worker,'apply',lambda *a:{'ok':True,'completed':[]})
    assert worker.main(str(path))==0
    assert not stage_dir.exists()
    reports=list((Path(plan['layout']['wolts_dir'])/'.space/platform/updates').glob('*.json'))
    assert len(reports)==1 and json.loads(reports[0].read_text())['ok']


def test_native_build_failure_during_rehearsal_never_stops_lodge(monkeypatch, plan, tmp_path):
    import base64
    import hashlib
    import io
    plan['components'][0]['changed'] = False
    root = tmp_path / 'real-global'
    metadata = root / '@woltspace' / 'tui' / 'package.json'; metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({'version': '0.5.1'}))
    data = b'test-tarball'
    plan['components'].append({'name':'@woltspace/tui', 'current':'0.5.1', 'target':'0.5.2',
        'changed':True, 'npm':'npm', 'root':str(root), 'dist':{'tarball':'https://example.org/tui.tgz',
        'integrity':'sha512-'+base64.b64encode(hashlib.sha512(data).digest()).decode()}})
    monkeypatch.setattr(worker.urllib.request, 'urlopen', lambda *a, **k: io.BytesIO(data))
    monkeypatch.setattr(worker, 'status', lambda *a: copy.deepcopy(plan['instance']))
    calls=[]
    def run(command, env, **kw):
        calls.append(command)
        if '--prefix' in command:
            raise worker.Failure('node-gyp: C++ toolchain unavailable')
        if command[1:] == ['root','-g']:
            return str(root)
        return 'woltspace '+planner.__version__
    monkeypatch.setattr(worker,'run',run)
    result=worker.apply(plan,tmp_path,{})
    assert not result['ok'] and result['completed']==[]
    assert 'toolchain unavailable' in result['error']
    assert not any('stop' in c or 'start' in c for c in calls)
    assert not any(c[:2]==['npm','install'] and '--prefix' not in c for c in calls)
    assert json.loads(metadata.read_text())['version']=='0.5.1'
