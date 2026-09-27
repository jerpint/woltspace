import argparse
import json
import runpy
import sys
from pathlib import Path

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


def test_app_detail_keeps_configured_port_when_stopped(monkeypatch):
    manifest = apps.WoltspaceApp(name="notes", keeper="n00b", port=4321)
    monkeypatch.setattr(server_app, "get_app", lambda name: manifest)
    monkeypatch.setattr(server_app, "running_apps", lambda: [])
    payload = TestClient(server_app.app, base_url="http://localhost:7777").get("/apps/notes").json()
    assert payload["configured_port"] == 4321
    assert payload["port"] is None


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
