import json
import sys
from pathlib import Path

from starlette.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

from server import app as server_app  # noqa: E402


def _wolt(root: Path, name: str = "n00b") -> None:
    home = root / name / "wolt"
    (home / "site").mkdir(parents=True)
    (home / "memory").mkdir()
    (home / "wolt.json").write_text(json.dumps({
        "name": name, "type": "raccoon", "description": "Builds useful things.",
    }))
    (home / "memory" / "identity.md").write_text(f"# {name}\n\nA raccoon.\n")


def test_wolt_page_is_bookmarkable_and_unknown_wolt_is_404(tmp_path, monkeypatch):
    _wolt(tmp_path)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    client = TestClient(server_app.app, base_url="http://localhost:7777")

    page = client.get("/w/n00b")

    assert page.status_code == 200
    assert 'data-wolt-name="n00b"' in page.text
    assert "/static/wolt-page.js" in page.text
    assert client.get("/w/missing").status_code == 404
    assert client.get("/connectors").status_code == 200


def test_sidebar_has_product_nav_and_quiet_footer():
    sidebar = (ROOT / "templates" / "partials" / "sidebar.html").read_text()

    for label in ("Home", "Apps", "Wolves", "Connectors"):
        assert f">{label}<" in sidebar
    assert "Sessions</span>" not in sidebar
    assert "System Creatures" not in sidebar
    assert "terminal" in sidebar and "⚙ settings" in sidebar


def test_wolt_page_has_render_level_xss_acceptance_check():
    browser_check = ROOT / "test" / "browser" / "lodge_wolt_page.cjs"
    assert browser_check.exists()
    assert "locator('.wolt-session-list img').count()" in browser_check.read_text()


def test_wolt_page_uses_provable_session_states():
    source = (ROOT / "public" / "static" / "wolt-page.js").read_text()
    assert "const sessionState = sessionStateText" in source
    assert "waiting on you" not in source.lower()
