"""Run stop/install/start outside the uv environment being replaced."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SUPPORTED_VERSION = re.compile(r"\d+\.\d+\.\d+(?:\.post\d+)?")


class Failure(RuntimeError):
    pass


def run(command, env, *, timeout=600):
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise Failure(result.stderr.strip() or result.stdout.strip() or f'{command[0]} failed')
    return result.stdout.strip()


def apply(update, env):
    install = update['install']
    cli = install['cli']
    restart = False
    failed = False
    try:
        state = json.loads(run([cli, 'status', '--json'], env, timeout=30))['state']
        if state not in {'healthy', 'stopped'}:
            raise Failure(f'Lodge is {state}; resolve it before updating.')
        if state == 'healthy':
            print('Stopping the lodge control plane.', flush=True)
            run([cli, 'stop', '--json'], env, timeout=30)
            restart = True
        extras = '[' + ','.join(install['extras']) + ']' if install['extras'] else ''
        print(f"Installing Woltspace {update['target']} with uv.", flush=True)
        subprocess.run([install['uv'], 'tool', 'install', '--force', '--python', install['python'],
                        f"woltspace{extras}=={update['target']}"], env=env, check=True)
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Update failed: {exc}', file=sys.stderr)
        failed = True
    finally:
        if restart:
            try:
                print('Starting the lodge control plane.', flush=True)
                run([cli, 'start', '--json'], env, timeout=60)
            except Exception as exc:
                print(f'Restart failed: {exc}. Run woltspace doctor, then woltspace start.', file=sys.stderr)
                failed = True
    try:
        actual = run([cli, '--version'], env, timeout=30)
        print(f'Installed: {actual}')
        if not failed and actual != f"woltspace {update['target']}":
            raise Failure('Installed version differs from the requested version.')
    except Exception as exc:
        print(f'Version check failed: {exc}', file=sys.stderr)
        failed = True
    return 1 if failed else 0


def main(path):
    directory = Path(path).parent
    try:
        update = json.loads(Path(path).read_text())
        env = dict(os.environ)
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'WOLTSPACE_DIR', 'WOLTSPACE_ENTRYPOINT'):
            env.pop(key, None)
        env.update(UV_TOOL_DIR=update['install']['tool_root'],
                   WOLTSPACE_WOLTS_DIR=update['layout']['wolts_dir'], WOLTS_DIR=update['layout']['wolts_dir'],
                   WOLTSPACE_HOST=update['layout']['host'], WOLTSPACE_PORT=str(update['layout']['port']),
                   WOLTSPACE_ISOLATION='host')
        with (Path(update['install']['tool_root']) / '.woltspace-update.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Another update is running; nothing changed.', file=sys.stderr)
                return 1
            return apply(update, env)
    finally:
        if (directory / '.owned-update-stage').is_file():
            shutil.rmtree(directory)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as exc:
        print(f'Update failed: {exc}', file=sys.stderr)
        sys.exit(1)
