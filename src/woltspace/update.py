"""Native Python update; application runs outside the replaced tool environment."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path

from packaging.version import Version, InvalidVersion
from . import __version__
from .layout import RuntimeLayout
from .instance import inspect_instance
from .update_worker import SUPPORTED_VERSION, validate_versions, Failure


class UpdateError(RuntimeError):
    pass


def get_json(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'woltspace-update'})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, **kwargs)
    if result.returncode:
        raise UpdateError(f"{command[0]} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip()


def native_install():
    """Refuse source/pip installs and custom uv recipes we cannot preserve."""
    prefix = Path(sys.prefix)
    receipt = prefix / 'uv-receipt.toml'
    if not receipt.is_file() or not Path(__file__).resolve().is_relative_to(prefix.resolve()):
        raise UpdateError('Updates support native uv tool installs only; see docs/updates.md.')
    tool = tomllib.loads(receipt.read_text())['tool']
    requirements = tool.get('requirements', [])
    if len(requirements) != 1 or requirements[0].get('name') != 'woltspace':
        raise UpdateError('Custom uv tool requirements need a manual upgrade.')
    req = requirements[0]
    if set(req) - {'name', 'extras', 'specifier'} or tool.get('options'):
        raise UpdateError('Custom uv sources/options need a manual upgrade.')
    entries = [e for e in tool['entrypoints'] if e['name'] == 'woltspace']
    if len(entries) != 1 or not Path(entries[0]['install-path']).is_file():
        raise UpdateError('The uv-installed woltspace entry point is missing.')
    uv = shutil.which('uv')
    if not uv:
        raise UpdateError('uv is required to update this installation.')
    if Path(run([uv, 'tool', 'dir', '--bin'])).resolve() != Path(entries[0]['install-path']).parent.resolve():
        raise UpdateError('uv tool bin directory differs from this entry point; use the matching UV_TOOL_BIN_DIR.')
    # Verify uv is addressing this very tool directory, including custom roots.
    if Path(run([uv, 'tool', 'dir'])).resolve() != prefix.parent.resolve():
        raise UpdateError('uv tool dir differs from this installation; use the matching UV_TOOL_DIR.')
    if not stable(__version__):
        raise UpdateError('Updates support stable three-part release versions only; upgrade prerelease/custom versions manually.')
    python = Path(sys.base_prefix) / 'bin' / f'python{sys.version_info.major}.{sys.version_info.minor}'
    if not python.is_file() or not os.access(python, os.X_OK):
        raise UpdateError('The base Python interpreter could not be located outside the tool environment.')
    return {'cli': entries[0]['install-path'], 'uv': uv,
            'extras': req.get('extras', []), 'tool_root': str(prefix.parent),
            'python': str(python.resolve())}


def stable(value):
    try:
        version = Version(value)
        return None if (version.is_prerelease or version.is_devrelease
                        or not SUPPORTED_VERSION.fullmatch(str(version))) else version
    except InvalidVersion:
        return None


def build_plan(layout, target=None):
    if layout.isolation != 'host':
        raise UpdateError('Container updates belong to host-side container tooling.')
    install = native_install()
    if target is not None and (not stable(target) or str(stable(target)) != target):
        raise UpdateError('Use an exact stable three-part version, for example --to 0.5.4.')
    url = f'https://pypi.org/pypi/woltspace/{target}/json' if target else 'https://pypi.org/pypi/woltspace/json'
    package = get_json(url)
    if package['info'].get('yanked'):
        reason = package['info'].get('yanked_reason') or 'no withdrawal reason provided'
        raise UpdateError(f'PyPI release is yanked: {reason}. Choose a non-yanked release.')
    published = str(stable(package['info']['version']) or '')
    if not published or (target is not None and published != target):
        raise UpdateError('PyPI did not return the requested stable release; check the version on PyPI.')
    target = published
    if Version(target) < Version(__version__):
        raise UpdateError('Downgrades are not supported by update; choose the installed version or a newer release.')
    wheel = {'name': 'woltspace', 'current': __version__, 'target': target,
             'changed': Version(target) > Version(__version__)}
    current = inspect_instance(layout)
    if current['state'] not in {'healthy', 'stopped'}:
        raise UpdateError(f"Control plane is {current['state']}; resolve it before updating.")
    return {'install': install, 'components': [wheel], 'layout': {'wolts_dir': str(layout.wolts_dir),
            'host': layout.host, 'port': layout.port}, 'instance': current,
            'changed': wheel['changed'],
            'impact': 'Control plane, tunnel and connectors briefly stop; tmux sessions survive. Existing sessions keep loaded instructions.'}


def print_plan(plan):
    for component in plan['components']:
        print(f"{component['name']}: {component['current']} → {component['target']}" +
              (' (update)' if component['changed'] else ' (unchanged)'))
    if plan['changed']:
        print('\n' + plan['impact'])
    else:
        print('Already current. Nothing changed.')


def command(args):
    directory = None
    try:
        plan = build_plan(RuntimeLayout.from_env(), args.to)
        validate_versions(plan)
        print_plan(plan)
        if args.check or not plan['changed']:
            return 0
        # An explicit target is the caller's instruction to install that exact
        # version. Bare update provides the interactive human convenience path.
        if not args.to:
            if not sys.stdin.isatty():
                raise UpdateError('Use --check to inspect, or --to VERSION to install an explicitly chosen version.')
            if input('Apply this update? [y/N] ').strip().lower() not in {'y', 'yes'}:
                print('Cancelled. Nothing changed.')
                return 0
        # The helper uses only stdlib and the base interpreter. exec discards this
        # process before uv replaces the environment that loaded these modules.
        directory = Path(tempfile.mkdtemp(prefix='woltspace-update-'))
        (directory / '.owned-update-stage').touch()
        worker = directory / 'worker.py'
        shutil.copyfile(Path(__file__).with_name('update_worker.py'), worker)
        path = directory / 'plan.json'
        path.write_text(json.dumps(plan))
        path.chmod(0o600)
        sys.stdout.flush()
        os.execv(plan['install']['python'], [plan['install']['python'], str(worker), str(path)])
    except (UpdateError, Failure, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        if directory is not None:
            shutil.rmtree(directory)
        print(f'Update failed before handoff: {exc}', file=sys.stderr)
        return 1
