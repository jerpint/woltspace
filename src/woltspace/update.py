"""Native update planning; application runs outside the replaced tool environment."""
from __future__ import annotations

import hashlib
import json
import os
import re
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

API = 'https://api.github.com/repos/jerpint/woltspace'


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
    return {'cli': entries[0]['install-path'], 'uv': uv,
            'extras': req.get('extras', []), 'tool_root': str(prefix.parent),
            'python': str(Path(sys._base_executable).resolve())}


def stable(value):
    try:
        version = Version(value)
        return None if version.is_prerelease or version.is_devrelease else version
    except InvalidVersion:
        return None


def release_notes(components):
    """Page through both tag streams; review every published crossed release."""
    changed = [c for c in components if c['changed']]
    if not changed:
        return []
    notes = []
    for page in range(1, 101):
        batch = get_json(f'{API}/releases?per_page=100&page={page}')
        for release in batch:
            if release.get('draft') or release.get('prerelease'):
                continue
            tag = release['tag_name']
            for component in changed:
                prefix = component['tag_prefix']
                if not tag.startswith(prefix):
                    continue
                version = stable(tag[len(prefix):])
                if version and Version(component['current']) < version <= Version(component['target']):
                    notes.append({'component': component['name'], 'version': str(version),
                                  'url': release['html_url'], 'body': release.get('body') or ''})
        if len(batch) < 100:
            break
    else:
        raise UpdateError('Release history is too large to review completely.')
    for component in changed:
        if not any(n['component'] == component['name'] and n['version'] == component['target'] for n in notes):
            raise UpdateError(f"Release notes missing for {component['name']} {component['target']}; refusing an unreviewed update.")
    return sorted(notes, key=lambda n: (n['component'], Version(n['version'])))


def migrations(current, target):
    """Read migration prose before installation, from the exact target commit."""
    ref = get_json(f'{API}/git/ref/tags/v{target}')['object']
    for _ in range(5):
        if ref['type'] == 'commit':
            break
        if ref['type'] != 'tag':
            raise UpdateError('Release tag does not resolve to a commit.')
        ref = get_json(f"{API}/git/tags/{ref['sha']}")['object']
    else:
        raise UpdateError('Release tag nesting exceeded.')
    files = get_json(f"{API}/contents/container/migrations?ref={ref['sha']}")
    result = []
    for item in files:
        match = re.fullmatch(r'v?(.+)\.(md|sh)', item['name'])
        version = stable(match[1]) if match else None
        if not version or not (Version(current) < version <= Version(target)):
            continue
        request = urllib.request.Request(item['download_url'], headers={'User-Agent': 'woltspace-update'})
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode('utf-8')
        result.append({'version': str(version), 'name': item['name'], 'body': body,
                       'url': item['html_url'], 'sha256': hashlib.sha256(body.encode()).hexdigest()})
    return sorted(result, key=lambda m: (Version(m['version']), m['name']))


