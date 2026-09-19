"""Verify saved onboarding choice reaches native first-wolt creation.

Uses real first-run selection/config/scaffolding and a mocked agent start; no
login or model call.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'container' / 'lib'))

import harness_auth  # noqa: E402,F401
import harnesses  # noqa: E402,F401
from harnesses import get_default_harness, platform_skill_invoke  # noqa: E402
import server.app as server_app  # noqa: E402
from server import state  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402


def test_selected_codex_drives_native_create_route(tmp_path, monkeypatch):
    import sessions
    import wolts

    root = tmp_path / 'wolts'
    root.mkdir()
    source = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('WOLTSPACE_WOLTS_DIR', str(root))
    monkeypatch.setenv('WOLTSPACE_ISOLATION', 'host')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', '/dev/null')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setattr(server_app, 'WOLTS_DIR', root)
    monkeypatch.setattr(state, 'WOLTS_DIR', root)
    monkeypatch.setattr(sessions, 'WOLTS_DIR', root)
    monkeypatch.setattr(wolts, 'WOLTS_DIR', root)
    monkeypatch.setattr(wolts, 'CONFIG_FILE', root / 'woltspace.json')
    monkeypatch.setattr(wolts, 'WOLTSPACE_DIR', source)
    calls = []

    def start(**kwargs):
        calls.append((sessions.wolt_harness(kwargs['wolt']), kwargs))
        return {'name': 'fixture-session', 'harness': calls[-1][0]}

    monkeypatch.setattr(server_app, 'start_session', start)
    monkeypatch.setattr(server_app, 'harness_installed', lambda name: True)
    client = TestClient(server_app.app)
    assert get_default_harness() == 'claude'
    assert client.post('/onboarding/harness', json={'harness': 'codex'}).status_code == 200
    response = client.post('/sessions/new/create', json={'name': 'fresh', 'type': 'raccoon'})
    assert response.status_code == 200, response.text
    wolt_config = json.loads((root / 'fresh/wolt/wolt.json').read_text())
    assert wolt_config['origin'] == 'user'
    assert json.loads((root / 'woltspace.json').read_text())['onboarding']['harness_selected'] is True
    assert calls[0][0] == 'codex'
    assert calls[0][1]['prompt'] == platform_skill_invoke('codex', 'create-wolt', delivery='copy')
    assert not (root / 'fresh/.codex/auth.json').exists()
