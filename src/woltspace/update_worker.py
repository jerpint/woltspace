"""Standalone stdlib updater. Copied out before replacing the uv environment."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import shutil
import uuid
import sys
import time


SUPPORTED_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.post\d+)?")


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
    if len(plan['components']) != 1 or plan['components'][0]['name'] != 'woltspace':
        raise Failure('Only the Woltspace Python package can be updated.')
    for component in plan['components']:
        for key in ('current', 'target'):
            if not SUPPORTED_VERSION.fullmatch(component[key]):
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
        staged_version = run([str(python), '-c',
            'from importlib.metadata import version; print(version("woltspace"))'], env)
        if staged_version != wheel['target']:
            raise Failure('Staged wheel version differs from requested target.')
        # Offline resolution preflight exercises the same pins while still live.
        run([uv, 'pip', 'install', '--dry-run', '--offline', '--python', str(python),
             '--constraint', str(constraints), spec], env)
        wheel['install_command'] = [uv, 'tool', 'install', '--force', '--offline',
            '--python', install['python'], '--constraint', str(constraints), spec]


def verify(plan, env, *, running):
    cli = plan['install']['cli']
    actual = run([cli, '--version'], env)
    wheel = plan['components'][0]
    expected = wheel['target'] if wheel['changed'] else wheel['current']
    if actual != f'woltspace {expected}':
        raise Failure(f'Expected woltspace {expected}, got {actual}.')
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
    try:
        with lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another update is running; nothing changed.', file=sys.stderr)
                return 1
            result = apply(plan, directory, env)
            report_dir = Path(plan['layout']['wolts_dir']) / '.space' / 'platform' / 'updates'
            report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            output = report_dir / (time.strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex + '.json')
            output.write_text(json.dumps(result, indent=2) + '\n')
            output.chmod(0o600)
            print(json.dumps(result, indent=2))
            print(f'Update report: {output}')
            return 0 if result['ok'] else 1
    finally:
        # Never remove an arbitrary plan's parent. Only our handoff creates
        # this marker, and reports have already moved into lodge state.
        if (directory / '.owned-update-stage').is_file():
            shutil.rmtree(directory)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:
        print(f'Updater failed: {exc}', file=sys.stderr)
        sys.exit(1)
