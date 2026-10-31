"""Hermes tui_gateway over a loopback WebSocket, with inbox hints dispatched in the TUI's own transport.

Runs on Hermes's own interpreter: `python hermes_gateway.py <port> <attempt dir> <attempt>`. The TUI attaches
through HERMES_TUI_GATEWAY_URL. A hint is a file in the attempt directory; the watcher claims it with one
rename and runs `prompt.submit` through tui_gateway's dispatch bound to the TUI's own transport, so the
turn's events render in the terminal rather than going to a second client. Never reads mail.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from tui_gateway import server, ws

port, directory, attempt = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
transports = []


class _Tracked(ws.WSTransport):  # handle_ws builds one per connection; the first is the TUI's
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        transports.append(self)


class _Answer:  # inline replies to the watcher's own lookups; never shown anywhere
    def __init__(self):
        self.frames = []

    def write(self, obj):
        self.frames.append(obj)
        return True


ws.WSTransport = _Tracked
app = FastAPI()


@app.websocket("/ws")
async def gateway(sock: WebSocket):
    await ws.handle_ws(sock)


def _live_session():
    sink = _Answer()  # session.active_list runs on the gateway's pool, which replies to the transport given
    answer = server.dispatch({"jsonrpc": "2.0", "id": "native-list", "method": "session.active_list", "params": {}},
                             sink)
    for _ in range(50):
        if answer is not None or sink.frames:
            break
        time.sleep(0.02)
    answer = answer or (sink.frames[0] if sink.frames else {})
    sessions = (answer.get("result") or {}).get("sessions") or []
    return sessions[0].get("session_id") or sessions[0].get("id") if len(sessions) == 1 else None


def _write_ready(session_id):
    if (directory / "retired").exists():
        return
    tmp = directory / f".ready.json.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"kind": "hermes-gateway", "attempt": attempt, "pid": os.getpid(), "gateway_session": session_id},
                  handle)
    os.replace(tmp, directory / "ready.json")
    _announce_ready()


def _announce_ready():
    """As native_adapters.announce_ready (this runs on Hermes's own interpreter): one bounded,
    reader-authenticated POST so mail that waited gets its hint. A failure is ignored."""
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):  # the token never follows a redirect
        def redirect_request(self, *args, **kwargs):
            return None
    token, session = os.environ.get("WOLTSPACE_INBOX_TOKEN"), os.environ.get("WOLTSPACE_WOLT_SESSION")
    if token and session:
        request = urllib.request.Request(
            f"{os.environ.get('WOLTSPACE_API', 'http://localhost:7777')}/inbox/ready", method="POST",
            data=json.dumps({"session": session, "attempt": attempt}).encode(),
            headers={"Content-Type": "application/json", "X-Woltspace-Inbox-Token": token})
        try:
            urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=2).close()
        except Exception:
            pass


def _watch():
    published = None
    while not (directory / "retired").exists():
        time.sleep(0.2)
        session_id = _live_session() if transports else None
        if session_id and session_id != published:
            _write_ready(session_id)
            published = session_id
        if not session_id:
            continue
        claimed = directory / f".claimed.{os.getpid()}"
        try:
            os.rename(directory / "hint", claimed)
        except FileNotFoundError:
            continue
        text = claimed.read_text().strip()
        claimed.unlink()
        # Bound to the TUI's transport, so the turn's events render there. Its reply (an id the TUI never
        # sent) goes there too; an inline one is returned here instead and dropped.
        server.dispatch({"jsonrpc": "2.0", "id": f"native-hint-{time.monotonic_ns()}", "method": "prompt.submit",
                         "params": {"session_id": session_id, "text": text}}, transports[0])


threading.Thread(target=_watch, daemon=True).start()
uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
