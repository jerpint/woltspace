"""Native session commands must work without lodge helpers on PATH."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shadow_helper", [False, True], ids=["missing-helper", "shadow-helper"])
@pytest.mark.parametrize(
    "args, expected_code, expected_help",
    [([], 1, "woltspace control CLI"), (["list", "--help"], 0, "--alive")],
    ids=["bare-session", "list-help"],
)
def test_native_session_uses_its_own_interpreter(
    tmp_path, shadow_helper, args, expected_code, expected_help,
):
    # Neither the real lodge nor its helper directory is visible to this child.
    path = os.pathsep.join(["/usr/bin", "/bin"])
    if shadow_helper:
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        helper = bin_dir / "woltspace-python"
        helper.write_text("#!/bin/sh\necho 'wrong interpreter' >&2\nexit 97\n")
        helper.chmod(0o755)
        path = str(bin_dir) + os.pathsep + path

    env = {
        "HOME": str(tmp_path),
        "PATH": path,
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *sys.path]),
        "WOLTSPACE_DIR": str(ROOT),
        "WOLTSPACE_WOLTS_DIR": str(tmp_path / "wolts"),
        "WOLTSPACE_API": "http://127.0.0.1:9",
        "WOLTSPACE_ISOLATION": "host",
    }
    # Exercise the native parser and real exec boundary, without contacting an
    # API: bare session and list --help both terminate at argument parsing.
    result = subprocess.run(
        [sys.executable, "-c",
         "from woltspace.cli import main; raise SystemExit(main())",
         "session", *args],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == expected_code, result.stderr
    assert expected_help in result.stdout
    assert result.stderr == ""


def test_native_wolt_reaches_the_bundled_client(tmp_path):
    # delete without --yes is refused by the bundled client, before any API call.
    env = {
        "HOME": str(tmp_path),
        "PATH": os.pathsep.join(["/usr/bin", "/bin"]),
        "PYTHONPATH": os.pathsep.join([str(ROOT / "src"), *sys.path]),
        "WOLTSPACE_DIR": str(ROOT),
        "WOLTSPACE_WOLTS_DIR": str(tmp_path / "wolts"),
        "WOLTSPACE_API": "http://127.0.0.1:9",
        "WOLTSPACE_ISOLATION": "host",
    }
    result = subprocess.run(
        [sys.executable, "-c", "from woltspace.cli import main; raise SystemExit(main())",
         "wolt", "delete", "pal"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert result.stderr == "woltspace: wolt delete needs --yes (the wolt will not be recoverable)\n"


def test_a_lodge_that_does_not_answer_in_time_is_not_a_traceback(monkeypatch, capsys):
    import argparse, runpy, urllib.request
    client = runpy.run_path(str(ROOT / "container" / "bin" / "woltspace"))
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(SystemExit) as e:
        client["cmd_wolt_delete"](argparse.Namespace(name="pal", yes=True))
    assert e.value.code == 1 and capsys.readouterr().err == (
        "no answer from the lodge in 15 s; it may still be working (check: woltspace wolt archived)\n")
