"""Standalone stdlib updater. Copied out before replacing the uv environment."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request


class Failure(RuntimeError):
    pass


def run(command, env, *, timeout=600):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Failure(f"{command[0]} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def status(cli, env):
    return json.loads(run([cli, 'status', '--json'], env, timeout=30))


def same_instance(before, after):
    if before['state'] != after['state']:
        return False
    return before['state'] == 'stopped' or (
        (before.get('health') or {}).get('instance_id') ==
        (after.get('health') or {}).get('instance_id')
    )


def validate_versions(plan):
    for component in plan['components']:
        for key in ('current', 'target'):
            if not re.fullmatch(r'\d+\.\d+\.\d+(?:\.post\d+)?', component[key]):
                raise Failure(f'Unsupported release version: {component[key]}')
    for extra in plan['install']['extras']:
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', extra):
            raise Failure('Invalid extra name.')


def stage(plan, directory, env):
    install = plan['install']
    uv = install['uv']
    wheel = plan['components'][0]
    if wheel['changed']:
        python = directory / 'staged' / 'bin' / 'python'
        run([uv, 'venv', '--python', install['python'], str(directory / 'staged')], env)
        extras = '[' + ','.join(install['extras']) + ']' if install['extras'] else ''
        spec = f"woltspace{extras}=={wheel['target']}"
        run([uv, 'pip', 'install', '--python', str(python), spec], env)
        # Lock every resolved Python dependency and retain the warmed cache.
        frozen = run([uv, 'pip', 'freeze', '--python', str(python)], env)
        constraints = directory / 'constraints.txt'
        constraints.write_text(frozen + '\n')
        # Compare the migration content actually shipping to the reviewed prose.
        staged_info = json.loads(run([str(python), '-c',
            'import json; from importlib.metadata import version; from woltspace.layout import installation_root; '
            'print(json.dumps({"version":version("woltspace"),"root":str(installation_root())}))'], env))
        if staged_info['version'] != wheel['target']:
            raise Failure('Staged wheel version differs from reviewed target.')
        root = Path(staged_info['root'])
        for migration in plan['migrations']:
            path = root / 'container' / 'migrations' / migration['name']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != migration['sha256']:
                raise Failure('Wheel migration content differs from review; nothing stopped.')
        # Offline resolution preflight exercises the same pins while still live.
        run([uv, 'pip', 'install', '--dry-run', '--offline', '--python', str(python),
             '--constraint', str(constraints), spec], env)
        wheel['install_command'] = [uv, 'tool', 'install', '--force', '--offline',
            '--python', install['python'], '--constraint', str(constraints), spec]
    for tui in plan['components'][1:]:
        if Path(run([tui['npm'], 'root', '-g'], env)).resolve() != Path(tui['root']).resolve():
            raise Failure('npm global root changed since review; nothing stopped.')
        metadata = Path(tui['root']) / '@woltspace' / 'tui' / 'package.json'
        if json.loads(metadata.read_text())['version'] != tui['current']:
            raise Failure('TUI changed since review; nothing stopped.')
        if not tui['changed']:
            continue
        tarball = directory / 'tui.tgz'
        with urllib.request.urlopen(tui['dist']['tarball'], timeout=30) as response:
            data = response.read()
        integrity = tui['dist'].get('integrity', '')
        candidates = integrity.split()
        if not any(value.startswith('sha512-') and value[7:] ==
                   base64.b64encode(hashlib.sha512(data).digest()).decode() for value in candidates):
            raise Failure('TUI tarball integrity mismatch or missing SHA-512.')
        tarball.write_bytes(data)
        cache = str(directory / 'npm-cache')
        run([tui['npm'], 'install', '--prefix', str(directory / 'npm-stage'),
             '--ignore-scripts', '--cache', cache, str(tarball)], env)
        command = [tui['npm'], 'install', '--global', '--offline', '--cache', cache, str(tarball)]
        run([*command, '--dry-run', '--ignore-scripts'], env)
        tui['install_command'] = command


def verify(plan, env, *, running):
    cli = plan['install']['cli']
    actual = run([cli, '--version'], env)
    wheel = plan['components'][0]
    expected = wheel['target'] if wheel['changed'] else wheel['current']
    if actual != f'woltspace {expected}':
        raise Failure(f'Expected woltspace {expected}, got {actual}.')
    for tui in plan['components'][1:]:
        actual_tui = json.loads((Path(tui['root']) / '@woltspace' / 'tui' / 'package.json').read_text())
        expected_tui = tui['target'] if tui['changed'] else tui['current']
        if actual_tui['version'] != expected_tui:
            raise Failure('TUI version verification failed.')
    deadline = time.monotonic() + 30
    while True:
        report = status(cli, env)
        health = report.get('health') or {}
        connectors = health.get('connectors') or []
        # Verify previously configured running connectors have resumed.
        expected_connectors = {c['name'] for c in (plan['instance'].get('health') or {}).get('connectors', [])
                               if c.get('state') == 'running'}
        running_connectors = {c['name'] for c in connectors if c.get('state') == 'running'}
        adoption = health.get('adoption') or {}
        previous_orphans = {(s.get('wolt'), s.get('session')) for s in
                            (plan['instance'].get('health') or {}).get('adoption', {}).get('orphaned', [])}
        new_orphans = {(s.get('wolt'), s.get('session')) for s in adoption.get('orphaned', [])} - previous_orphans
        if not running:
            if report['state'] != 'stopped':
                raise Failure('A previously stopped lodge changed state.')
            return report
        if (report['state'] == 'healthy' and expected_connectors <= running_connectors
                and not new_orphans
                and (not (plan['instance'].get('health') or {}).get('adoption') or adoption)):
            return report
        if time.monotonic() >= deadline:
            raise Failure('Verification failed: control-plane health, connector recovery or session adoption incomplete: ' + json.dumps(report))
        time.sleep(1)


def apply(plan, directory, env):
    validate_versions(plan)
    report = {'ok': False, 'completed': [], 'error': None, 'recovery': None}
    cli = plan['install']['cli']
    running = plan['instance']['state'] == 'healthy'
    stopped = False
    try:
        before = status(cli, env)
        if not same_instance(plan['instance'], before):
            raise Failure('Control-plane identity changed since review; create a new plan.')
        stage(plan, directory, env)
        if not same_instance(before, status(cli, env)):
            raise Failure('Control-plane identity changed during staging; nothing stopped.')
        if plan['components'][0]['changed'] and running:
            print('Stopping only the control plane; tmux sessions stay alive.', flush=True)
            run([cli, 'stop', '--json'], env, timeout=30)
            stopped = True
        for component in plan['components']:
            if component['changed']:
                run(component['install_command'], env)
                report['completed'].append({'name': component['name'], 'version': component['target']})
        if stopped:
            report['restart'] = json.loads(run([cli, 'start', '--json'], env, timeout=60))
            stopped = False
        report['status'] = verify(plan, env, running=running)
        report['migration_actions'] = plan['migrations']
        report['ok'] = True
    except (Exception, KeyboardInterrupt) as exc:
        report['error'] = str(exc) or type(exc).__name__
    finally:
        if stopped:
            try:
                report['recovery'] = json.loads(run([cli, 'start', '--json'], env, timeout=60))
            except Exception as exc:
                report['recovery'] = {'error': str(exc), 'action': 'Inspect the installed versions, then run woltspace doctor and woltspace start.'}
    # A failed installer may have changed files even with a nonzero exit.
    # Observe what is present rather than infer state from completed commands.
    observed = {}
    try:
        observed['woltspace'] = run([cli, '--version'], env, timeout=30)
    except Exception as exc:
        observed['woltspace'] = {'error': str(exc)}
    for tui in plan['components'][1:]:
        try:
            observed[tui['name']] = json.loads((Path(tui['root']) / '@woltspace' / 'tui' / 'package.json').read_text())['version']
        except Exception as exc:
            observed[tui['name']] = {'error': str(exc)}
    report['observed_versions'] = observed
    return report


def main(path):
    plan = json.loads(Path(path).read_text())
    directory = Path(path).parent
    env = dict(os.environ)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'WOLTSPACE_DIR', 'WOLTSPACE_ENTRYPOINT'):
        env.pop(key, None)
    env.update({'UV_TOOL_DIR': plan['install']['tool_root'], 'UV_CACHE_DIR': str(directory / 'uv-cache'),
                'WOLTSPACE_WOLTS_DIR': plan['layout']['wolts_dir'], 'WOLTS_DIR': plan['layout']['wolts_dir'],
                'WOLTSPACE_HOST': plan['layout']['host'], 'WOLTSPACE_PORT': str(plan['layout']['port']),
                'WOLTSPACE_ISOLATION': 'host'})
    lock_path = Path(plan['install']['tool_root']) / '.woltspace-update.lock'
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another update is running; nothing changed.', file=sys.stderr)
            return 1
        result = apply(plan, directory, env)
        output = directory / 'result.json'
        output.write_text(json.dumps(result, indent=2) + '\n')
        output.chmod(0o600)
        print(json.dumps(result, indent=2))
        print(f'Update report: {output}')
        if result['ok'] and result.get('migration_actions'):
            print('Package update verified. Reviewed migration instructions remain for the user/wolt to carry out; no migration script was executed.')
        return 0 if result['ok'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:
        print(f'Updater failed: {exc}', file=sys.stderr)
        sys.exit(1)
