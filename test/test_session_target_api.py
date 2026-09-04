"""API contract for native targets, capabilities, and Auto consent."""

import asyncio
import json

import httpx

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
