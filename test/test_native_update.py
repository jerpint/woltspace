"""Exercise the update wrapper without installing tools or touching a lodge."""
import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from woltspace import update as cli
from woltspace import update_worker as worker
from woltspace.cli import build_parser
from woltspace.layout import RuntimeLayout


@pytest.fixture
def update(tmp_path):
    return {'current':cli.__version__, 'target':'0.6.0', 'changed':True, 'impact':'Lodge briefly stops.',
            'install':{'cli':'fake-cli','uv':'fake-uv','extras':['connectors'],'python':sys.executable,'tool_root':str(tmp_path)},
            'layout':{'wolts_dir':str(tmp_path/'wolts'),'host':'127.0.0.1','port':7788}}


def fake_wrapper(monkeypatch, state='healthy', install_fail=False, restart_fail=False, actual='0.6.0'):
    calls=[]
    def run(command, env, **kw):
        calls.append(command)
        if 'status' in command:return json.dumps({'state':state})
        if '--version' in command:return 'woltspace '+actual
        if 'start' in command and restart_fail:raise worker.Failure('start failed')
        return '{}'
    def install(command, **kw):
        calls.append(command)
        if install_fail:raise worker.Failure('uv install failed')
    monkeypatch.setattr(worker,'run',run)
    monkeypatch.setattr(worker.subprocess,'run',install)
    return calls


def test_stop_install_start_order_and_exact_target(monkeypatch, update):
    calls=fake_wrapper(monkeypatch)
    assert worker.apply(update,{})==0
    assert calls==[['fake-cli','status','--json'],['fake-cli','stop','--json'],
                   ['fake-uv','tool','install','--force','--python',sys.executable,'woltspace[connectors]==0.6.0'],
                   ['fake-cli','start','--json'],['fake-cli','--version']]


def test_install_failure_still_restarts_and_returns_failure(monkeypatch, update, capsys):
    calls=fake_wrapper(monkeypatch,install_fail=True,actual=cli.__version__)
    assert worker.apply(update,{})==1
    assert ['fake-cli','start','--json'] in calls
    assert 'uv install failed' in capsys.readouterr().err


def test_restart_failure_returns_failure_and_remedy(monkeypatch, update, capsys):
    fake_wrapper(monkeypatch,restart_fail=True)
    assert worker.apply(update,{})==1
    assert 'woltspace doctor' in capsys.readouterr().err


def test_initially_stopped_lodge_stays_stopped(monkeypatch, update):
    calls=fake_wrapper(monkeypatch,state='stopped')
    assert worker.apply(update,{})==0
    assert not any('stop' in c or 'start' in c for c in calls)


def test_unhealthy_lodge_does_not_install(monkeypatch, update):
    calls=fake_wrapper(monkeypatch,state='unhealthy')
    assert worker.apply(update,{})==1
    assert not any('tool' in c or 'stop' in c for c in calls)


def test_installed_version_mismatch_is_failure(monkeypatch, update):
    fake_wrapper(monkeypatch,actual=cli.__version__)
    assert worker.apply(update,{})==1


