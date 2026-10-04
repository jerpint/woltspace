from pathlib import Path

from starlette.testclient import TestClient

from server import app as server_app


ROOT = Path(__file__).resolve().parent.parent


def test_apps_api_exposes_invalid_manifest_reason(monkeypatch):
    monkeypatch.setattr(
        server_app,
        "discover_apps_with_errors",
        lambda: ([], [{"name": "broken", "error": "keeper: Field required"}]),
    )
    monkeypatch.setattr(server_app, "running_apps", lambda: [])

    response = TestClient(server_app.app, base_url="http://localhost:7777").get("/apps")

    assert response.status_code == 200
    assert response.json() == [{
        "name": "broken",
        "invalid": True,
        "error": "keeper: Field required",
        "running": False,
        "port": None,
        "url": None,
    }]


def test_lodge_renders_invalid_app_with_dom_text_only():
    source = (ROOT / "public" / "static" / "lodge.js").read_text()
    branch = source.split("if (p.invalid) {", 1)[1].split("return card;", 1)[0]
    assert "Invalid woltspace.json" in branch
    assert "p.error" in branch
    assert "lodgeElement(" in branch
    assert "innerHTML" not in branch
