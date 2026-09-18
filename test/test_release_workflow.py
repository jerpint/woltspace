"""Release admission and immutable-artifact recovery, without registry writes."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('release', Path(__file__).resolve().parents[1] / 'scripts/release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def environment():
    return {'can_admins_bypass': False,
            'protection_rules': [{'type': 'required_reviewers', 'prevent_self_review': False, 'reviewers': [
                {'type': 'User', 'reviewer': {'login': 'jerpint'}}]}],
            'deployment_branch_policy': {'custom_branch_policies': True, 'protected_branches': False}}


@pytest.mark.parametrize('change', ['bypass', 'no_reviewers', 'bot_reviewer', 'additional_reviewer',
                                     'self_review_blocked', 'all_branches', 'protected_branches', 'tag', 'other_branch', 'extra_branch'])
def test_release_refuses_inadequate_environment_protection(monkeypatch, change):
    env = environment()
    branches = [{'name': 'main', 'type': 'branch'}]
    if change == 'bypass':
        env['can_admins_bypass'] = True
    elif change == 'self_review_blocked':
        env['protection_rules'][0]['prevent_self_review'] = True
    elif change == 'no_reviewers':
        env['protection_rules'] = []
    elif change == 'bot_reviewer':
        env['protection_rules'][0]['reviewers'][0]['reviewer']['login'] = 'woltspace-jerpint[bot]'
    elif change == 'additional_reviewer':
        env['protection_rules'][0]['reviewers'].append({'type': 'User', 'reviewer': {'login': 'someone'}})
    elif change == 'all_branches':
        env['deployment_branch_policy'] = None
    elif change == 'protected_branches':
        env['deployment_branch_policy'] = {'protected_branches': True, 'custom_branch_policies': False}
    elif change == 'tag':
        branches[0]['type'] = 'tag'
    elif change == 'other_branch':
        branches[0]['name'] = 'release/*'
    elif change == 'extra_branch':
        branches.append({'name': 'develop', 'type': 'branch'})
    monkeypatch.setattr(release, 'get_json', lambda url: {'branch_policies': branches} if url.endswith('deployment-branch-policies') else env)
    with pytest.raises(RuntimeError):
        release.check_environments()


def test_both_environments_are_checked(monkeypatch):
    calls = []
    def read(url):
        calls.append(url)
        return {'branch_policies': [{'name': 'main', 'type': 'branch'}]} if url.endswith('deployment-branch-policies') else environment()
    monkeypatch.setattr(release, 'get_json', read)
    release.check_environments()
    assert any('/environments/pypi/' in url for url in calls)
    assert any('/environments/npm/' in url for url in calls)


@pytest.fixture
def artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'ROOT', tmp_path)
    monkeypatch.setenv('GITHUB_SHA', 'reviewed-commit')
    dist = tmp_path / 'dist'
    dist.mkdir()
    files = {'woltspace-0.5.5-py3-none-any.whl': b'wheel',
             'woltspace-0.5.5.tar.gz': b'source', 'woltspace-tui-0.5.2.tgz': b'tui'}
    for name, data in files.items():
        (dist / name).write_bytes(data)
    manifest = {'commit': 'reviewed-commit', 'versions': {'python': '0.5.5', 'npm': '0.5.2'},
                'files': {name: release.digest(dist / name) for name in files}}
    (tmp_path / 'release-manifest.json').write_text(json.dumps(manifest))
    return manifest


@pytest.mark.parametrize('change', ['commit', 'bytes', 'unsafe_name'])
def test_artifact_handoff_refuses_tampering(artifacts, monkeypatch, change):
    if change == 'commit':
        monkeypatch.setenv('GITHUB_SHA', 'different-commit')
    elif change == 'bytes':
        (release.ROOT / 'dist/woltspace-tui-0.5.2.tgz').write_bytes(b'changed')
    else:
        artifacts['files']['../escape'] = 'abc'
        (release.ROOT / 'release-manifest.json').write_text(json.dumps(artifacts))
    with pytest.raises(RuntimeError):
        release.load_manifest()


def pypi_data(manifest, names=None):
    names = names if names is not None else [n for n in manifest['files'] if not n.endswith('.tgz')]
    return {'urls': [{'filename': n, 'digests': {'sha256': manifest['files'][n]},
                      'url': f'https://files.example/{n}'} for n in names]}


def registry_downloads(monkeypatch, manifest):
    monkeypatch.setattr(release, 'remote_digest', lambda url: manifest['files'][url.rsplit('/', 1)[1]])


@pytest.mark.parametrize('registry', ['python', 'npm'])
def test_absent_version_is_staged_without_rebuilding(artifacts, monkeypatch, registry):
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: None)
    release.check(registry)
    staged = list((release.ROOT / 'publish-dist').iterdir())
    assert len(staged) == (2 if registry == 'python' else 1)
    assert all(release.digest(p) == artifacts['files'][p.name] for p in staged)


@pytest.mark.parametrize('registry', ['python', 'npm'])
def test_matching_version_is_verified_and_not_republished(artifacts, monkeypatch, registry, capsys):
    data = pypi_data(artifacts) if registry == 'python' else {
        'name': '@woltspace/tui', 'version': '0.5.2',
        'dist': {'tarball': 'https://files.example/woltspace-tui-0.5.2.tgz'}}
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: data)
    registry_downloads(monkeypatch, artifacts)
    release.check(registry)
    assert not (release.ROOT / 'publish-dist').exists()
    assert f'Verified {registry}' in capsys.readouterr().out


def test_half_uploaded_python_version_stages_only_missing_file(artifacts, monkeypatch):
    wheel = 'woltspace-0.5.5-py3-none-any.whl'
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: pypi_data(artifacts, [wheel]))
    registry_downloads(monkeypatch, artifacts)
    release.check('python')
    assert [p.name for p in (release.ROOT / 'publish-dist').iterdir()] == ['woltspace-0.5.5.tar.gz']


@pytest.mark.parametrize('registry', ['python', 'npm'])
def test_existing_version_with_different_download_stops(artifacts, monkeypatch, registry):
    data = pypi_data(artifacts) if registry == 'python' else {
        'name': '@woltspace/tui', 'version': '0.5.2',
        'dist': {'tarball': 'https://files.example/woltspace-tui-0.5.2.tgz'}}
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: data)
    monkeypatch.setattr(release, 'remote_digest', lambda url: 'different-sha')
    with pytest.raises(RuntimeError, match='mismatch|differs'):
        release.check(registry)
    assert not (release.ROOT / 'publish-dist').exists()


def test_pypi_metadata_mismatch_stops_before_download(artifacts, monkeypatch):
    data = pypi_data(artifacts)
    data['urls'][0]['digests']['sha256'] = 'wrong'
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: data)
    monkeypatch.setattr(release, 'remote_digest', lambda url: pytest.fail('must reject metadata first'))
    with pytest.raises(RuntimeError, match='digest mismatch'):
        release.check('python')


def test_releases_are_never_created_with_missing_registry_half(artifacts, monkeypatch):
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: [] if registry == 'python' else ['missing.tgz'])
    monkeypatch.setattr(release, 'gh', lambda *args: pytest.fail('must not create any tag or release'))
    with pytest.raises(RuntimeError, match='Both registries'):
        release.finalize()


def test_failed_registry_verification_cannot_claim_success(artifacts, monkeypatch, capsys):
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: None)
    monkeypatch.setattr(release.time, 'sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='not fully available'):
        release.check('npm', verify=True)
    assert 'Verified npm' not in capsys.readouterr().out


def fake_releases(monkeypatch, artifacts, existing=None):
    """Model GitHub draft recovery and asset downloads; no network or writes."""
    releases = existing or {}
    calls = []
    def fake_gh(*args):
        calls.append(args)
        if args[:1] == ('api',):
            return json.dumps([[{'tag_name': tag, 'draft': value['draft']}
                                for tag, value in releases.items()]])
        action, tag = args[1:3]
        if action == 'create':
            releases[tag] = {'draft': True, 'commit': artifacts['commit'], 'assets': []}
        elif action == 'view':
            value = releases[tag]
            return json.dumps({'isDraft': value['draft'], 'assets': [{'name': n} for n in value['assets']]})
        elif action == 'upload':
            releases[tag]['assets'].append(Path(args[3]).name)
        elif action == 'download':
            name = args[args.index('--pattern') + 1]
            target = Path(args[args.index('--dir') + 1]) / name
            target.write_bytes((release.ROOT / 'dist' / name).read_bytes())
        elif action == 'edit':
            releases[tag]['draft'] = False
        return ''
    monkeypatch.setattr(release, 'gh', fake_gh)
    monkeypatch.setattr(release, 'tag_commit', lambda tag: releases.get(tag, {}).get('commit'))
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: [])
    (release.ROOT / 'docs').mkdir()
    (release.ROOT / 'docs/release-notes.md').write_text('Reviewed release changes')
    return calls, releases


def test_no_tags_required_before_first_successful_publication(artifacts, monkeypatch):
    calls, releases = fake_releases(monkeypatch, artifacts)
    release.finalize()
    created = [call[2] for call in calls if call[:2] == ('release', 'create')]
    edited = [call for call in calls if call[:2] == ('release', 'edit')]
    assert created == ['tui-v0.5.2', 'v0.5.5']
    assert '--latest=false' in edited[0]
    assert '--latest=true' in edited[1]
    assert all(not r['draft'] for r in releases.values())
    assert all(call[call.index('--target') + 1] == artifacts['commit']
               for call in calls if call[:2] == ('release', 'create'))


def test_draft_retry_keeps_already_uploaded_assets(artifacts, monkeypatch):
    existing = {'tui-v0.5.2': {'draft': False, 'commit': artifacts['commit'], 'assets': ['woltspace-tui-0.5.2.tgz']},
                'v0.5.5': {'draft': True, 'commit': artifacts['commit'], 'assets': ['woltspace-0.5.5.tar.gz']}}
    calls, releases = fake_releases(monkeypatch, artifacts, existing)
    release.finalize()
    uploaded = [Path(call[3]).name for call in calls if call[:2] == ('release', 'upload')]
    assert uploaded == ['woltspace-0.5.5-py3-none-any.whl']
    assert not any(call[:2] == ('release', 'create') for call in calls)
    assert not releases['v0.5.5']['draft']


def test_unchanged_tui_can_reuse_original_public_release(artifacts, monkeypatch):
    existing = {'tui-v0.5.2': {'draft': False, 'commit': 'previous-reviewed-commit', 'assets': ['woltspace-tui-0.5.2.tgz']}}
    calls, releases = fake_releases(monkeypatch, artifacts, existing)
    release.finalize()
    assert releases['tui-v0.5.2']['commit'] == 'previous-reviewed-commit'
    assert not any(call[:3] == ('release', 'edit', 'tui-v0.5.2') for call in calls)
    assert releases['v0.5.5']['commit'] == artifacts['commit']


def test_python_tag_on_another_commit_stops(artifacts, monkeypatch):
    existing = {'v0.5.5': {'draft': True, 'commit': 'unreviewed-commit', 'assets': []}}
    calls, releases = fake_releases(monkeypatch, artifacts, existing)
    with pytest.raises(RuntimeError, match='points elsewhere'):
        release.finalize()
    assert releases['v0.5.5']['draft']
    assert not any(call[:3] == ('release', 'upload', 'v0.5.5') for call in calls)


def test_release_asset_mismatch_does_not_publish_draft(artifacts, monkeypatch):
    calls, releases = fake_releases(monkeypatch, artifacts)
    original = release.gh
    def corrupt_download(*args):
        result = original(*args)
        if args[:2] == ('release', 'download'):
            name = args[args.index('--pattern') + 1]
            (Path(args[args.index('--dir') + 1]) / name).write_bytes(b'wrong release asset')
        return result
    monkeypatch.setattr(release, 'gh', corrupt_download)
    with pytest.raises(RuntimeError, match='asset mismatch'):
        release.finalize()
    assert releases['tui-v0.5.2']['draft']
    assert not any(call[:2] == ('release', 'edit') for call in calls)


@pytest.mark.parametrize('status', [401, 403, 429, 500])
def test_registry_read_errors_never_become_publish_permission(monkeypatch, status):
    import urllib.error
    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, 'registry unavailable', {}, None)
    monkeypatch.setattr(release.urllib.request, 'urlopen', fail)
    with pytest.raises(urllib.error.HTTPError):
        release.get_json('https://registry.npmjs.org/example/1.0.0', missing_ok=True)


@pytest.mark.parametrize('registry', ['python', 'npm'])
def test_prepare_rejects_version_confirmation_for_another_commit(artifacts, monkeypatch, registry):
    monkeypatch.setattr(release, 'check_environments', lambda: None)
    (release.ROOT / 'pyproject.toml').write_text('[project]\nversion="0.5.5"\n')
    (release.ROOT / 'tui').mkdir()
    (release.ROOT / 'tui/package.json').write_text('{"version":"0.5.2"}')
    monkeypatch.setenv('EXPECTED_PYTHON', '0.5.5' if registry == 'npm' else '0.5.4')
    monkeypatch.setenv('EXPECTED_TUI', '0.5.2' if registry == 'python' else '0.5.1')
    with pytest.raises(RuntimeError, match='Confirm'):
        release.prepare()
