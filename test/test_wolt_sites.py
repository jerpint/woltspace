"""Tests for wolt sites: the site folder (container/lib/sites.py) and the old
address, which redirects to the site's own origin (served by the Mill;
serving and traversal are tested in test_mill.py)."""

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
    return TestClient(server_app.app, base_url="http://localhost:7777")


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


class TestOldAddress:
    @pytest.mark.parametrize("path,target", [
        ("/wolt/testwolt/site/", "/sites/open?wolt=testwolt&path=/"),
        ("/wolt/testwolt/site/sub/page.html", "/sites/open?wolt=testwolt&path=/sub/page.html"),
        ("/wolt/TestWolt/site/style.css", "/sites/open?wolt=testwolt&path=/style.css"),
    ])
    def test_redirects_to_the_sites_own_origin(self, client, path, target):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"] == target
        assert "<" not in resp.text  # serves nothing of the site itself

    def test_read_does_not_scaffold_a_missing_site(self, client, tmp_wolts):
        client.get("/wolt/freshwolt/site/", follow_redirects=False)
        assert not (tmp_wolts / "freshwolt" / "wolt" / "site").exists()


class TestSitesApi:
    def test_lists_wolts_with_sites(self, client):
        resp = client.get("/sites")
        assert resp.status_code == 200
        assert {"wolt": "testwolt", "url": "/sites/open?wolt=testwolt&path=/"} in resp.json()

    def test_site_detail(self, client):
        resp = client.get("/sites/testwolt")
        assert resp.json()["dir_exists"] is True
        assert resp.json()["url"] == "/sites/open?wolt=testwolt&path=/"
