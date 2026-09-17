"""Verify built Python/npm artifacts contain the reviewed source and versions."""

import json
import re
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist'
python_version = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
npm_manifest = json.loads((ROOT / 'tui/package.json').read_text())
tui_version = npm_manifest['version']

with zipfile.ZipFile(DIST / f'woltspace-{python_version}-py3-none-any.whl') as wheel:
    metadata = next(name for name in wheel.namelist() if name.endswith('.dist-info/METADATA'))
    assert Parser().parsestr(wheel.read(metadata).decode())['Version'] == python_version
    required = [*ROOT.glob('container/lib/*.py'),
                *ROOT.glob('container/skills/*/SKILL.md'),
                ROOT / 'docs/shared-skills.md', ROOT / 'container/bin/notify']
    for source in required:
        relative = source.relative_to(ROOT).as_posix()
        assert wheel.read(f'woltspace/_bundle/{relative}') == source.read_bytes(), relative
    assert (wheel.getinfo('woltspace/_bundle/container/bin/notify').external_attr >> 16) & 0o111

with tarfile.open(DIST / f'woltspace-{python_version}.tar.gz') as sdist:
    for source in [*required, ROOT / 'pyproject.toml', ROOT / 'container/Dockerfile']:
        relative = source.relative_to(ROOT).as_posix()
        assert sdist.extractfile(f'woltspace-{python_version}/{relative}').read() == source.read_bytes(), relative

    assert sdist.getmember(f'woltspace-{python_version}/container/bin/notify').mode & 0o111

with tarfile.open(DIST / f'woltspace-tui-{tui_version}.tgz') as npm:
    assert json.load(npm.extractfile('package/package.json')) == npm_manifest
    for source in (ROOT / 'tui/src').rglob('*.js'):
        relative = source.relative_to(ROOT / 'tui').as_posix()
        assert npm.extractfile(f'package/{relative}').read() == source.read_bytes(), relative

runtime = (ROOT / 'tui/src/version.js').read_text()
assert re.search(r"packageVersion = ['\"]" + re.escape(tui_version) + r"['\"]", runtime)
dockerfile = (ROOT / 'container/Dockerfile').read_text()
assert f'ARG WOLTSPACE_PYPI_VERSION={python_version}\n' in dockerfile
assert f'ARG WOLTSPACE_TUI_VERSION={tui_version}\n' in dockerfile
print(f'Verified Python {python_version} and TUI {tui_version}: source contents, identities and Docker pins.')
