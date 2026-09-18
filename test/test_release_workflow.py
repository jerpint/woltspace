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
    tags = {tag: value['commit'] for tag, value in releases.items() if value.get('tag_exists', True)}
    def fake_gh(*args):
        calls.append(args)
        if args[:1] == ('api',):
            if args[1].endswith('/git/refs'):
                fields = dict(arg.split('=', 1) for arg in args if arg.startswith(('ref=', 'sha=')))
                tag = fields['ref'].removeprefix('refs/tags/')
                assert tag not in tags, 'existing tags must never be overwritten'
                tags[tag] = fields['sha']
                return '{}'
            return json.dumps([[{'tag_name': tag, 'draft': value['draft']}
                                for tag, value in releases.items()]])
        action, tag = args[1:3]
        if action == 'create':
            releases[tag] = {'draft': True, 'commit': artifacts['commit'], 'assets': []}
        elif action == 'view':
            value = releases[tag]
            return json.dumps({'isDraft': value['draft'], 'targetCommitish': value['commit'], 'assets': [{'name': n} for n in value['assets']]})
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
    monkeypatch.setattr(release, 'tag_commit', lambda tag: tags.get(tag))
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


def configure_prepare(monkeypatch):
    monkeypatch.setattr(release, 'check_environments', lambda: None)
    (release.ROOT / 'pyproject.toml').write_text('[project]\nversion="0.5.5"\n')
    (release.ROOT / 'tui').mkdir()
    (release.ROOT / 'tui/package.json').write_text('{"version":"0.5.2"}')
    monkeypatch.setenv('EXPECTED_PYTHON', '0.5.5')
    monkeypatch.setenv('EXPECTED_TUI', '0.5.2')
    monkeypatch.setenv('DRY_RUN', 'false')


@pytest.mark.parametrize('target', ['python', 'npm', 'both'])
def test_plan_requires_approval_only_for_missing_selected_registry(artifacts, monkeypatch, target):
    configure_prepare(monkeypatch)
    monkeypatch.setenv('REGISTRY_TARGET', target)
    selected = ['python', 'npm'] if target == 'both' else [target]
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: ['missing'] if registry in selected else [])
    outputs = {}
    monkeypatch.setattr(release, 'output', lambda key, value: outputs.update({key: value}))
    release.prepare()
    assert release.load_manifest()['targets'] == selected
    assert outputs == {f'publish_{registry}': str(registry in selected).lower() for registry in ('python', 'npm')}


def test_plan_skips_both_approvals_when_artifacts_already_exist(artifacts, monkeypatch):
    configure_prepare(monkeypatch)
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: [])
    outputs = {}
    monkeypatch.setattr(release, 'output', lambda key, value: outputs.update({key: value}))
    release.prepare()
    assert outputs == {'publish_python': 'false', 'publish_npm': 'false'}


def test_missing_unselected_pin_stops_before_any_publication(artifacts, monkeypatch):
    configure_prepare(monkeypatch)
    monkeypatch.setenv('REGISTRY_TARGET', 'npm')
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: ['missing'])
    with pytest.raises(RuntimeError, match='Unselected python'):
        release.prepare()
    assert not (release.ROOT / 'publish-dist').exists()


def test_dry_run_never_contacts_registries(artifacts, monkeypatch):
    configure_prepare(monkeypatch)
    monkeypatch.setenv('DRY_RUN', 'true')
    monkeypatch.setattr(release, 'registry_state', lambda *args: pytest.fail('dry run must not inspect registry state'))
    release.prepare()


def test_recovery_preserves_original_bytes_and_pending_release_targets(artifacts, monkeypatch):
    configure_prepare(monkeypatch)
    monkeypatch.setenv('GITHUB_SHA', 'new-workflow-commit')
    monkeypatch.setenv('RELEASE_COMMIT', artifacts['commit'])
    monkeypatch.setenv('RESUME_RUN_ID', '123')
    monkeypatch.setenv('REGISTRY_TARGET', 'npm')
    monkeypatch.setattr(release, 'registry_state', lambda registry, manifest: ['woltspace-tui-0.5.2.tgz'] if registry == 'npm' else [])
    release.prepare()
    manifest = release.load_manifest()
    assert manifest['commit'] == artifacts['commit']
    assert manifest['files'] == artifacts['files']
    assert manifest['targets'] == ['npm']
    assert manifest['release_targets'] == manifest['verify_targets'] == ['python', 'npm']
    release.check('npm')
    assert release.digest(release.ROOT / 'publish-dist/woltspace-tui-0.5.2.tgz') == artifacts['files']['woltspace-tui-0.5.2.tgz']
    with pytest.raises(RuntimeError, match='unselected'):
        release.check('python')


