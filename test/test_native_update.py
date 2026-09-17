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
    return {'changed': True, 'impact': 'Control plane briefly stops.',
        'install': {'cli': 'fake-cli', 'uv': 'fake-uv', 'extras': ['connectors'],
                    'python': sys.executable, 'tool_root': str(tmp_path)},
        'layout': {'wolts_dir': str(tmp_path / 'wolts'), 'host': '127.0.0.1', 'port': 7788},
        'components': [{'name': 'woltspace', 'current': planner.__version__, 'target': '0.6.0',
                        'changed': True}],
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


def test_failed_python_install_restarts_and_reports_observed_version(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan, lambda cmd: cmd == ['install', 'woltspace'])
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and result['completed'] == []
    assert calls[-1] == ['fake-cli', 'start', '--json']
    assert result['recovery']['state'] == 'healthy'
    assert 'woltspace' in result['observed_versions']


def test_restart_failure_is_not_success(monkeypatch, plan, tmp_path):
    calls = setup_worker(monkeypatch, plan, lambda cmd: cmd[1] == 'start')
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and 'error' in result['recovery']
    assert result['completed']


def test_initially_stopped_lodge_stays_stopped(monkeypatch, plan, tmp_path):
    plan['instance'] = {'state': 'stopped'}
    calls = setup_worker(monkeypatch, plan)
    assert worker.apply(plan, tmp_path, {})['ok']
    assert all('stop' not in cmd and 'start' not in cmd for cmd in calls)


def test_concurrent_update_refuses_before_apply(monkeypatch, plan, tmp_path):
    path = tmp_path / 'plan.json'; path.write_text(json.dumps(plan))
    monkeypatch.setattr(worker, 'apply', lambda *a: pytest.fail('concurrent apply'))
    with (tmp_path / '.woltspace-update.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert worker.main(str(path)) == 1


def test_noninteractive_apply_needs_prior_authorization(monkeypatch, plan):
    monkeypatch.setattr(planner, 'build_plan', lambda *a: plan)
    monkeypatch.setattr(planner, 'print_plan', lambda *a: None)
    monkeypatch.setattr(planner.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(planner.os, 'execv', lambda *a: pytest.fail('unauthorized apply'))
    assert planner.command(SimpleNamespace(to=None, check=False)) == 1


def test_read_only_check_does_not_handoff(monkeypatch, plan):
    monkeypatch.setattr(planner, 'build_plan', lambda *a: plan)
    monkeypatch.setattr(planner, 'print_plan', lambda *a: None)
    monkeypatch.setattr(planner.os, 'execv', lambda *a: pytest.fail('check applied update'))
    assert planner.command(SimpleNamespace(to=None, check=True)) == 0


def test_external_native_commands_absent(monkeypatch):
    monkeypatch.setenv('WOLTSPACE_ISOLATION', 'external')
    parser = build_parser()
    for verb in ('update', 'start', 'stop', 'status', 'doctor', 'auto'):
        with pytest.raises(SystemExit):
            parser.parse_args([verb])
    assert parser.parse_args(['session', 'list']).command == 'session'


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


@pytest.mark.parametrize('value', ['1.2', '1.2.3.4', '1!1.2.3', '1.2.3+local', '1.2.3rc1', '1.2.3.dev1'])
def test_planner_rejects_versions_worker_cannot_apply(value):
    assert planner.stable(value) is None


@pytest.mark.parametrize('value', ['0.5.2', '1.2.3.post4'])
def test_planner_accepted_versions_are_accepted_by_worker(plan, value):
    version = planner.stable(value)
    assert version is not None
    plan['components'][0]['target'] = str(version)
    worker.validate_versions(plan)


def test_completed_handoff_cleans_staging_and_preserves_report(monkeypatch, plan, tmp_path):
    stage_dir=tmp_path/'stage';stage_dir.mkdir()
    (stage_dir/'.owned-update-stage').touch()
    path=stage_dir/'plan.json';path.write_text(json.dumps(plan))
    monkeypatch.setattr(worker,'apply',lambda *a:{'ok':True,'completed':[]})
    assert worker.main(str(path))==0
    assert not stage_dir.exists()
    reports=list((Path(plan['layout']['wolts_dir'])/'.space/platform/updates').glob('*.json'))
    assert len(reports)==1 and json.loads(reports[0].read_text())['ok']


def test_python_plan_never_inspects_npm(monkeypatch, plan, tmp_path):
    monkeypatch.setattr(planner, 'native_install', lambda: plan['install'])
    monkeypatch.setattr(planner.shutil, 'which', lambda name: pytest.fail('Unexpected executable lookup: '+name))
    monkeypatch.setattr(planner, 'get_json', lambda url: {'info': {'version': planner.__version__}} if url == 'https://pypi.org/pypi/woltspace/json' else pytest.fail('Unexpected registry: '+url))
    monkeypatch.setattr(planner, 'inspect_instance', lambda layout: plan['instance'])
    result = planner.build_plan(RuntimeLayout(Path(plan['layout']['wolts_dir']), tmp_path, port=7788))
    assert len(result['components']) == 1 and result['components'][0]['name'] == 'woltspace'
    assert not result['changed']


def test_python_dependency_staging_failure_never_stops_lodge(monkeypatch, plan, tmp_path):
    monkeypatch.setattr(worker, 'status', lambda *a: copy.deepcopy(plan['instance']))
    calls = []
    def run(command, env, **kw):
        calls.append(command)
        if command[:3] == ['fake-uv', 'pip', 'install']:
            raise worker.Failure('Python dependency staging failed')
        return 'woltspace '+planner.__version__
    monkeypatch.setattr(worker, 'run', run)
    result = worker.apply(plan, tmp_path, {})
    assert not result['ok'] and result['completed'] == []
    assert 'dependency staging failed' in result['error']
    assert not any('stop' in command or 'start' in command or 'tool' in command for command in calls)


@pytest.mark.parametrize('target', ['0.6', '0.6.0rc1', '0.6.0;touch x', '--help'])
def test_explicit_version_invalid_before_network(monkeypatch, plan, tmp_path, target):
    monkeypatch.setattr(planner, 'native_install', lambda: plan['install'])
    monkeypatch.setattr(planner, 'get_json', lambda *a: pytest.fail('network lookup for invalid input'))
    with pytest.raises(planner.UpdateError, match='exact stable'):
        planner.build_plan(RuntimeLayout(tmp_path/'wolts', tmp_path), target)


def test_exact_target_never_looks_up_latest(monkeypatch, plan, tmp_path):
    monkeypatch.setattr(planner, 'native_install', lambda: plan['install'])
    calls=[]
    def registry(url):
        calls.append(url)
        return {'info': {'version': '0.6.0'}}
    monkeypatch.setattr(planner, 'get_json', registry)
    monkeypatch.setattr(planner, 'inspect_instance', lambda *a: plan['instance'])
    result=planner.build_plan(RuntimeLayout(tmp_path/'wolts', tmp_path), '0.6.0')
    assert calls==['https://pypi.org/pypi/woltspace/0.6.0/json']
    assert result['components'][0]['target']=='0.6.0'


def test_explicit_target_handoff_does_not_prompt(monkeypatch, plan, tmp_path):
    monkeypatch.setattr(planner, 'build_plan', lambda layout,target: plan if target=='0.6.0' else pytest.fail('wrong target'))
    monkeypatch.setattr(planner.tempfile, 'mkdtemp', lambda **k: str(tmp_path))
    monkeypatch.setattr(planner.sys.stdin, 'isatty', lambda: pytest.fail('explicit target should not prompt'))
    calls=[]
    def execv(python,command):
        calls.append(command)
        raise OSError('controlled handoff end')
    monkeypatch.setattr(planner.os,'execv',execv)
    assert planner.command(SimpleNamespace(to='0.6.0',check=False))==1
    assert len(calls)==1
    assert not tmp_path.exists()


def test_staged_version_mismatch_never_installs(monkeypatch, plan, tmp_path):
    calls=[]
    def run(command, env, **kw):
        calls.append(command)
        return '0.5.2' if '-c' in command else 'woltspace==0.6.0'
    monkeypatch.setattr(worker,'run',run)
    with pytest.raises(worker.Failure,match='Staged wheel version'):
        worker.stage(plan,tmp_path,{})
    assert not any('tool' in c or '--offline' in c for c in calls)


def test_only_python_component_supported(plan):
    plan['components'].append({'name':'@woltspace/tui'})
    with pytest.raises(worker.Failure,match='Only the Woltspace'):
        worker.validate_versions(plan)


def test_lower_target_refuses_before_staging(monkeypatch, plan, tmp_path):
    monkeypatch.setattr(planner,'native_install',lambda:plan['install'])
    monkeypatch.setattr(planner,'get_json',lambda url:{'info':{'version':'0.5.0'}})
    monkeypatch.setattr(planner,'inspect_instance',lambda *a:pytest.fail('downgrade should refuse before lodge inspection'))
    with pytest.raises(planner.UpdateError,match='Downgrades are not supported'):
        planner.build_plan(RuntimeLayout(tmp_path/'wolts',tmp_path),'0.5.0')


def test_exact_target_check_never_handoffs(monkeypatch, plan):
    monkeypatch.setattr(planner,'build_plan',lambda layout,target:plan if target=='0.6.0' else pytest.fail('unexpected target'))
    monkeypatch.setattr(planner.os,'execv',lambda *a:pytest.fail('check must not install'))
    assert planner.command(SimpleNamespace(to='0.6.0',check=True))==0
