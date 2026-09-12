"""Shell-safety tests for the stdin-only IWCL command."""

import argparse
import io
import json
import os
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).parents[1]
WOLTSPACE = ROOT / "container" / "bin" / "woltspace"


class UnreadableTty(io.StringIO):
    def isatty(self):
        return True

    def read(self, *args, **kwargs):
        raise AssertionError("TTY input must not be read")


def _load_client():
    return runpy.run_path(str(WOLTSPACE))


def test_shared_input_never_blocks_reading_a_tty():
    from notify_prompt import MessageInputError, read_message_input

    assert read_message_input(
        ["woltspace", "session", "spawn", "target"],
        [],
        UnreadableTty(),
        prefix="WOLTSPACE_IWCL",
        allow_empty=True,
    ) == ""

    with pytest.raises(MessageInputError, match="use a single-quoted heredoc"):
        read_message_input(
            ["woltspace", "session", "send", "target"],
            [],
            UnreadableTty(),
            prefix="WOLTSPACE_IWCL",
        )

    with pytest.raises(MessageInputError, match="use a single-quoted heredoc"):
        read_message_input(
            ["notify"],
            [],
            UnreadableTty(),
            prefix="WOLTSPACE_NOTIFY",
        )


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


@pytest.mark.parametrize(
    "prompt",
    ["inspect `code` and $(literal)\n\nthen report\n", ""],
)
def test_session_spawn_reads_optional_seed_prompt_from_stdin(monkeypatch, prompt):
    client = _load_client()
    sent = {}
    monkeypatch.setenv("WOLTSPACE_WOLT_NAME", "sender")
    monkeypatch.setenv("WOLTSPACE_WOLT_SESSION", "sender-maple-a1b2c3")

    def fake_req(method, path, body=None):
        sent.update(method=method, path=path, body=body)
        return 200, {"name": "target-maple-d4e5f6", "url": "https://lodge.test"}

    client["cmd_session_spawn"].__globals__["_req"] = fake_req
    monkeypatch.setattr("sys.stdin", io.StringIO(prompt))
    args = argparse.Namespace(
        wolt="target",
        prompt=[],
        from_="",
        json=False,
        workdir="/tmp/work tree",
        auto=True,
    )

    client["cmd_session_spawn"](args)

    assert sent["method"] == "POST"
    assert sent["path"] == "/sessions/new/lodge"
    assert sent["body"] == {
        "wolt": "target",
        "prompt": prompt,
        "from_wolt": "sender",
        "from_session": "sender-maple-a1b2c3",
        "workdir": "/tmp/work tree",
        "execution_policy": "auto",
    }


def test_session_spawn_rejects_legacy_prompt_with_recovery():
    prompt = 'read `date` and $HOME, then say "done"'
    rejected = subprocess.run(
        [
            WOLTSPACE,
            "session",
            "spawn",
            "target",
            prompt,
            "--from",
            "sender",
            "--json",
            "--workdir",
            "/tmp/work tree",
            "--auto",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert rejected.returncode == 2
    error = rejected.stderr
    assert "only what survived shell expansion" in error
    assert "woltspace session spawn target --from sender --json" in error
    assert "--workdir '/tmp/work tree' --auto <<'WOLTSPACE_IWCL_" in error
    assert prompt in error


def test_native_cli_delegates_whole_session_noun(tmp_path, monkeypatch):
    from woltspace import cli

    client = tmp_path / "container" / "bin" / "woltspace"
    client.parent.mkdir(parents=True)
    client.write_text("#!/usr/bin/env python3\n")
    layout = SimpleNamespace(
        install_root=tmp_path,
        endpoint="http://127.0.0.1:8123",
    )
    monkeypatch.setattr(cli.RuntimeLayout, "from_env", lambda: layout)
    monkeypatch.delenv("WOLTSPACE_API", raising=False)
    executed = {}
    monkeypatch.setattr(
        cli.os,
        "execv",
        lambda executable, argv: executed.update(
            executable=executable, argv=argv
        ),
    )

    result = cli._session(
        argparse.Namespace(
            session_args=["send", "target-maple-a1b2c3", "--json"]
        )
    )

    assert result == 0
    assert executed == {
        "executable": str(client),
        "argv": [
            str(client),
            "session",
            "send",
            "target-maple-a1b2c3",
            "--json",
        ],
    }
    assert os.environ["WOLTSPACE_API"] == "http://127.0.0.1:8123"


def test_native_parser_passes_session_help_to_bundled_client():
    from woltspace.cli import build_parser

    args = build_parser().parse_args(["session", "send", "--help"])

    assert args.session_args == ["send", "--help"]