@pytest.mark.parametrize('target', ['python', 'npm'])
def test_independent_release_leaves_other_registry_tags_untouched(artifacts, monkeypatch, target):
    artifacts['targets'] = artifacts['release_targets'] = [target]
    (release.ROOT / 'release-manifest.json').write_text(json.dumps(artifacts))
    calls, _ = fake_releases(monkeypatch, artifacts)
    release.finalize()
    assert [call[2] for call in calls if call[:2] == ('release', 'create')] == [
        'v0.5.5' if target == 'python' else 'tui-v0.5.2']


def test_unchanged_python_pin_verifies_published_bytes_instead_of_rebuilt_wheel(artifacts, monkeypatch):
    artifacts['targets'] = ['npm']
    data = pypi_data(artifacts)
    for item in data['urls']:
        item['digests']['sha256'] = 'original-published-digest'
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: data)
    monkeypatch.setattr(release, 'remote_digest', lambda url: 'original-published-digest')
    assert release.registry_state('python', artifacts) == []


def test_unchanged_npm_pin_checks_download_integrity(artifacts, monkeypatch):
    import base64
    artifacts['targets'] = ['python']
    integrity = base64.b64encode(bytes.fromhex('ab' * 64)).decode()
    data = {'name': '@woltspace/tui', 'version': '0.5.2',
            'dist': {'tarball': 'https://files.example/tui.tgz', 'integrity': 'sha512-' + integrity}}
    monkeypatch.setattr(release, 'get_json', lambda *args, **kwargs: data)
    monkeypatch.setattr(release, 'remote_digest', lambda url, algorithm: 'ab' * 64 if algorithm == 'sha512' else 'wrong')
    assert release.registry_state('npm', artifacts) == []
    monkeypatch.setattr(release, 'remote_digest', lambda *args: 'different')
    with pytest.raises(RuntimeError, match='integrity mismatch'):
        release.registry_state('npm', artifacts)


def original_run():
    return {'head_repository': {'full_name': release.REPO}, 'head_branch': 'main',
            'path': '.github/workflows/publish.yml', 'event': 'workflow_dispatch',
            'status': 'completed', 'head_sha': 'a' * 40}


@pytest.mark.parametrize('change', ['repository', 'branch', 'workflow', 'event', 'running', 'sha', 'validation', 'run_id'])
def test_recovery_rejects_untrusted_or_unvalidated_source_run(monkeypatch, change):
    run = original_run()
    jobs = [{'name': name, 'conclusion': 'success'} for name in (
        'guard', 'validation / tests (3.11)', 'validation / tests (3.13)', 'validation / packages', 'prepare')]
    if change == 'repository': run['head_repository']['full_name'] = 'other/repo'
    elif change == 'branch': run['head_branch'] = 'feature'
    elif change == 'workflow': run['path'] = '.github/workflows/other.yml'
    elif change == 'event': run['event'] = 'pull_request'
    elif change == 'running': run['status'] = 'in_progress'
    elif change == 'sha': run['head_sha'] = 'invalid'
    elif change == 'validation': jobs[1]['conclusion'] = 'failure'
    monkeypatch.setenv('RESUME_RUN_ID', '../escape' if change == 'run_id' else '123')
    monkeypatch.setattr(release, 'get_json', lambda url: {'jobs': jobs} if '/jobs?' in url else run)
    with pytest.raises(RuntimeError): release.source_run()


