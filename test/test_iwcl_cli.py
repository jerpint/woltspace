"""Shell-safety tests for the stdin-only IWCL command."""

import argparse
import io
import json
import os
from pathlib import Path
import runpy
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
WOLTSPACE = ROOT / "container" / "bin" / "woltspace"


def _load_client():
    return runpy.run_path(str(WOLTSPACE))


def test_session_send_reads_multiline_message_from_stdin(monkeypatch):
    client = _load_client()
    sent = {}
    monkeypatch.setenv("WOLTSPACE_WOLT_NAME", "")
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "")
    monkeypatch.delenv("WOLT_NAME", raising=False)
    monkeypatch.delenv("WOLT_SESSION", raising=False)

    def fake_req(method, path, body=None):
        sent.update(method=method, path=path, body=body)
        return 200, {"ok": True, "session": "target-maple-a1b2c3"}

    client["cmd_session_send"].__globals__["_req"] = fake_req
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('literal `whoami` $(id) $HOME * "quotes"\n\nlast\n'),
    )
    args = argparse.Namespace(
        target="target-maple-a1b2c3", message=[], from_="", json=False
    )

    client["cmd_session_send"](args)

    assert sent == {
        "method": "POST",
        "path": "/sessions/target-maple-a1b2c3/message",
        "body": {
            "text": 'literal `whoami` $(id) $HOME * "quotes"\n\nlast\n',
            "from_wolt": "",
            "from_session": "",
        },
    }


def test_session_send_rejection_prints_executable_recovery(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture_args = tmp_path / "args.json"
    capture_body = tmp_path / "body.txt"
    fake = fake_bin / "woltspace"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "pathlib.Path(os.environ['IWCL_ARGS']).write_text(json.dumps(sys.argv[1:]))\n"
        "pathlib.Path(os.environ['IWCL_BODY']).write_bytes(sys.stdin.buffer.read())\n"
    )
    fake.chmod(0o755)
    message = 'literal `whoami` $(id) $HOME * "quotes"\n\nlast'
    rejected = subprocess.run(
        [
            WOLTSPACE,
            "session",
            "send",
            "target-maple-a1b2c3",
            message,
            "--from",
            "sender",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert rejected.returncode == 2
    assert "only what survived shell expansion" in rejected.stderr
    recovery = rejected.stderr.split("Run this instead:\n\n", 1)[1]
    env = os.environ.copy()
    env.update(
        PATH=f"{fake_bin}:{env['PATH']}",
        IWCL_ARGS=str(capture_args),
        IWCL_BODY=str(capture_body),
    )
    rerun = subprocess.run(
        ["sh", "-c", recovery],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(capture_args.read_text()) == [
        "session",
        "send",
        "target-maple-a1b2c3",
        "--from",
        "sender",
        "--json",
    ]
    assert capture_body.read_text() == f"{message}\n"


def test_session_send_rejects_ambiguous_or_empty_message(monkeypatch, capsys):
    client = _load_client()
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    base = dict(target="target", from_="", json=False)

    with pytest.raises(SystemExit) as multiple:
        client["cmd_session_send"](
            argparse.Namespace(**base, message=["one", "two"])
        )
    assert multiple.value.code == 2
    assert "pass one message body on stdin" in capsys.readouterr().err

    with pytest.raises(SystemExit) as empty:
        client["cmd_session_send"](argparse.Namespace(**base, message=[]))
    assert empty.value.code == 2
    assert "message must not be empty" in capsys.readouterr().err
