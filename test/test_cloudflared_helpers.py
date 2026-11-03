"""The share tunnel's log and the orphan sweep's view of processes."""

import os
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "container" / "lib"))

import app_sharing  # noqa: E402
import tunnel  # noqa: E402


def test_the_sweep_reads_whole_command_lines(monkeypatch):
    seen = {}

    def run(args, **_kwargs):
        seen["args"] = args
        return SimpleNamespace(stdout="42 cloudflared tunnel --http-host-header share.example\n")
    monkeypatch.setattr(app_sharing.subprocess, "run", run)
    assert app_sharing._cloudflared_processes() == [(42, "cloudflared tunnel --http-host-header share.example")]
    assert "-ww" in seen["args"]


@pytest.fixture
def fake_cloudflared(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "cloudflared"
    script.write_text("#!/bin/sh\necho 'https://quiet-share-test.trycloudflare.com'\nexec sleep 30\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(logs))
    real_sleep = time.sleep
    monkeypatch.setattr(tunnel.time, "sleep", lambda _s: real_sleep(0.1))
    return logs


def test_a_quick_tunnels_log_is_private_and_gone_once_read(fake_cloudflared, monkeypatch):
    modes = []
    real_open = open

    def watching_open(path, *args, **kwargs):
        if str(path).endswith("-cloudflared.log"):
            modes.append(stat.S_IMODE(Path(path).stat().st_mode))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr("builtins.open", watching_open)
    result = tunnel.start_cloudflared(7777)
    try:
        assert result["url"] == "https://quiet-share-test.trycloudflare.com"
        assert modes and all(mode == 0o600 for mode in modes)
        assert list(fake_cloudflared.iterdir()) == []
    finally:
        os.kill(result["pid"], signal.SIGTERM)
