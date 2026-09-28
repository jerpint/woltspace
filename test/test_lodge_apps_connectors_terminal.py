import argparse
import asyncio
import json
import runpy
import sys
import time
from pathlib import Path
from unittest.mock import Mock

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import apps  # noqa: E402
from server import app as server_app  # noqa: E402


def _manifest(root: Path, name="notes", keeper="n00b"):
    target = root / "apps" / name
    target.mkdir(parents=True)
    (target / "woltspace.json").write_text(json.dumps({
        "name": name, "keeper": keeper, "port": 4321,
        "start": "echo hello", "stack": "html",
    }))
    return target


def test_tunnel_status_has_public_shape(monkeypatch):
    monkeypatch.setattr(server_app.tunnel_mgr, "get_tunnel_url", lambda: "https://lodge.example")
    monkeypatch.setattr(server_app.tunnel_mgr, "get_tunnel_mode", lambda: "named")
    response = TestClient(server_app.app, base_url="http://localhost:7777").get("/tunnel")
    assert response.json() == {"mode": "named", "url": "https://lodge.example"}


def test_keeper_update_is_atomic_and_updates_running_record(tmp_path, monkeypatch):
    _manifest(tmp_path)
    state = tmp_path / ".space" / "apps"
    state.mkdir(parents=True)
    (state / "notes.json").write_text(json.dumps({"name": "notes", "keeper": "n00b", "pid": 1}))
    for attr, value in (("WOLTS_DIR", tmp_path), ("APPS_DIR", tmp_path / "apps"),
                        ("LEGACY_PROJECTS_DIR", tmp_path / "projects"), ("_RUNNING_STATE_DIR", state)):
        monkeypatch.setattr(apps, attr, value)
    updated = apps.set_app_keeper("notes", "pixie")
    assert updated.keeper == "pixie"
    assert json.loads((tmp_path / "apps" / "notes" / "woltspace.json").read_text())["keeper"] == "pixie"
    assert json.loads((state / "notes.json").read_text())["keeper"] == "pixie"
    assert not (tmp_path / "apps" / "notes" / "woltspace.json.tmp").exists()


def test_logs_are_bounded_and_unknown_app_is_404(tmp_path, monkeypatch):
    _manifest(tmp_path)
    log = tmp_path / "notes.log"
    log.write_text("one\ntwo\nthree\n")
    monkeypatch.setattr(server_app, "get_app", lambda name: object() if name == "notes" else None)
    monkeypatch.setattr(server_app, "app_log_file", lambda name: log)
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    assert client.get("/apps/notes/logs?tail=2").json()["lines"] == ["two", "three"]
    assert client.get("/apps/missing/logs").status_code == 404


def test_logs_read_only_the_last_64k_of_a_multi_megabyte_file(tmp_path, monkeypatch):
    log = tmp_path / "notes.log"
    with log.open("wb") as handle:
        handle.write(b"old-secret-line\n" + b"x" * (3 * 1024 * 1024) + b"\nlast-line\n")
    monkeypatch.setattr(server_app, "get_app", lambda name: object())
    monkeypatch.setattr(server_app, "app_log_file", lambda name: log)
    payload = TestClient(server_app.app, base_url="http://localhost:7777").get(
        "/apps/notes/logs?tail=2000"
    ).json()
    rendered = "\n".join(payload["lines"])
    assert "old-secret-line" not in rendered
    assert rendered.endswith("last-line")
    assert len(rendered.encode()) <= 65536


def test_app_routes_reject_bad_names_before_filesystem_helpers(monkeypatch):
    get = Mock()
    monkeypatch.setattr(server_app, "get_app", get)
    response = TestClient(server_app.app, base_url="http://localhost:7777").get("/apps/bad.name")
    assert response.status_code == 400
    assert response.json() == {"error": "invalid app name"}
    get.assert_not_called()


def test_keeper_update_failure_paths_are_400(monkeypatch):
    monkeypatch.setattr(server_app, "_configured_wolts", lambda: [{"dir": "n00b"}])
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    assert client.put("/apps/notes", content="not-json", headers={"content-type": "application/json"}).status_code == 400
    response = client.put("/apps/notes", json=[])
    assert response.status_code == 400
    assert response.json() == {"error": "JSON object body required"}
    response = client.put("/apps/notes", json={"keeper": "missing"})
    assert response.status_code == 400
    assert response.json() == {"error": "keeper must name an existing wolt"}


