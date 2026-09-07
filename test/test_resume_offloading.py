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
