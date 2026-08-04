"""Tests for wolt sites — direct serving (container/lib/sites.py + /wolt/{name}/site route)."""

import json
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

# Add container/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import sites
import wolts as wolts_mod

import server.app as server_app


@pytest.fixture(autouse=True)
def tmp_wolts(tmp_path, monkeypatch):
    """Set up a temporary wolts directory structure."""
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(wolts_mod, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)

    # Create a wolt with a site dir
    site = tmp_path / "testwolt" / "wolt" / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<html><body><h1>test</h1></body></html>")
    (site / "style.css").write_text("body { color: green }")
    (site / "sub").mkdir()
    (site / "sub" / "page.html").write_text("<h1>sub page</h1>")

    # Something secret OUTSIDE the site dir that must never be served
    (tmp_path / "testwolt" / "wolt" / "memory").mkdir(parents=True)
    (tmp_path / "testwolt" / "wolt" / "memory" / "identity.md").write_text("SECRET")

    return tmp_path


@pytest.fixture
def client():
    return TestClient(server_app.app)


class TestSiteDir:
    def test_site_dir_path(self, tmp_wolts):
        assert sites.site_dir("testwolt") == tmp_wolts / "testwolt" / "wolt" / "site"

    def test_site_dir_nonexistent_wolt(self, tmp_wolts):
        assert not sites.site_dir("nonexistent").exists()


class TestEnsureSite:
    def test_existing_site_untouched(self, tmp_wolts):
        sdir = sites.ensure_site("testwolt")
        assert (sdir / "index.html").read_text() == "<html><body><h1>test</h1></body></html>"

    def test_scaffolds_missing_site(self, tmp_wolts):
        wolt = tmp_wolts / "newwolt" / "wolt"
        wolt.mkdir(parents=True)
        (wolt / "wolt.json").write_text(json.dumps({"name": "newwolt", "type": "raccoon"}))
        sdir = sites.ensure_site("newwolt")
        assert (sdir / "index.html").exists()
        assert "newwolt" in (sdir / "index.html").read_text()


class TestServeSite:
    def test_serves_index(self, client):
        resp = client.get("/wolt/testwolt/site/")
        assert resp.status_code == 200
        assert "<h1>test</h1>" in resp.text

    def test_injects_livereload_script(self, client):
        resp = client.get("/wolt/testwolt/site/")
        assert "/wolt/testwolt/site/livereload" in resp.text
        # Injected before </body>, not appended after </html>
        assert resp.text.index("livereload") < resp.text.index("</body>")

    def test_serves_nested_page(self, client):
        resp = client.get("/wolt/testwolt/site/sub/page.html")
        assert resp.status_code == 200
        assert "sub page" in resp.text

    def test_serves_asset_without_injection(self, client):
        resp = client.get("/wolt/testwolt/site/style.css")
        assert resp.status_code == 200
        assert "text/css" in resp.headers["content-type"]
        assert "livereload" not in resp.text

    def test_missing_file_404(self, client):
        assert client.get("/wolt/testwolt/site/nope.html").status_code == 404

    def test_unknown_wolt_404(self, client):
        assert client.get("/wolt/ghost/site/").status_code == 404

    def test_scaffolds_on_first_request(self, client, tmp_wolts):
        wolt = tmp_wolts / "freshwolt" / "wolt"
        wolt.mkdir(parents=True)
        (wolt / "wolt.json").write_text(json.dumps({"name": "freshwolt", "type": "otter"}))
        resp = client.get("/wolt/freshwolt/site/")
        assert resp.status_code == 200
        assert "freshwolt" in resp.text


class TestPathTraversal:
    def test_dotdot_is_blocked(self, client):
        resp = client.get("/wolt/testwolt/site/%2e%2e/memory/identity.md")
        assert resp.status_code == 404
        assert "SECRET" not in resp.text

    def test_encoded_traversal_to_root_blocked(self, client):
        resp = client.get("/wolt/testwolt/site/%2e%2e/%2e%2e/%2e%2e/%2e%2e/etc/passwd")
        assert resp.status_code == 404

    def test_symlink_escape_blocked(self, client, tmp_wolts):
        site = tmp_wolts / "testwolt" / "wolt" / "site"
        (site / "sneaky.md").symlink_to(tmp_wolts / "testwolt" / "wolt" / "memory" / "identity.md")
        resp = client.get("/wolt/testwolt/site/sneaky.md")
        assert resp.status_code == 404
        assert "SECRET" not in resp.text


class TestSitesApi:
    def test_lists_wolts_with_sites(self, client):
        resp = client.get("/sites")
        assert resp.status_code == 200
        assert {"wolt": "testwolt", "url": "/wolt/testwolt/site/"} in resp.json()

    def test_site_detail(self, client):
        resp = client.get("/sites/testwolt")
        assert resp.json()["dir_exists"] is True
        assert resp.json()["url"] == "/wolt/testwolt/site/"