def build_plan(layout):
    if layout.isolation != 'host':
        raise UpdateError('Container updates belong to host-side container tooling.')
    install = native_install()
    package = get_json('https://pypi.org/pypi/woltspace/json')
    target = str(stable(package['info']['version']) or '')
    if not target:
        raise UpdateError('PyPI did not return a stable release.')
    wheel = {'name': 'woltspace', 'current': __version__, 'target': target,
             'tag_prefix': 'v', 'changed': Version(target) > Version(__version__)}
    components = [wheel]
    npm = shutil.which('npm')
    tui = None
    if npm:
        root = Path(run([npm, 'root', '-g']))
        metadata = root / '@woltspace' / 'tui' / 'package.json'
        if metadata.is_file():
            installed = json.loads(metadata.read_text())
            if installed.get('name') != '@woltspace/tui':
                raise UpdateError('Global TUI package identity mismatch.')
            registry = get_json('https://registry.npmjs.org/@woltspace%2Ftui/latest')
            target_tui = str(stable(registry['version']) or '')
            if not target_tui:
                raise UpdateError('npm did not return a stable TUI release.')
            tui = {'name': '@woltspace/tui', 'current': installed['version'],
                   'target': target_tui, 'tag_prefix': 'tui-v',
                   'changed': Version(target_tui) > Version(installed['version']),
                   'dist': registry['dist'], 'npm': npm,
                   'root': str(root)}
            components.append(tui)
    local_tui = shutil.which('woltspace-tui-service')
    if local_tui and not tui:
        raise UpdateError('TUI is installed outside npm global management; update it manually.')
    notes = release_notes(components)
    migration = migrations(__version__, target) if wheel['changed'] else []
    current = inspect_instance(layout)
    if current['state'] not in {'healthy', 'stopped'}:
        raise UpdateError(f"Control plane is {current['state']}; resolve it before updating.")
    return {'schema': 1, 'install': install, 'components': components, 'notes': notes,
            'migrations': migration, 'layout': {'wolts_dir': str(layout.wolts_dir),
            'host': layout.host, 'port': layout.port}, 'instance': current,
            'changed': any(c['changed'] for c in components),
            'impact': 'Control plane, tunnel and connectors briefly stop; tmux sessions survive. Existing sessions keep loaded instructions.'}


def print_plan(plan):
    for component in plan['components']:
        print(f"{component['name']}: {component['current']} → {component['target']}" +
              (' (update)' if component['changed'] else ' (unchanged)'))
    if len(plan['components']) == 1:
        print('TUI: no managed global installation; no TUI will be installed.')
    for note in plan['notes']:
        print(f"\n{note['component']} {note['version']}: {note['url']}\n{note['body']}")
    for migration in plan['migrations']:
        print(f"\nMigration {migration['name']}:\n{migration['body']}")
    if plan['changed']:
        print('\n' + plan['impact'])
        if plan['migrations']:
            print('Migration instructions require separate review; no migration script runs automatically.')
    else:
        print('Already current. Nothing changed.')


def command(args):
    try:
        if args.apply_plan:
            plan = json.loads(Path(args.apply_plan).read_text())
            if plan.get('schema') != 1:
                raise UpdateError('Unsupported update plan schema.')
            # Re-plan exact versions only through saved plan; never resolve latest on apply.
            install = native_install()
            if plan['install'] != install or plan['components'][0]['current'] != __version__:
                raise UpdateError('Installation changed since review; create a new plan.')
            layout = RuntimeLayout.from_env()
            if plan['layout'] != {'wolts_dir': str(layout.wolts_dir), 'host': layout.host, 'port': layout.port}:
                raise UpdateError('Plan belongs to another lodge endpoint.')
        else:
            plan = build_plan(RuntimeLayout.from_env())
        if args.json:
            print(json.dumps(plan, indent=2))
        else:
            print_plan(plan)
        if args.plan:
            path = Path(args.plan)
            path.write_text(json.dumps(plan, indent=2) + '\n')
            path.chmod(0o600)
        if args.check or not plan['changed']:
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise UpdateError('Applying requires approval: use --check to review, then --apply-plan with --yes after authorization.')
            if input('Apply this update? [y/N] ').strip().lower() not in {'y', 'yes'}:
                print('Cancelled. Nothing changed.')
                return 0
        # The helper uses only stdlib and the base interpreter. exec discards this
        # process before uv replaces the environment that loaded these modules.
        directory = Path(tempfile.mkdtemp(prefix='woltspace-update-'))
        worker = directory / 'worker.py'
        shutil.copyfile(Path(__file__).with_name('update_worker.py'), worker)
        path = directory / 'plan.json'
        path.write_text(json.dumps(plan))
        path.chmod(0o600)
        sys.stdout.flush()
        os.execv(plan['install']['python'], [plan['install']['python'], str(worker), str(path)])
    except (UpdateError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Update failed before handoff: {exc}', file=sys.stderr)
        return 1
