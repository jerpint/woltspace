"""The wolt site shell: page tree, site.json, injection, built-in pages."""

import json
import re
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "container" / "lib"))

import site_shell
import sites
import wolts as wolts_mod

import server.app as server_app


@pytest.fixture(autouse=True)
def tmp_wolts(tmp_path, monkeypatch):
    monkeypatch.setattr(sites, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(wolts_mod, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(server_app, "WOLTS_DIR", tmp_path)
    monkeypatch.delenv("WOLTSPACE_SITE_SHELL", raising=False)

    wolt = tmp_path / "testwolt" / "wolt"
    site = wolt / "site"
    (site / "notes").mkdir(parents=True)
    (site / "index.html").write_text("<html><head><title>Home</title></head><body><h1>home</h1></body></html>")
    (site / "zebra.html").write_text("<title>Zebra &amp; co</title><body>z</body>")
    (site / "apple.html").write_text("<body>no title</body>")
    (site / "notes" / "field.html").write_text("<title>Field notes</title><body>f</body>")
    (site / "style.css").write_text("body{}")
    (site / ".hidden.html").write_text("<title>hidden</title>")
    (site / "_draft.html").write_text("<title>draft</title>")
    (site / "node_modules").mkdir()
    (site / "node_modules" / "junk.html").write_text("<title>junk</title>")

    (wolt / "wolt.json").write_text(json.dumps({
        "name": "testwolt", "type": "beaver", "role": "builds dams",
        "harness": "claude", "model": "sonnet", "secret_token": "DO-NOT-LEAK",
    }))
    mem = wolt / "memory"
    (mem / "archive").mkdir(parents=True)
    (mem / "identity.md").write_text("# Testwolt\nI build dams.")
    (mem / "context.md").write_text("\n".join(f"context line {i}" for i in range(200)))
    (mem / "learnings.md").write_text("\n".join(f"learning {i}" for i in range(100)))
    (mem / "archive" / "conversations.md").write_text("old")
    return tmp_path


@pytest.fixture
def client():
    return TestClient(server_app.app, base_url="http://localhost:7777")


def site_dir(tmp_wolts):
    return tmp_wolts / "testwolt" / "wolt" / "site"


SHELL_TAG = '<script src="/static/wolt-shell/shell.js"></script>'


def manifest_of(html):
    m = re.search(r"window\.__WOLT_SHELL__=(\{.*?\});</script>", html, re.S)
    assert m, "no inline manifest"
    return json.loads(m.group(1))


class TestPageTree:
    def test_index_first_then_pages_then_folders(self, tmp_wolts):
        tree = site_shell.page_tree(site_dir(tmp_wolts))
        assert [i.get("path") for i in tree] == ["index.html", "apple.html", "zebra.html", "notes/"]
        assert tree[-1]["children"] == [{"title": "Field notes", "path": "notes/field.html"}]

    def test_titles_from_title_tag_unescaped_or_filename(self, tmp_wolts):
        titles = {i["path"]: i["title"] for i in site_shell.page_tree(site_dir(tmp_wolts)) if "title" in i}
        assert titles["zebra.html"] == "Zebra & co"
        assert titles["apple.html"] == "apple"

    def test_skips_hidden_underscore_deps_and_slot_files(self, tmp_wolts):
        sdir = site_dir(tmp_wolts)
        (sdir / "header.html").write_text("<div>slot</div>")
        paths = json.dumps(site_shell.page_tree(sdir, {"header_html": "header.html"}))
        for name in (".hidden.html", "_draft.html", "junk.html", "header.html", "style.css"):
            assert name not in paths

    def test_huge_site_is_capped(self, tmp_wolts, monkeypatch):
        monkeypatch.setattr(site_shell, "MAX_PAGES", 2)
        tree = site_shell.page_tree(site_dir(tmp_wolts))
        assert sum(1 for i in tree if "title" in i) == 2


class TestSiteConfig:
    def test_missing_or_broken_site_json_means_defaults(self, tmp_wolts):
        sdir = site_dir(tmp_wolts)
        assert site_shell.load_site_config(sdir) == {}
        (sdir / "site.json").write_text("{not json")
        assert site_shell.load_site_config(sdir) == {}
        (sdir / "site.json").write_text("[1, 2]")
        assert site_shell.load_site_config(sdir) == {}

    def test_page_meta_off(self):
        page = '<head><meta name="wolt-shell" content="off"></head>'
        assert site_shell.shell_wanted({}, page) is False
        assert site_shell.shell_wanted({}, "<head></head>") is True

    def test_site_json_off_and_lodge_kill_switch(self, monkeypatch):
        assert site_shell.shell_wanted({"shell": False}, "") is False
        monkeypatch.setenv("WOLTSPACE_SITE_SHELL", "off")
        assert site_shell.shell_wanted({}, "") is False


class TestInjection:
    def test_site_page_gets_manifest_and_shell_before_body_end(self, client):
        html = client.get("/wolt/testwolt/site/").text
        assert '<script src="/static/wolt-shell/shell.js"></script>' in html
        assert html.index("shell.js") < html.rindex("</body>")
        m = manifest_of(html)
        assert m["wolt"]["name"] == "testwolt" and m["wolt"]["type"] == "beaver"
        assert m["base"] == "/wolt/testwolt/site/" and m["builtin"] == "/wolt/testwolt/_/"
        assert [i.get("path") for i in m["tree"]][0] == "index.html"

    def test_livereload_still_injected(self, client):
        assert "/wolt/testwolt/site/livereload" in client.get("/wolt/testwolt/site/").text

    def test_manifest_carries_the_display_name(self, client, tmp_wolts):
        cfg_path = tmp_wolts / "testwolt" / "wolt" / "wolt.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["display_name"] = "Test Wolt"
        cfg_path.write_text(json.dumps(cfg))
        m = manifest_of(client.get("/wolt/testwolt/site/").text)
        assert m["wolt"]["display_name"] == "Test Wolt"
        assert m["wolt"]["name"] == "testwolt"

    def test_only_public_wolt_fields_reach_the_page(self, client):
        html = client.get("/wolt/testwolt/site/").text
        assert "DO-NOT-LEAK" not in html
        assert "secret_token" not in html

    def test_site_json_look_is_passed_through(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "site.json").write_text(json.dumps({
            "title": "Dam Works", "tokens": {"accent": "#123456"}, "custom_css": "shell.css", "shell": True,
        }))
        m = manifest_of(client.get("/wolt/testwolt/site/").text)
        assert m["site"] == {"title": "Dam Works", "tokens": {"accent": "#123456"}, "custom_css": "shell.css"}

    def test_takeover_by_site_json(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "site.json").write_text('{"shell": false}')
        html = client.get("/wolt/testwolt/site/").text
        assert "__WOLT_SHELL__" not in html and SHELL_TAG not in html
        assert "<h1>home</h1>" in html

    def test_takeover_by_page_meta(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "own.html").write_text(
            '<html><head><meta name="wolt-shell" content="off"></head><body>mine</body></html>')
        assert SHELL_TAG not in client.get("/wolt/testwolt/site/own.html").text

    def test_assets_untouched(self, client):
        assert "shell" not in client.get("/wolt/testwolt/site/style.css").text

    def test_page_text_cannot_break_out_of_the_inline_script(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "evil.html").write_text(
            "<title></script><script>alert(1)</script></title><body>x</body>")
        html = client.get("/wolt/testwolt/site/").text
        inline = re.search(r"<script>window\.__WOLT_SHELL__=.*?</script>", html, re.S).group(0)
        # The first </script> after the manifest is its own closing tag, and the
        # hostile title survives intact as data inside it.
        assert inline.endswith("};</script>")
        titles = [i.get("title") for i in manifest_of(html)["tree"]]
        assert "</script><script>alert(1)</script>" in titles

    def test_no_body_tag_appends(self):
        out = site_shell.inject_shell("<h1>bare</h1>", {"v": 1})
        assert out.startswith("<h1>bare</h1><script>")


class TestBuiltinPages:
    @pytest.mark.parametrize("path", ["/wolt/testwolt/_/", "/wolt/testwolt/_/memory", "/wolt/testwolt/_/settings"])
    def test_pages_render_with_manifest_and_drawer(self, client, path):
        resp = client.get(path)
        assert resp.status_code == 200
        assert manifest_of(resp.text)["wolt"]["name"] == "testwolt"
        assert SHELL_TAG in resp.text

    def test_unknown_tab_and_wolt_404(self, client):
        assert client.get("/wolt/testwolt/_/secrets").status_code == 404
        assert client.get("/wolt/ghost/_/").status_code == 404
        assert client.get("/wolt/ghost/_/memory.json").status_code == 404

    def test_builtin_page_keeps_manifest_but_no_drawer_when_shell_off(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "site.json").write_text('{"shell": false}')
        html = client.get("/wolt/testwolt/_/").text
        assert "__WOLT_SHELL__" in html and SHELL_TAG not in html

    def test_memory_json_is_windowed_like_boot(self, client):
        mem = client.get("/wolt/testwolt/_/memory.json").json()
        assert mem["identity"].startswith("# Testwolt")
        assert mem["context"].splitlines()[-1] == "context line 79"
        assert mem["context_lines"] == 200 and mem["context_window"] == 80
        assert len(mem["learnings"].splitlines()) == 40
        assert mem["archive"] == ["conversations.md"]

    def test_manifest_json_route(self, client):
        m = client.get("/wolt/testwolt/_/manifest.json").json()
        assert m["wolt"]["role"] == "builds dams"
        assert "secret_token" not in json.dumps(m)

    def test_site_without_home_lands_on_about(self, client, tmp_wolts):
        (site_dir(tmp_wolts) / "index.html").unlink()
        resp = client.get("/wolt/testwolt/site/", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/wolt/testwolt/_/"

    def test_static_shell_assets_are_served(self, client):
        for name in ("shell.js", "shell.css"):
            assert client.get(f"/static/wolt-shell/{name}").status_code == 200


class TestLightweight:
    def test_titles_are_cached_until_the_page_changes(self, tmp_wolts, monkeypatch):
        sdir = site_dir(tmp_wolts)
        page = sdir / "zebra.html"
        assert site_shell._page_title(page) == "Zebra & co"
        reads = []
        real_open = Path.open
        monkeypatch.setattr(Path, "open", lambda self, *a, **k: (reads.append(self), real_open(self, *a, **k))[1])
        assert site_shell._page_title(page) == "Zebra & co"
        assert reads == [], "an unchanged page must not be re-read"
        page.write_text("<title>Zebra renamed, longer</title>")
        assert site_shell._page_title(page) == "Zebra renamed, longer"

    def test_no_third_party_fonts_by_default(self, client):
        for path in ("/static/wolt-shell/shell.js", "/wolt/testwolt/_/"):
            text = client.get(path).text
            assert "https://fonts.googleapis.com" not in text
        assert "fonts_href" in client.get("/static/wolt-shell/shell.js").text