def test_recovery_admits_validated_original_run_across_job_pages(monkeypatch):
    monkeypatch.setenv('RESUME_RUN_ID', '123')
    jobs = [{'name': name, 'conclusion': 'success'} for name in (
        'guard', 'validation / tests (3.11)', 'validation / tests (3.13)', 'validation / packages', 'prepare')]
    def read(url):
        if '/jobs?' not in url: return original_run()
        return {'jobs': [{'name': 'unrelated', 'conclusion': 'success'}] * 100 if url.endswith('page=1') else jobs}
    monkeypatch.setattr(release, 'get_json', read)
    outputs = {}
    monkeypatch.setattr(release, 'output', lambda key, value: outputs.update({key: value}))
    release.source_run()
    assert outputs == {'release_commit': 'a' * 40}


def test_workflow_npm_command_accepts_local_tarball_without_git_lookup(tmp_path):
    """Run the actual workflow command in dry-run mode to catch npm path parsing."""
    import io
    import shutil
    import subprocess
    import tarfile
    if not shutil.which('npm'):
        pytest.skip('npm is required for the publish-command contract')
    workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/publish.yml').read_text()
    command, = [line.strip().removeprefix('run: ') for line in workflow.splitlines()
                if line.strip().startswith('run: npm publish ')]
    dist = tmp_path / 'publish-dist'
    dist.mkdir()
    payload = json.dumps({'name': '@woltspace/tui', 'version': '0.5.2'}).encode()
    with tarfile.open(dist / 'woltspace-tui-0.5.2.tgz', 'w:gz') as archive:
        info = tarfile.TarInfo('package/package.json')
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    # npm's dry run can query package metadata. Keep this contract independent
    # of whether the real version has since been published.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    class Registry(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(404)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"error":"not found"}')
        def log_message(self, *args):
            pass
    with ThreadingHTTPServer(('127.0.0.1', 0), Registry) as registry:
        thread = threading.Thread(target=registry.serve_forever)
        thread.start()
        try:
            arguments = f' --dry-run --json --cache ./npm-cache --registry http://127.0.0.1:{registry.server_port}'
            result = subprocess.run(['bash', '-c', command + arguments], cwd=tmp_path,
                                    capture_output=True, text=True, timeout=30)
        finally:
            registry.shutdown()
            thread.join()

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['id'] == '@woltspace/tui@0.5.2'


def test_existing_untagged_draft_creates_verified_ref_before_publication(artifacts, monkeypatch):
    existing = {'tui-v0.5.2': {'draft': True, 'commit': artifacts['commit'],
                'assets': ['woltspace-tui-0.5.2.tgz'], 'tag_exists': False}}
    calls, releases = fake_releases(monkeypatch, artifacts, existing)
    release.finalize()
    create = next(i for i, call in enumerate(calls) if call[:2] == ('api', f'repos/{release.REPO}/git/refs'))
    verify_asset = next(i for i, call in enumerate(calls) if call[:3] == ('release', 'download', 'tui-v0.5.2'))
    publish = next(i for i, call in enumerate(calls) if call[:3] == ('release', 'edit', 'tui-v0.5.2'))
    assert verify_asset < create < publish
    assert f'sha={artifacts["commit"]}' in calls[create]
    assert not releases['tui-v0.5.2']['draft']
    assert not any(call[:3] == ('release', 'upload', 'tui-v0.5.2') for call in calls)


def test_untagged_draft_with_wrong_target_is_not_published(artifacts, monkeypatch):
    existing = {'tui-v0.5.2': {'draft': True, 'commit': 'wrong-commit',
                'assets': ['woltspace-tui-0.5.2.tgz'], 'tag_exists': False}}
    calls, _ = fake_releases(monkeypatch, artifacts, existing)
    with pytest.raises(RuntimeError, match='untagged draft'):
        release.finalize()
    assert not any(call[:2] == ('release', 'edit') or call[:2] == ('api', f'repos/{release.REPO}/git/refs') for call in calls)


def test_failed_tag_creation_keeps_release_draft(artifacts, monkeypatch):
    calls, releases = fake_releases(monkeypatch, artifacts)
    original = release.gh
    def reject_ref(*args):
        if args[:2] == ('api', f'repos/{release.REPO}/git/refs'):
            raise RuntimeError('ref creation denied')
        return original(*args)
    monkeypatch.setattr(release, 'gh', reject_ref)
    with pytest.raises(RuntimeError, match='ref creation denied'):
        release.finalize()
    assert releases['tui-v0.5.2']['draft']
    assert not any(call[:2] == ('release', 'edit') for call in calls)