def test_restart_waits_for_old_pid_and_verifies_new_one(monkeypatch):
    monkeypatch.setattr(apps, "get_app", lambda name: object())
    monkeypatch.setattr(apps, "_read_state", lambda name: {"pid": 7})
    stop = Mock(return_value=True)
    start = Mock(return_value={"pid": 8, "name": "notes"})
    monkeypatch.setattr(apps, "stop_app", stop)
    monkeypatch.setattr(apps, "start_app", start)
    monkeypatch.setattr(apps, "_is_pid_alive", Mock(side_effect=[True, False, False, True]))
    monkeypatch.setattr(apps.time, "sleep", lambda _seconds: None)
    assert apps.restart_app("notes")["pid"] == 8
    stop.assert_called_once_with("notes")
    start.assert_called_once_with("notes")


def test_restart_refuses_to_spawn_while_old_pid_lives(monkeypatch):
    monkeypatch.setattr(apps, "get_app", lambda name: object())
    monkeypatch.setattr(apps, "_read_state", lambda name: {"pid": 7})
    monkeypatch.setattr(apps, "stop_app", Mock(return_value=True))
    start = Mock()
    monkeypatch.setattr(apps, "start_app", start)
    monkeypatch.setattr(apps, "_is_pid_alive", lambda pid: True)
    ticks = iter([0, 6])
    monkeypatch.setattr(apps.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(apps.time, "sleep", lambda _seconds: None)
    try:
        apps.restart_app("notes")
    except RuntimeError as exc:
        assert "still stopping" in str(exc)
    else:
        raise AssertionError("restart spawned over a live old pid")
    start.assert_not_called()


def test_slow_restart_does_not_block_the_event_loop(monkeypatch):
    order = []

    def slow_restart(name):
        order.append("restart-started")
        time.sleep(0.12)
        order.append("restart-finished")
        return {"name": name, "pid": 8}

    monkeypatch.setattr(server_app, "restart_app", slow_restart)

    async def exercise():
        async def ticker():
            await asyncio.sleep(0.01)
            order.append("loop-responsive")

        result, _ = await asyncio.gather(server_app.app_restart("notes"), ticker())
        return result

    assert asyncio.run(exercise()) == {"name": "notes", "pid": 8}
    assert order == ["restart-started", "loop-responsive", "restart-finished"]


def test_start_and_stop_routes_offload_lifecycle_calls():
    source = (ROOT / "server" / "app.py").read_text()
    assert "await asyncio.to_thread(start_app, name)" in source
    assert "await asyncio.to_thread(stop_app, name)" in source


def test_app_detail_keeps_configured_port_when_stopped(monkeypatch):
    manifest = apps.WoltspaceApp(name="notes", keeper="n00b", port=4321)
    monkeypatch.setattr(server_app, "get_app", lambda name: manifest)
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    payload = TestClient(server_app.app, base_url="http://localhost:7777").get("/apps/notes").json()
    assert payload["configured_port"] == 4321
    assert payload["port"] is None


def test_app_detail_reports_dedicated_address(monkeypatch):
    manifest = apps.WoltspaceApp(name="notes", keeper="n00b", port=4321)
    monkeypatch.setattr(server_app, "get_app", lambda name: manifest)
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    monkeypatch.setattr(server_app, "get_apps_domain", lambda: "owner.woltspace.app")

    payload = TestClient(server_app.app, base_url="http://localhost:7777").get("/apps/notes").json()

    assert payload["own_url"] == "https://notes.owner.woltspace.app"


def test_app_cli_twins_only_call_api(capsys):
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    calls = []
    client["cmd_app_action"].__globals__["_req"] = lambda method, path, body=None: (calls.append((method, path, body)) or (200, {"ok": True}))
    client["cmd_app_action"](argparse.Namespace(name="notes", verb="restart", json=False))
    assert calls == [("POST", "/apps/notes/restart", None)]
    assert "restart ok" in capsys.readouterr().out


def test_ui_keeps_sharing_read_only_and_guards_terminal_resize():
    app_js = (ROOT / "public" / "static" / "app-page.js").read_text()
    terminal_js = (ROOT / "public" / "static" / "terminal.js").read_text()
    assert "🔒 Just me" in app_js
    assert "/share" not in app_js and "/unshare" not in app_js
    assert "offsetWidth<50" in terminal_js
    assert "term.cols>=20&&term.rows>=5" in terminal_js
    assert (ROOT / "public" / "static" / "SymbolsNerdFontMono-Regular.woff2").stat().st_size > 1000
