"""#643 review fixes at the inbox's edges: the rearm route's caller and name, the body cap, a
bounded Codex steer, tmux's provable "no such session", and token clients that never follow
a redirect. Fakes and loopback only."""

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from inbox_support import limits, wolt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib" / "native"))

import inbox  # noqa: E402
import native_adapters as na  # noqa: E402
from server import inbox_routes  # noqa: E402
from session_runtime import RuntimeContext, RuntimeHandle, TmuxSessionRuntime  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox_routes, "WOLTS_DIR", tmp_path)
    monkeypatch.setattr(inbox, "LIMITS", limits())
    (tmp_path / "woltspace.json").write_text(json.dumps({"inbox": {"enabled": True}}))
    wolt(tmp_path, "pal")
    app = FastAPI()
    app.include_router(inbox_routes.router)
    return TestClient(app, base_url="http://localhost:7777")


def test_rearm_refuses_a_session_token_and_a_name_that_is_not_a_wolt(tmp_path, client):
    reply = client.post("/inbox/rearm", json={"wolt": "pal"}, headers={inbox_routes.TOKEN_HEADER: "tok"})
    assert (reply.status_code, reply.json()) == (403, {"error": "owner_only"})
    for bad in ("../x", "nobody"):
        reply = client.post("/inbox/rearm", json={"wolt": bad})
        assert (reply.status_code, reply.json()) == (404, {"error": "unknown_wolt"})
    assert not (tmp_path / ".space" / "run" / "x.lock").exists()


@pytest.mark.parametrize("path", ["/inbox/send", "/inbox/ready", "/inbox/rearm"])
def test_a_body_over_the_cap_is_refused_as_too_large(client, monkeypatch, path):
    monkeypatch.setattr(inbox_routes, "MAX_BODY", 64)
    big = b'{"text": "' + b"x" * 100 + b'"}'
    reply = client.post(path, content=big, headers={"Content-Type": "application/json"})
    assert (reply.status_code, reply.json()) == (413, {"error": "too_large"})

    def chunks():  # no Content-Length: the streamed bytes are counted
        yield big[:50]
        yield big[50:]
    reply = client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
    assert (reply.status_code, reply.json()) == (413, {"error": "too_large"})


def _codex_server(sock, answer_turns):
    """An app-server that answers initialize (and thread/turns/list when asked), then only ever
    streams notifications: the reply never comes."""
    from websockets.sync.server import unix_serve

    def handler(conn):
        for raw in conn:
            message = json.loads(raw)
            if message.get("method") == "initialize" or (answer_turns and message.get("method") == "thread/turns/list"):
                result = {"data": [{"id": "turn-7", "status": "inProgress"}]} if answer_turns else {}
                conn.send(json.dumps({"id": message["id"], "result": result}))
            elif "id" in message:
                try:
                    while True:
                        conn.send(json.dumps({"method": "thread/status/changed", "params": {}}))
                        time.sleep(0.01)
                except Exception:
                    return
    server = unix_serve(handler, sock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("answer_turns, expected", [(False, ("pending", "busy")), (True, ("unknown", "timeout"))])
def test_a_codex_steer_ends_at_the_deliver_budget_while_only_notifications_arrive(tmp_path, monkeypatch,
                                                                                    answer_turns, expected):
    wolts = Path(os.environ.get("TMPDIR", "/tmp")) / f"rf{os.getpid()}{time.monotonic_ns() % 10**6}"
    wolts.mkdir()
    native = na.prepare(wolts, "pal-x", "codex", "a1")
    sock = str(Path(native["addr"]) / "app.sock")
    na.write_ready(native["addr"], {"kind": native["kind"], "attempt": "a1", "sock": sock, "thread": "t1"})
    na.write_state(native["addr"], True)  # busy: the steer path
    monkeypatch.setattr(na, "BUDGET", 0.6)
    server = _codex_server(sock, answer_turns)
    try:
        started = time.monotonic()
        assert na.deliver({"status": "running", "attempt": "a1", "native": native}, "hint") == expected
        assert time.monotonic() - started < 2.0
    finally:
        server.shutdown()
        import shutil
        shutil.rmtree(wolts, ignore_errors=True)


def test_tmux_says_no_such_session_only_when_it_answered():
    def runner(answer):
        def run(command, **kwargs):
            if isinstance(answer, BaseException):
                raise answer
            return SimpleNamespace(stdout=answer, returncode=0)
        return run
    handle = RuntimeHandle("pal-a", "pal-a", "%1")
    exists = lambda answer: TmuxSessionRuntime(RuntimeContext(tmux_bin="tmux"), runner=runner(answer)).session_exists(handle)
    assert exists("pal-a\t%1\t10\t1\n") is True
    assert exists(subprocess.CalledProcessError(1, ["tmux"])) is False  # no such session, or no server
    assert exists(subprocess.TimeoutExpired("tmux", 5)) is None
    assert exists(OSError("no tmux binary")) is None


@pytest.fixture
def redirecting():
    """A loopback lodge that redirects every POST, and records whether the token followed it."""
    seen = []

    class Lodge(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            seen.append((self.path, self.headers.get(inbox_routes.TOKEN_HEADER)))
            self.send_response(303 if self.path != "/elsewhere" else 200)  # urllib follows a 303 as a GET
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST

        def log_message(self, *a):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Lodge)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_token_clients_treat_a_redirect_as_an_error_and_never_send_the_token_on(redirecting, monkeypatch):
    api, seen = redirecting
    monkeypatch.setenv("WOLTSPACE_API", api)
    monkeypatch.setenv("WOLTSPACE_INBOX_TOKEN", "tok")
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "pal-a")
    na.announce_ready("a1")
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("TMPDIR", "/tmp"), "WOLTSPACE_API": api,
           "WOLTSPACE_INBOX_TOKEN": "tok"}
    cli = subprocess.run([sys.executable, str(ROOT / "container" / "bin" / "woltspace"), "inbox", "fetch"],
                         env=env, capture_output=True, text=True, timeout=30)
    assert cli.returncode != 0
    assert seen == [("/inbox/ready", "tok"), ("/inbox/fetch", "tok")]  # nothing reached /elsewhere
