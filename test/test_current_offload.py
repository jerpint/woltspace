"""POST /current must not wait for a session lock on the event loop.

push-view lands here, and set_viewport writes the record under the session's
flock. A rest holds that flock while it stops tmux, so a synchronous call
would freeze every request, the /tui sockets and livereload for that long.
"""

import asyncio
import sys
from pathlib import Path

from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

import server.app as server_app


def test_set_current_url_runs_off_the_event_loop(monkeypatch):
    seen = []

    def fake_set_current_url(url, session, port):
        try:
            asyncio.get_running_loop()
            seen.append("event loop")
        except RuntimeError:
            seen.append("worker thread")

    monkeypatch.setattr(server_app, "set_current_url", fake_set_current_url)
    monkeypatch.setattr(server_app, "log_view", lambda *a, **k: None)
    monkeypatch.setattr(server_app, "get_current_url", lambda session: "/x")
    client = TestClient(server_app.app, base_url="http://localhost:7777")
    response = client.post("/current?session=pal-one", json={"url": "/x"})
    assert response.status_code == 200
    assert seen == ["worker thread"]