def test_concurrent_update_refuses_before_install(monkeypatch, update, tmp_path):
    path=tmp_path/'update.json';path.write_text(json.dumps(update))
    monkeypatch.setattr(worker,'apply',lambda *a:pytest.fail('concurrent install'))
    with (tmp_path/'.woltspace-update.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        assert worker.main(str(path))==1


def test_handoff_cleanup_only_owned_directory(monkeypatch, update, tmp_path):
    folder=tmp_path/'handoff';folder.mkdir()
    path=folder/'update.json';path.write_text(json.dumps(update))
    monkeypatch.setattr(worker,'apply',lambda *a:0)
    assert worker.main(str(path))==0
    assert folder.exists()
    (folder/'.owned-update-stage').touch()
    assert worker.main(str(path))==0
    assert not folder.exists()


def resolve_fixture(monkeypatch, update, target='0.6.0', yanked=False):
    urls=[]
    monkeypatch.setattr(cli,'native_install',lambda:update['install'])
    def registry(url):
        urls.append(url)
        return {'info':{'version':target,'yanked':yanked,'yanked_reason':'Broken release'}}
    monkeypatch.setattr(cli,'get_json',registry)
    monkeypatch.setattr(cli,'inspect_instance',lambda *a:{'state':'stopped'})
    return urls


def test_explicit_target_never_resolves_latest(monkeypatch, update, tmp_path):
    urls=resolve_fixture(monkeypatch,update)
    result=cli.resolve_update(RuntimeLayout(tmp_path/'wolts',tmp_path),'0.6.0')
    assert urls==['https://pypi.org/pypi/woltspace/0.6.0/json']
    assert result['target']=='0.6.0'


@pytest.mark.parametrize('target',['0.6','v0.6.0','0.6.0.0','0.6.0rc1','0.6.0;touch x','--help'])
def test_invalid_target_fails_before_network(monkeypatch, update, tmp_path, target):
    monkeypatch.setattr(cli,'native_install',lambda:update['install'])
    monkeypatch.setattr(cli,'get_json',lambda *a:pytest.fail('invalid target reached network'))
    with pytest.raises(cli.UpdateError,match='exact stable'):
        cli.resolve_update(RuntimeLayout(tmp_path/'wolts',tmp_path),target)


@pytest.mark.parametrize('target',[None,'0.6.0'])
def test_yanked_release_fails_before_lodge_inspection(monkeypatch, update, tmp_path, target):
    resolve_fixture(monkeypatch,update,yanked=True)
    monkeypatch.setattr(cli,'inspect_instance',lambda *a:pytest.fail('yanked release reached lodge'))
    with pytest.raises(cli.UpdateError,match='yanked: Broken release.*non-yanked'):
        cli.resolve_update(RuntimeLayout(tmp_path/'wolts',tmp_path),target)


def test_downgrade_fails_before_lodge_inspection(monkeypatch, update, tmp_path):
    resolve_fixture(monkeypatch,update,target='0.5.0')
    monkeypatch.setattr(cli,'inspect_instance',lambda *a:pytest.fail('downgrade reached lodge'))
    with pytest.raises(cli.UpdateError,match='Downgrades'):
        cli.resolve_update(RuntimeLayout(tmp_path/'wolts',tmp_path),'0.5.0')


@pytest.mark.parametrize('target',[None,'0.6.0'])
def test_check_never_handoffs(monkeypatch, update, target):
    monkeypatch.setattr(cli,'resolve_update',lambda layout,version:update if version==target else pytest.fail('target changed'))
    monkeypatch.setattr(cli.os,'execv',lambda *a:pytest.fail('check installed'))
    assert cli.command(SimpleNamespace(to=target,check=True))==0


def test_bare_noninteractive_update_does_not_install(monkeypatch, update):
    monkeypatch.setattr(cli,'resolve_update',lambda *a:update)
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:False)
    monkeypatch.setattr(cli.os,'execv',lambda *a:pytest.fail('unconfirmed update'))
    assert cli.command(SimpleNamespace(to=None,check=False))==1


def test_explicit_target_handoff_does_not_prompt(monkeypatch, update, tmp_path):
    folder=tmp_path/'handoff';folder.mkdir()
    monkeypatch.setattr(cli,'resolve_update',lambda layout,target:update if target=='0.6.0' else pytest.fail('target changed'))
    monkeypatch.setattr(cli.tempfile,'mkdtemp',lambda **kw:str(folder))
    monkeypatch.setattr(cli.sys.stdin,'isatty',lambda:pytest.fail('explicit target prompted'))
    calls=[]
    def handoff(python,command):
        calls.append(command)
        data=json.loads(Path(command[-1]).read_text())
        assert data['target']=='0.6.0'
        raise OSError('controlled handoff end')
    monkeypatch.setattr(cli.os,'execv',handoff)
    assert cli.command(SimpleNamespace(to='0.6.0',check=False))==1
    assert len(calls)==1 and not folder.exists()


def test_external_native_commands_absent(monkeypatch):
    monkeypatch.setenv('WOLTSPACE_ISOLATION','external')
    parser=build_parser()
    for verb in ('update','start','stop','status','doctor','auto'):
        with pytest.raises(SystemExit):parser.parse_args([verb])
    assert parser.parse_args(['session','list']).command=='session'
