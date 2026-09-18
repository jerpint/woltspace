"""Prepare, verify and resume a paired release; publishing stays in gated jobs."""
import argparse
import base64
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REPO = 'jerpint/woltspace'
OWNER = 'jerpint'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def get_json(url, missing_ok=False):
    try:
        with urllib.request.urlopen(urllib.request.Request(
            url, headers={'User-Agent': 'woltspace-release-verifier'}), timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        if error.code == 404 and missing_ok:
            return None
        raise


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def remote_digest(url, algorithm='sha256'):
    require(url.startswith('https://'), 'Registry download must use HTTPS')
    with urllib.request.urlopen(url, timeout=60) as response:
        return hashlib.new(algorithm, response.read()).hexdigest()


def check_environments():
    for name in ('pypi', 'npm'):
        env = get_json(f'https://api.github.com/repos/{REPO}/environments/{name}')
        require(env.get('can_admins_bypass') is False, f'{name}: disable administrator bypass')
        rules = [r for r in env['protection_rules'] if r['type'] == 'required_reviewers']
        require(len(rules) == 1, f'{name}: required reviewer protection missing')
        # The sole reviewer must be able to approve a run they started.
        require(rules[0].get('prevent_self_review') is False,
                f'{name}: allow self-review so the sole owner can approve their run')
        reviewers = rules[0]['reviewers']
        require(len(reviewers) == 1 and reviewers[0]['type'] == 'User'
                and reviewers[0]['reviewer']['login'] == OWNER,
                f'{name}: only {OWNER} may approve publication')
        policy = env.get('deployment_branch_policy') or {}
        require(policy.get('custom_branch_policies') is True
                and policy.get('protected_branches') is False,
                f'{name}: select custom deployment branches')
        branches = get_json(f'https://api.github.com/repos/{REPO}/environments/{name}/deployment-branch-policies')['branch_policies']
        require(len(branches) == 1 and branches[0]['name'] == 'main'
                and branches[0]['type'] == 'branch', f'{name}: permit only main, no tags')
    print('Both publishing environments require jerpint approval, main only, no bypass.')


def output(key, value):
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as handle:
            handle.write(f'{key}={value}\n')


def source_run():
    """Admit retained artifacts only from a validated main publishing run."""
    run_id = os.environ.get('RESUME_RUN_ID', '')
    if not run_id:
        output('release_commit', os.environ['GITHUB_SHA'])
        return
    require(re.fullmatch(r'[0-9]+', run_id) is not None, 'Original run ID must be numeric')
    base = f'https://api.github.com/repos/{REPO}/actions/runs/{run_id}'
    run = get_json(base)
    require(run['head_repository']['full_name'] == REPO and run['head_branch'] == 'main'
            and run['path'] == '.github/workflows/publish.yml'
            and run['event'] == 'workflow_dispatch' and run['status'] == 'completed',
            'Resume requires a completed main publishing run from this repository')
    sha = run['head_sha']
    require(re.fullmatch(r'[0-9a-f]{40}', sha) is not None, 'Original commit is invalid')
    required = {'guard', 'validation / tests (3.11)', 'validation / tests (3.13)',
                'validation / packages', 'prepare'}
    successful = set()
    page = 1
    while True:
        jobs = get_json(f'{base}/jobs?filter=all&per_page=100&page={page}')['jobs']
        successful.update(job['name'] for job in jobs if job['conclusion'] == 'success')
        if len(jobs) < 100:
            break
        page += 1
    require(required <= successful, 'Original run did not validate and prepare release artifacts')
    output('release_commit', sha)
    print(f'Resuming original artifacts from run {run_id}, commit {sha}; do not rebuild.')


def load_manifest():
    manifest = json.loads((ROOT / 'release-manifest.json').read_text())
    expected_commit = os.environ.get('RELEASE_COMMIT') or os.environ['GITHUB_SHA']
    require(manifest['commit'] == expected_commit, 'Artifact commit differs from approved source run')
    for name, sha in manifest['files'].items():
        require(Path(name).name == name, 'Unsafe artifact filename')
        require(digest(ROOT / 'dist' / name) == sha, f'Artifact digest mismatch: {name}')
    return manifest


def prepare():
    import tomllib
    check_environments()
    versions = {
        'python': tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version'],
        'npm': json.loads((ROOT / 'tui/package.json').read_text())['version'],
    }
    require(versions['python'] == os.environ['EXPECTED_PYTHON'], 'Confirm the Python version from this commit')
    require(versions['npm'] == os.environ['EXPECTED_TUI'], 'Confirm the TUI version from this commit')
    files = [f'woltspace-{versions["python"]}-py3-none-any.whl',
             f'woltspace-{versions["python"]}.tar.gz',
             f'woltspace-tui-{versions["npm"]}.tgz']
    require({p.name for p in (ROOT / 'dist').iterdir()} == set(files), 'Unexpected or missing artifacts')
    source_commit = os.environ.get('RELEASE_COMMIT') or os.environ['GITHUB_SHA']
    manifest = {'commit': source_commit, 'versions': versions,
                'files': {name: digest(ROOT / 'dist' / name) for name in files}}
    previous_release_targets = []
    if os.environ.get('RESUME_RUN_ID'):
        retained = load_manifest()
        require({key: retained[key] for key in manifest} == manifest,
                'Retained release manifest differs; do not rebuild or substitute artifacts')
        previous_release_targets = retained.get('release_targets', retained.get('targets', ['python', 'npm']))
    target = os.environ.get('REGISTRY_TARGET', 'both')
    require(target in ('python', 'npm', 'both'), 'Choose python, npm or both')
    manifest['targets'] = ['python', 'npm'] if target == 'both' else [target]
    manifest['release_targets'] = [registry for registry in ('python', 'npm')
                                   if registry in manifest['targets'] or registry in previous_release_targets]
    manifest['verify_targets'] = manifest['release_targets']
    # Plan before approval. An unchanged registry gets verified, never staged.
    # Dry runs deliberately avoid registry access and only rehearse selected gates.
    for registry in ('python', 'npm'):
        missing = registry_state(registry, manifest) if os.environ.get('DRY_RUN') == 'false' else []
        if registry not in manifest['targets']:
            require(not missing,
                    f'Unselected {registry} version must already be available; choose both to publish it')
        output(f'publish_{registry}', 'true' if registry in manifest['targets'] and missing else 'false')
    (ROOT / 'release-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    summary = f'Commit: `{manifest["commit"]}`\n\nPython: **{versions["python"]}**; TUI: **{versions["npm"]}**\n\n'
    summary += f'Publish targets: **{", ".join(manifest["targets"])}**\n\n'
    summary += '\n'.join(f'- `{name}` SHA256 `{sha}`' for name, sha in manifest['files'].items())
    summary += '\n\nPublication still requires jerpint approval for each registry.\n'
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as handle:
            handle.write(summary)
    print(summary)


def registry_state(registry, manifest):
    version = manifest['versions'][registry]
    selected = registry in manifest.get('verify_targets', manifest.get('targets', ['python', 'npm']))
    if registry == 'python':
        wanted = {name: sha for name, sha in manifest['files'].items() if not name.endswith('.tgz')}
        data = get_json(f'https://pypi.org/pypi/woltspace/{version}/json', missing_ok=True)
        if data is None:
            return list(wanted)
        found = {item['filename']: item for item in data['urls']}
        require(set(found) <= set(wanted), 'PyPI version contains unexpected artifacts')
        for name, item in found.items():
            sha = wanted[name] if selected else item['digests']['sha256']
            require(item['digests']['sha256'] == sha, f'PyPI digest mismatch: {name}. Version exists with different bytes; reuse the original run artifacts, or investigate another publisher.')
            require(remote_digest(item['url']) == sha, f'PyPI download mismatch: {name}')
        return [name for name in wanted if name not in found]
    name = next(name for name in manifest['files'] if name.endswith('.tgz'))
    package = urllib.parse.quote('@woltspace/tui', safe='')
    data = get_json(f'https://registry.npmjs.org/{package}/{version}', missing_ok=True)
    if data is None:
        return [name]
    require(data['name'] == '@woltspace/tui' and data['version'] == version, 'npm identity mismatch')
    if selected:
        require(remote_digest(data['dist']['tarball']) == manifest['files'][name], 'npm tarball differs from approved artifact. Version exists with different bytes; reuse the original run artifacts, or investigate another publisher.')
    else:
        integrity = data['dist']['integrity']
        require(integrity.startswith('sha512-'), 'Existing npm package must have SHA512 integrity')
        sha = base64.b64decode(integrity.removeprefix('sha512-'), validate=True).hex()
        require(remote_digest(data['dist']['tarball'], 'sha512') == sha, 'Pinned npm download integrity mismatch')
    return []


def check(registry, verify=False):
    manifest = load_manifest()
    selected = registry in manifest.get('targets', ['python', 'npm'])
    require(verify or selected, 'An unselected registry must never stage files for publishing')
    for attempt in range(12 if verify else 1):
        missing = registry_state(registry, manifest)
        if not missing:
            exact = registry in manifest.get('verify_targets', manifest.get('targets', ['python', 'npm']))
            description = 'exact approved bytes' if exact else 'existing pinned registry files and their integrity'
            print(f'Verified {registry} {manifest["versions"][registry]}: {description} are available.')
            output('missing', 'false')
            return
        if verify:
            if attempt < 11:
                time.sleep(5)
            continue
        output('missing', 'true')
        target = ROOT / 'publish-dist'
        target.mkdir(exist_ok=False)
        for name in missing:
            shutil.copyfile(ROOT / 'dist' / name, target / name)
        print(f'{registry}: only missing artifacts will be published: {missing}')
        return
    raise RuntimeError(f'{registry}: approved artifacts are not fully available')


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True).strip()


def tag_commit(tag):
    refs = json.loads(gh('api', f'repos/{REPO}/git/matching-refs/tags/{tag}'))
    exact = [ref for ref in refs if ref['ref'] == f'refs/tags/{tag}']
    if not exact:
        return None
    obj = exact[0]['object']
    while obj['type'] == 'tag':
        obj = json.loads(gh('api', f'repos/{REPO}/git/tags/{obj["sha"]}'))['object']
    require(obj['type'] == 'commit', f'{tag}: tag must resolve to a commit')
    return obj['sha']


def finalize():
    manifest = load_manifest()
    require(not registry_state('python', manifest) and not registry_state('npm', manifest),
            'Both registries must match before creating releases')
    notes = ROOT / 'docs/release-notes.md'
    require(notes.is_file(), 'Review docs/release-notes.md before publishing')
    for registry, tag in [('npm', f'tui-v{manifest["versions"]["npm"]}'),
                          ('python', f'v{manifest["versions"]["python"]}')]:
        if registry not in manifest.get('release_targets', manifest.get('targets', ['python', 'npm'])):
            # Preserve an independently released registry's tags and assets.
            continue
        existing_commit = tag_commit(tag)
        releases = json.loads(gh('api', '--paginate', '--slurp', f'repos/{REPO}/releases?per_page=100'))
        release = next((r for page in releases for r in page if r['tag_name'] == tag), None)
        require(existing_commit is None or existing_commit == manifest['commit']
                or (registry == 'npm' and release is not None and not release['draft']),
                f'{tag}: existing tag points elsewhere')
        if release is None:
            gh('release', 'create', tag, '--repo', REPO, '--target', manifest['commit'],
               '--draft', '--latest=false', '--title', tag, '--notes-file', str(notes))
        wanted = {name: sha for name, sha in manifest['files'].items()
                  if name.endswith('.tgz') == (registry == 'npm')}
        release = json.loads(gh('release', 'view', tag, '--repo', REPO, '--json', 'assets,isDraft,targetCommitish'))
        assets = {asset['name'] for asset in release['assets']}
        require(assets <= set(wanted), f'{tag}: unexpected release assets')
        for name, sha in wanted.items():
            if name not in assets:
                gh('release', 'upload', tag, str(ROOT / 'dist' / name), '--repo', REPO)
            with tempfile.TemporaryDirectory() as temp:
                gh('release', 'download', tag, '--pattern', name, '--dir', temp, '--repo', REPO)
                require(digest(Path(temp) / name) == sha, f'{tag}: release asset mismatch: {name}')
        if existing_commit is None:
            # GitHub can retain a draft without creating its tag. Create the
            # ref explicitly after verifying assets, before publishing it.
            require(release['isDraft'] and release['targetCommitish'] == manifest['commit'],
                    f'{tag}: untagged draft must target the approved commit')
            gh('api', f'repos/{REPO}/git/refs', '-f', f'ref=refs/tags/{tag}',
               '-f', f'sha={manifest["commit"]}')
        require(tag_commit(tag) == (existing_commit or manifest['commit']), f'{tag}: tag commit mismatch')
        if release['isDraft']:
            gh('release', 'edit', tag, '--repo', REPO, '--draft=false',
               f'--latest={"true" if registry == "python" else "false"}')
        print(f'Verified GitHub release {tag} at {existing_commit or manifest["commit"]}.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['environments', 'source-run', 'prepare', 'check', 'verify', 'finalize'])
    parser.add_argument('registry', nargs='?', choices=['python', 'npm'])
    args = parser.parse_args()
    if args.command in ('check', 'verify'):
        require(args.registry is not None, 'Registry required')
        check(args.registry, verify=args.command == 'verify')
    else:
        {'environments': check_environments, 'source-run': source_run,
         'prepare': prepare, 'finalize': finalize}[args.command]()
