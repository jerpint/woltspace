"""API contract for native targets, capabilities, and Auto consent."""

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from server import app as app_module


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.request(method, path, **kwargs)


def _layout(tmp_path, monkeypatch):
    import paths
    import sessions

    wolts = tmp_path / "wolts"
    home = wolts / "maple"
    (home / "wolt").mkdir(parents=True)
    (home / "wolt" / "wolt.json").write_text(
        json.dumps({"name": "maple", "type": "raccoon"})
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(app_module, "WOLTS_DIR", wolts)
    monkeypatch.setattr(sessions, "WOLTS_DIR", wolts)
    monkeypatch.setattr(paths, "WOLTS_DIR", wolts)
    return wolts, home, repo


def test_runtime_capabilities_distinguish_host_from_external(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    response = asyncio.run(_request("GET", "/runtime/capabilities"))
    assert response.status_code == 200
    assert response.json() == {
        "isolation": "host",
        "supports_host_workdirs": True,
        "default_execution_policy": "prompt",
        "policy_version": 1,
    }


def test_health_identifies_the_exact_control_plane(monkeypatch):
    monkeypatch.setenv("WOLTSPACE_INSTANCE_ID", "instance-abc")
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    response = asyncio.run(_request("GET", "/health"))
    assert response.status_code == 200
    assert response.json()["instance_id"] == "instance-abc"
    assert response.json()["isolation"] == "host"


def test_wolt_list_exposes_absolute_home(tmp_path, monkeypatch):
    _, home, _ = _layout(tmp_path, monkeypatch)
    response = asyncio.run(_request("GET", "/wolts"))
    assert response.json()[0]["home"] == str(home.resolve())


def test_auto_grant_requires_exact_canonical_confirmation(tmp_path, monkeypatch):
    _, _, repo = _layout(tmp_path, monkeypatch)
    rejected = asyncio.run(_request("POST", "/auto-grants/grant", json={
        "wolt_id": "maple", "workdir": str(repo), "confirm": "yes",
    }))
    assert rejected.status_code == 400
    assert rejected.json()["canonical_workdir"] == str(repo.resolve())

    granted = asyncio.run(_request("POST", "/auto-grants/grant", json={
        "wolt_id": "maple", "workdir": str(repo),
        "confirm": str(repo.resolve()),
    }))
    assert granted.status_code == 200

    checked = asyncio.run(_request("POST", "/auto-grants/check", json={
        "wolt_id": "maple", "workdir": str(repo),
    }))
    assert checked.json()["approved"] is True

    revoked = asyncio.run(_request("POST", "/auto-grants/revoke", json={
        "wolt_id": "maple", "workdir": str(repo),
    }))
    assert revoked.json()["revoked"] is True


def test_lodge_spawn_passes_explicit_target_and_policy(tmp_path, monkeypatch):
    _, _, repo = _layout(tmp_path, monkeypatch)
    seen = {}

    def fake_start(**kwargs):
        seen.update(kwargs)
        return {"name": "maple-session", "wolt": "maple"}

    monkeypatch.setattr(app_module, "start_session", fake_start)
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json={
        "wolt": "maple",
        "workdir": str(repo),
        "execution_policy": "prompt",
    }))
    assert response.status_code == 200
    assert seen["workdir"] == str(repo)
    assert seen["execution_policy"] == "prompt"


def test_lodge_reports_unapproved_auto_as_forbidden(monkeypatch):
    def denied(**kwargs):
        raise PermissionError("exact grant required")

    monkeypatch.setattr(app_module, "start_session", denied)
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json={
        "wolt": "maple", "execution_policy": "auto",
    }))
    assert response.status_code == 403
    assert "exact grant" in response.json()["error"]


def test_create_wolt_passes_confirmed_target_and_policy(tmp_path, monkeypatch):
    import wolts

    _, _, repo = _layout(tmp_path, monkeypatch)
    seen = {}
    monkeypatch.setattr(
        wolts, "create_creature_wolt",
        lambda name, kind, **kwargs: seen.update({
            "scaffold_harness": kwargs.get("harness"),
        }),
    )

    def fake_start(**kwargs):
        seen.update(kwargs)
        return {"name": "newmaple-session", "wolt": "newmaple"}

    monkeypatch.setattr(app_module, "start_session", fake_start)
    response = asyncio.run(_request("POST", "/sessions/new/create", json={
        "name": "newmaple",
        "type": "raccoon",
        "workdir": str(repo),
        "execution_policy": "prompt",
    }))
    assert response.status_code == 200
    assert seen["wolt"] == "newmaple"
    assert seen["workdir"] == str(repo)
    assert seen["execution_policy"] == "prompt"
    assert seen["scaffold_harness"] == seen["harness"]


@pytest.mark.skipif(not shutil.which("node"), reason="TUI requires Node")
@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("approved, override, expected", [
    (False, None, "prompt"),
    (True, None, "auto"),
    (True, "prompt", "prompt"),
    (False, "auto", None),
    (True, "auto", "auto"),
])
def test_tui_payload_through_api_resolves_grants_and_harness_command(
    tmp_path, monkeypatch, fake_runtime, harness, approved, override, expected,
):
    """Run the TUI request builder, real API and session policy resolution.

    Only the process runtime is faked: no agent or live lodge is started.
    """
    import sessions
    from execution_policy import AutoGrantStore
    from session_targets import SessionTarget

    wolts, home, _ = _layout(tmp_path, monkeypatch)
    (home / "wolt" / "wolt.json").write_text(json.dumps({
        "name": "maple", "type": "raccoon", "harness": harness,
        "model": "sonnet" if harness == "claude" else "gpt-5.6-sol",
    }))
    monkeypatch.setenv("WOLTSPACE_ISOLATION", "host")
    if approved:
        AutoGrantStore(wolts).grant(SessionTarget.resolve("maple", home, wolts_dir=wolts))

    # Capture exactly what the JS API sends from a default TUI spawn target.
    script = """
        import { spawnTarget } from './src/session-view.js';
        import { spawnSession } from './src/api.js';
        globalThis.fetch = async (_url, options) => {
            console.log(options.body);
            return { ok: true, json: async () => ({}) };
        };
        const target = spawnTarget({
            supports_host_workdirs: true, default_execution_policy: 'prompt',
        }, { home: process.argv[1] }, '/unrelated-launch-dir');
        await spawnSession('maple', target.workdir,
            process.argv[2] || target.executionPolicy);
    """
    result = subprocess.run([
        "node", "--input-type=module", "-e", script, str(home), override or "",
    ], cwd=Path(__file__).resolve().parents[1] / "tui",
        capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    if override is None:
        assert "execution_policy" not in payload
    response = asyncio.run(_request("POST", "/sessions/new/lodge", json=payload))
    if expected is None:
        assert response.status_code == 403
        return
    assert response.status_code == 200, response.text
    session = response.json()
    assert session["execution_policy"]["mode"] == expected
    assert session["target"]["canonical_workdir"] == str(home.resolve())
    command = sessions.prepare_session_command(session["name"], "spawn")
    auto_flag = ("--dangerously-skip-permissions" if harness == "claude"
                 else "--dangerously-bypass-approvals-and-sandbox")
    assert (auto_flag in command) == (expected == "auto")
