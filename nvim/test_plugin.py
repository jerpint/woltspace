import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
def test_nvim_plugin():
    nvim = shutil.which("nvim")
    if not nvim: pytest.skip("Neovim 0.11+ is required for the editor smoke")
    calls = []
    class API(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self): self.handle_api()
        def do_POST(self): self.handle_api()
        def handle_api(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            calls.append((self.path, self.headers.get("X-Woltspace-Key"), body))
            status, payload = 200, {}
            if self.path.startswith("/deny") or (not self.path.startswith("/public") and self.headers.get("X-Woltspace-Key") != "dummy-secret"):
                status, payload = 401, {"error": "dummy-secret must not be displayed"}
            elif self.path.startswith("/redirect"):
                self.send_response(302)
                self.send_header("Location", "/trap")
                self.end_headers()
                return
            elif self.path.endswith("/wolts"): payload = [{"name": "alpha"}]
            elif self.path.endswith("/sessions"):
                payload = [{"name": "live", "wolt": "alpha", "agent_alive": True},
                           {"name": "rest", "wolt": "alpha", "status": "resting"}]
            elif self.path.endswith("/sessions/new/lodge"): payload = {"name": "fresh"}
            if self.path.startswith("/drop") and self.command == "POST":
                self.close_connection = True
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), API)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        env = {**os.environ, "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1", "WOLTS_TEST_URL": f"http://127.0.0.1:{server.server_port}"}
        script = Path(__file__).resolve().parent / "test.lua"
        result = subprocess.run([nvim, "--headless", "-u", "NONE", "-l", str(script)],
                                env=env, text=True, capture_output=True, timeout=40)
        assert result.returncode == 0, result.stdout + result.stderr
        assert not any(path == "/trap" for path, _, _ in calls)
        assert all(key is None for path, key, _ in calls if path.startswith("/public"))
        posts = [(p, k, json.loads(b)) for p, k, b in calls if b and p.endswith("/message")]
        assert len(posts) == 2 and all(k == "dummy-secret" for _, k, _ in posts)
        assert posts[0][2]["text"] == '@~/project/sample.lua#L1-2\n\nquotes " and \\ and é'
        assert sum(p.startswith("/drop") for p, _, _ in posts) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
