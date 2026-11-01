import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
def test_nvim_plugin(tmp_path, monkeypatch):
    nvim = shutil.which("nvim")
    if not nvim: pytest.skip("Neovim 0.11+ is required for the editor smoke")
    # Model a caller inside a real lodge, using disposable sentinel files only.
    inherited_home, inherited_lodge = tmp_path / "home", tmp_path / "lodge"
    keydir = inherited_lodge / ".space/run/lodge"
    inherited_home.mkdir()
    keydir.mkdir(parents=True)
    sentinels = [inherited_home / ".curlrc", keydir / "owner.key", keydir / "wolt.key"]
    for path in sentinels: path.write_bytes(b"caller-sentinel\n")
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sentinels}
    for key, value in {"HOME": inherited_home, "WOLTSPACE_WOLTS_DIR": inherited_lodge,
                       "WOLTS_DIR": inherited_lodge, "WOLTSPACE_LODGE_KEY": "caller-key",
                       "WOLTSPACE_WOLT_SESSION": "caller-session", "WOLT_SESSION": "caller-session",
                       "WOLTS_TEST_ROOT": inherited_lodge}.items():
        monkeypatch.setenv(key, str(value))
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
        script = Path(__file__).resolve().parent / "test.lua"
        command = [nvim, "--headless", "-u", "NONE", "-i", "NONE", "-l", str(script)]
        refused = subprocess.run(command, text=True, capture_output=True, timeout=10)
        assert refused.returncode != 0 and "private test root" in refused.stderr
        # Do not inherit lodge credentials, session identifiers or user config.
        with tempfile.TemporaryDirectory(prefix="wolts-nvim-") as temporary:
            root = Path(temporary).resolve()
            for name in ("home", "wolts", "tmp", "config", "data", "state", "cache"):
                (root / name).mkdir()
            env = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "TERM": "xterm-256color",
                   "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
                   "WOLTSPACE_WOLTS_DIR": str(root / "wolts"), "WOLTS_DIR": str(root / "wolts"),
                   "WOLTS_TEST_ROOT": str(root), "WOLTS_TEST_URL": f"http://127.0.0.1:{server.server_port}",
                   "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
            env.update({f"XDG_{name.upper()}_HOME": str(root / name) for name in ("config", "data", "state", "cache")})
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=40)
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
        assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in sentinels} == before
