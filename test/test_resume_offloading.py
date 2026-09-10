"""Resume waits for an agent — it must not do that on an event loop.

`resume_session` polls the process table for up to 12s (18s across the
escalation). Called straight from an `async def` that freezes the whole
control plane: every HTTP request, the /tui socket proxy, viewport livereload
and the TUI's own session poll stop for the duration — and waking a session is
the single most common action in the lodge.
"""

import ast
import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
# `server` is a package in the checkout root, not on the test path by default —
# the suite only picks it up when another module happens to have added it.
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "container" / "lib"))

# server/app.py puts `$WOLTSPACE_DIR/container/lib` at the FRONT of sys.path when
# it imports. A shell inside a running colony inherits WOLTSPACE_DIR pointing at
# the installed wheel's bundle, so without this pin the server would import the
# *installed* sessions.py ahead of this worktree's, and every test that reads
# `sessions` afterwards would be testing shipped code instead of the diff.
import os
os.environ["WOLTSPACE_DIR"] = str(ROOT)

from server import app as app_module


def test_a_slow_resume_leaves_the_control_plane_answering(monkeypatch):
    def slow_resume(name, prompt=""):
        time.sleep(0.5)
        return {"name": name, "status": "respawned", "detail": "slept"}

    monkeypatch.setattr(app_module, "resume_session", slow_resume)

    async def scenario():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            # Timed from before the resume starts, on purpose: if the resume
            # holds the loop, even this sleep does not come back until it is
            # over, and a clock started after it would measure nothing.
            started = time.monotonic()
            resume = asyncio.create_task(
                client.post("/sessions/somewolt-abc123/resume",
                            json={"prompt": "wake up"})
            )
            # Let the handler reach the blocking call before we knock.
            await asyncio.sleep(0.05)
            health = await client.get("/health")
            answered_in = time.monotonic() - started
            return answered_in, health, await resume

    answered_in, health, resumed = asyncio.run(scenario())

    assert health.status_code == 200
    # Answered well inside the resume's own 0.5s wait — the loop stayed live
    # while the resume was still in flight.
    assert answered_in < 0.3, f"/health waited {answered_in:.2f}s on the resume"
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "respawned"


@pytest.mark.parametrize("route", [
    "/sessions/somewolt-abc123/message",
    "/wolts/somewolt/message",
])
def test_a_paced_delivery_leaves_the_control_plane_answering(monkeypatch, route):
    """Delivery used to be one tmux call; now it is a paced sequence.

    A long message goes into the composer as 500-character pastes 0.3s apart
    — about 1.5s for 3KB, plus whatever the session's delivery lock costs.
    Called inline from an `async def` that is 1.5s in which nothing else in
    the colony is served: not /health, not the /tui socket proxy, not
    viewport livereload. Both message routes hand it to a thread.
    """
    def slow_delivery(session_id, text, from_wolt="", from_session=""):
        time.sleep(0.5)
        return {"status": "delivered", "session": session_id, "harness": "claude"}

    monkeypatch.setattr(app_module, "deliver_message", slow_delivery)
    monkeypatch.setattr(app_module, "resolve_active_session",
                        lambda wolt: "somewolt-abc123")

    async def scenario():
        transport = httpx.ASGITransport(app=app_module.app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://testserver") as client:
            started = time.monotonic()
            delivery = asyncio.create_task(
                client.post(route, json={"text": "a long one " * 300})
            )
            await asyncio.sleep(0.05)
            health = await client.get("/health")
            return time.monotonic() - started, health, await delivery

    answered_in, health, delivered = asyncio.run(scenario())

    assert health.status_code == 200
    assert answered_in < 0.3, f"/health waited {answered_in:.2f}s on a paste"
    assert delivered.status_code == 200
    assert delivered.json()["ok"] is True


ADAPTERS = [
    "container/bot/telegram_adapter.py",
    "container/bot/telegram_adapter_v1.py",
    "container/bot/slack_adapter.py",
]

BLOCKING = {"message_session", "resume_session"}


@pytest.mark.parametrize("relative", ADAPTERS)
def test_chat_adapters_never_resume_on_their_event_loop(relative):
    """The bots have the same shape as the HTTP handler.

    `message_session` is `resume_session` with a nicer return value: called
    inline from an async handler it stops the bot answering every other chat
    for as long as one session takes to boot.
    """
    tree = ast.parse((ROOT / relative).read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in BLOCKING):
                offenders.append(f"{node.name}:{call.lineno}")
    assert offenders == [], (
        f"{relative}: blocking session call(s) on the event loop at "
        f"{offenders} — wrap them in asyncio.to_thread"
    )


def test_the_adapters_do_hand_those_calls_to_a_thread(monkeypatch):
    """The guard above passes trivially if the calls vanish — they didn't."""
    for relative in ADAPTERS:
        source = (ROOT / relative).read_text()
        assert "asyncio.to_thread(message_session" in source, relative


def test_the_control_plane_never_delivers_on_its_event_loop():
    """Same guard, on the server's own handlers.

    `deliver_message` joined `resume_session` on the blocking list the day a
    long message started arriving as several paced pastes. The AST check is
    what stops the next route from being written the quick way.
    """
    tree = ast.parse((ROOT / "server" / "app.py").read_text())
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id in {"deliver_message", "resume_session"}):
                offenders.append(f"{node.name}:{call.lineno}")
    assert offenders == [], (
        f"server/app.py: blocking session call(s) on the event loop at "
        f"{offenders} — wrap them in asyncio.to_thread"
    )
    source = (ROOT / "server" / "app.py").read_text()
    assert source.count("asyncio.to_thread(\n        deliver_message") == 2, (
        "both message routes should still be delivering, just off the loop"
    )
