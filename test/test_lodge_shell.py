"""One-view lodge layout (/shell): the page renders and the framed pages cooperate."""

import asyncio
import re
from pathlib import Path

import httpx

from server import app as app_module

ROOT = Path(__file__).resolve().parents[1]


async def _request(method, path, **kwargs):
    transport = httpx.ASGITransport(app=app_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost:7777",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_shell_page_renders_with_its_page_frame(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("GET", "/shell"))

    assert response.status_code == 200
    body = response.text
    assert 'name="lodge-shell-page"' in body
    assert "/static/lodge-shell.js" in body
    assert "/static/lodge-shell.css" in body


def test_shell_script_treats_backend_data_as_data():
    source = (ROOT / "public/static/lodge-shell.js").read_text()

    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "javascript:"):
        assert banned not in source, banned
    assert not re.search(r"\bon[a-z]+\s*=\s*['\"`]", source)
    # Messages are accepted only from this origin.
    assert "e.origin !== location.origin" in source


def test_shell_page_refuses_to_be_framed(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "WOLTS_DIR", tmp_path)
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))

    response = asyncio.run(_request("GET", "/shell"))

    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["x-frame-options"] == "DENY"


def test_framed_pages_hide_their_own_chrome_only_inside_the_shell():
    base = (ROOT / "templates/base.html").read_text()
    tui = (ROOT / "templates/tui.html").read_text()
    style = (ROOT / "public/static/style.css").read_text()

    assert "window.name === 'lodge-shell-page' && window.parent !== window" in base
    assert "window.parent.location.pathname === '/shell'" in base
    assert ".in-shell .sidebar, .in-shell .hamburger { display: none; }" in style
    assert "inLodgeShell('lodge-shell-session')" in tui
    assert "window.parent.location.pathname === '/shell'" in tui
    assert ".in-shell #topbar { display: none; }" in tui


def test_session_screen_hands_off_instead_of_opening_a_terminal_in_the_page_frame():
    tui = (ROOT / "templates/tui.html").read_text()

    handoff = tui.index("woltspace-shell-open-session")
    assert handoff < tui.index("new Terminal(")
    assert "await new Promise(() => {});" in tui[handoff:tui.index("new Terminal(")]
