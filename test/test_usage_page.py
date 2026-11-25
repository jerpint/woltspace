"""The usage page: served lodge-private at /usage, fed only by fresh counts, linked from the lodge nav."""

import re
import sys
from pathlib import Path

from fastapi import FastAPI
from starlette.testclient import TestClient

import usage_support  # noqa: F401  (puts container/lib on the path)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import usage_routes  # noqa: E402

PAGE = (ROOT / "public/usage.html").read_text()


def test_the_page_loads_numbers_only_from_a_fresh_count():
    assert "dashboard.json" not in PAGE
    assert re.findall(r"fetch\('([^']+)'", PAGE) == ["/api/usage/data"]
    assert '<main hidden id="dashboard">' in PAGE
    assert '<script src="/static/usage-recount.js"></script>' in PAGE
    assert (ROOT / "public/static/usage-recount.js").is_file()


def test_the_page_follows_the_lodge_zone_not_a_fixed_one():
    assert "America/" not in PAGE and "setZone(data.timezone)" in PAGE


def test_the_page_has_one_element_per_id():
    ids = re.findall(r'\sid="([^"]+)"', PAGE)
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})


def test_usage_is_served_without_caching_and_linked_from_the_nav(monkeypatch):
    app = FastAPI()
    app.include_router(usage_routes.router)
    page = TestClient(app, base_url="http://localhost:7777").get("/usage")
    assert page.status_code == 200 and page.text == PAGE
    assert page.headers["cache-control"] == "no-store"
    sidebar = (ROOT / "templates/partials/sidebar.html").read_text()
    assert 'id="nav-usage" href="/usage"' in sidebar


def test_a_shared_app_address_never_serves_the_page(monkeypatch):
    import server.app as server_app
    monkeypatch.setattr("apps.running_apps", lambda: [])
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    response = client.get("/usage", headers={"host": "pal.localhost:7777"})
    assert response.status_code == 503 and "Lodge usage" not in response.text
