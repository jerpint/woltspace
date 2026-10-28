"""`woltspace start` opens the lodge in the owner's browser, paired."""

import argparse
import sys
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import lodge_auth
from woltspace import cli, lifecycle


@pytest.fixture
def desk(tmp_path, monkeypatch):
    """A started lodge on a throwaway data root, at a desktop terminal."""
    monkeypatch.setenv("WOLTSPACE_WOLTS_DIR", str(tmp_path))
    for name in ("WOLTSPACE_LODGE_AUTH", "WOLTSPACE_WOLT_SESSION", "WOLT_SESSION", "SSH_CONNECTION", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(lifecycle, "start", lambda layout, timeout: (
        0, {"endpoint": "http://127.0.0.1:7777", "detail": "started"}))
    monkeypatch.setattr(lifecycle, "tunnel_report", lambda layout: {})
    monkeypatch.setattr(cli, "_at_a_terminal", lambda: True)
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
    return tmp_path, opened


def _start(**kw) -> int:
    args = dict(host="", port=0, timeout=1.0, json=False, no_browser=False)
    args.update(kw)
    return cli._start(argparse.Namespace(**args))


def _code(url: str) -> str:
    parsed = urlparse(url)
    assert parsed.path == "/pair"
    return parse_qs(parsed.query)["code"][0]


def test_with_auth_on_the_browser_comes_in_paired(desk, capsys):
    root, opened = desk
    (root / ".env").write_text("WOLTSPACE_LODGE_AUTH=1\n")

    assert _start() == 0

    assert len(opened) == 1 and opened[0].startswith("http://127.0.0.1:7777/pair?code=")
    code = _code(opened[0])
    assert code not in capsys.readouterr().out
    assert lodge_auth.redeem_pairing_code(root, code)
    assert lodge_auth.redeem_pairing_code(root, code) is None


def test_every_start_gets_a_fresh_code(desk, monkeypatch):
    root, opened = desk
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")

    _start()
    _start()

    assert len(opened) == 2 and _code(opened[0]) != _code(opened[1])


def test_with_auth_off_it_opens_the_plain_address(desk):
    root, opened = desk
    assert _start() == 0
    assert opened == ["http://127.0.0.1:7777"]
    assert not lodge_auth.auth_dir(root).exists()


def test_the_shell_setting_wins_over_the_env_file(desk, monkeypatch):
    root, opened = desk
    (root / ".env").write_text("WOLTSPACE_LODGE_AUTH=1\n")
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "0")
    _start()
    assert opened == ["http://127.0.0.1:7777"]


@pytest.mark.parametrize("setup", [
    lambda mp: mp.setenv("WOLTSPACE_WOLT_SESSION", "softie-a-b-123456"),
    lambda mp: mp.setenv("WOLT_SESSION", "softie-a-b-123456"),
    lambda mp: mp.setenv("SSH_CONNECTION", "10.0.0.2 5000 10.0.0.1 22"),
    lambda mp: mp.setenv("SSH_TTY", "/dev/ttys001"),
    lambda mp: mp.setattr(cli, "_at_a_terminal", lambda: False),
])
def test_no_browser_and_no_code_outside_the_owners_desk(desk, monkeypatch, setup):
    root, opened = desk
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    lodge_auth.ensure_keys(root)
    setup(monkeypatch)

    _start()

    assert opened == []
    assert lodge_auth._read_json(lodge_auth.auth_dir(root) / "pairing.json") == {}


def test_linux_without_a_display_opens_nothing(desk, monkeypatch):
    root, opened = desk
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    _start()
    assert opened == []


def test_no_browser_flag_and_json_open_nothing(desk, monkeypatch, capsys):
    root, opened = desk
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")
    _start(no_browser=True)
    assert "woltspace pair" in capsys.readouterr().out
    _start(json=True)
    assert opened == []
    assert "/pair?code=" not in capsys.readouterr().out
    assert lodge_auth._read_json(lodge_auth.auth_dir(root) / "pairing.json") == {}


def test_a_browser_that_fails_never_fails_the_start(desk, monkeypatch, capsys):
    root, _ = desk
    monkeypatch.setenv("WOLTSPACE_LODGE_AUTH", "1")

    def broken(url):
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "open", broken)
    assert _start() == 0
    assert "woltspace pair" in capsys.readouterr().out


def test_a_failed_start_opens_nothing(desk, monkeypatch):
    root, opened = desk
    monkeypatch.setattr(lifecycle, "start", lambda layout, timeout: (1, {"error": "boom"}))
    _start()
    assert opened == []
