"""push-view: what a wolt types becomes a page the viewport can show.

A brand new wolt often runs `push-view page.html`; that used to reach the
viewport as is and the human saw "not found". These tests run the real script
against a tiny fake lodge.
"""

import json
import os
import subprocess
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "container" / "bin" / "push-view"
PAGES = {
    "/wolt/sprout/site/hello.html", "/wolt/sprout/site/", "/wolt/other/site/a.html",
    "/wolt/sprout/site/my%20page.html",
}
MOVED = {"/wolt/sprout/site/plans"}
SLOW = {"/wolt/sprout/site/slow.html"}


@pytest.fixture
def lodge():
    pushed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path in SLOW:
                time.sleep(10)
            if self.path in MOVED:
                self.send_response(308)
                self.send_header("Location", self.path + "/")
            else:
                self.send_response(200 if self.path in PAGES else 404)
            self.end_headers()

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            pushed.append((self.path, json.loads(body)["url"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", pushed
    server.shutdown()


def run(lodge, *args, cwd=None, wolt="sprout"):
    api, _ = lodge
    env = {
        "PATH": os.environ["PATH"],
        "WOLTSPACE_API": api,
        "WOLTSPACE_WOLT_SESSION": "sprout-test-1",
    }
    if wolt:
        env["WOLTSPACE_WOLT_NAME"] = wolt
    return subprocess.run(
        ["bash", str(SCRIPT), *args], env=env, cwd=cwd, capture_output=True, text=True,
    )


@pytest.mark.parametrize("given", [
    "hello.html", "./hello.html", "site/hello.html", "wolt/site/hello.html",
    "/wolt/sprout/site/hello.html",
])
def test_own_site_page_in_any_spelling(lodge, given, tmp_path):
    result = run(lodge, given, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert lodge[1] == [("/current?session=sprout-test-1", "/wolt/sprout/site/hello.html")]


def test_file_path_on_disk_becomes_its_site_url(lodge, tmp_path):
    page = tmp_path / "other" / "wolt" / "site" / "a.html"
    page.parent.mkdir(parents=True)
    page.write_text("hi")
    result = run(lodge, str(page))
    assert result.returncode == 0, result.stderr
    assert lodge[1][0][1] == "/wolt/other/site/a.html"


def test_missing_page_is_not_pushed(lodge, tmp_path):
    result = run(lodge, "nope.html", cwd=tmp_path)
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert lodge[1] == []


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_pushes_nothing(lodge, flag):
    result = run(lodge, flag)
    assert result.returncode == 0
    assert "usage: push-view" in result.stdout
    assert lodge[1] == []


def test_unknown_option_and_no_argument(lodge):
    assert run(lodge, "--url").returncode == 2
    assert run(lodge).returncode == 2
    assert lodge[1] == []


def test_relative_name_without_wolt_name_is_refused(lodge, tmp_path):
    result = run(lodge, "hello.html", cwd=tmp_path, wolt="")
    assert result.returncode == 2
    assert "/wolt/<name>/site/hello.html" in result.stderr
    assert lodge[1] == []


def test_app_url_is_pushed_without_a_check(lodge):
    result = run(lodge, "http://blog.localhost:7777/")
    assert result.returncode == 0, result.stderr
    assert lodge[1][0][1] == "http://blog.localhost:7777/"


def test_quotes_in_url_stay_valid_json(lodge):
    PAGES.add('/wolt/sprout/site/a"b.html')
    try:
        result = run(lodge, '/wolt/sprout/site/a"b.html')
    finally:
        PAGES.discard('/wolt/sprout/site/a"b.html')
    assert result.returncode == 0, result.stderr
    assert lodge[1][0][1] == '/wolt/sprout/site/a"b.html'


def test_space_in_name_is_checked_encoded_and_pushed_as_typed(lodge):
    result = run(lodge, "/wolt/sprout/site/my page.html")
    assert result.returncode == 0, result.stderr
    assert lodge[1][0][1] == "/wolt/sprout/site/my page.html"


def test_redirect_counts_as_found(lodge):
    result = run(lodge, "/wolt/sprout/site/plans")
    assert result.returncode == 0, result.stderr
    assert lodge[1][0][1] == "/wolt/sprout/site/plans"


def test_hung_lodge_times_out(lodge):
    started = time.monotonic()
    result = run(lodge, "/wolt/sprout/site/slow.html")
    assert time.monotonic() - started < 9
    assert result.returncode == 1
    assert "failed to reach" in result.stderr
    assert lodge[1] == []


def test_lodge_down_says_unreachable(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    result = run((f"http://127.0.0.1:{port}", []), "/wolt/sprout/site/hello.html", cwd=tmp_path)
    assert result.returncode == 1
    assert "failed to reach" in result.stderr
