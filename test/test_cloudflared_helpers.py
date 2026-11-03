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
