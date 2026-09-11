import json
import os
from pathlib import Path
import shlex
import subprocess


ROOT = Path(__file__).parents[1]
NOTIFY = ROOT / "container" / "bin" / "notify"


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "payload.json"
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/bin/bash
while [ \"$#\" -gt 0 ]; do
  if [ \"$1\" = \"-d\" ]; then
    printf '%s' \"$2\" > \"$NOTIFY_CAPTURE\"
    shift 2
  else
    shift
  fi
done
printf '%s\\n' '{\"ok\":true,\"adapter\":\"telegram\"}'
"""
    )
    curl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "NOTIFY_CAPTURE": str(capture),
            "WOLTSPACE_API": "http://notify.test",
            "WOLTSPACE_WOLT_NAME": "testwolt",
            "WOLTSPACE_WOLT_SESSION": "",
            "WOLT_SESSION": "",
        }
    )
    return env, capture


def test_stdin_preserves_shell_metacharacters_and_multiline_text(tmp_path):
    env, capture = _environment(tmp_path)
    marker = tmp_path / "must-not-exist"
    message = (
        f"literal `touch {marker}` and $(touch {marker})\n"
        'quotes: "double" and \'single\'; wildcard: *; dollar: $HOME\n\n'
    )

    result = subprocess.run(
        [NOTIFY, "--telegram", "-100123"],
        input=message,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    payload = json.loads(capture.read_text())
    assert payload["chat_id"] == "-100123"
    assert payload["message"] == f"🦫 testwolt: {message}"


def test_quoted_heredoc_keeps_command_looking_text_literal(tmp_path):
    env, capture = _environment(tmp_path)
    marker = tmp_path / "must-not-exist"
    delimiter = "WOLTSPACE_NOTIFY_7F3A91C2"
    message = (
        f"do not run `touch {marker}` or $(touch {marker})\n"
        'formatting stays intact: *bold-ish* `code` "quotes"\n'
    )
    command = (
        f"{shlex.quote(str(NOTIFY))} --telegram 123 <<'{delimiter}'\n"
        f"{message}{delimiter}\n"
    )

    result = subprocess.run(
        ["sh", "-c", command],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert json.loads(capture.read_text())["message"] == f"🦫 testwolt: {message}"


def test_rejects_message_arguments_without_sending(tmp_path):
    env, capture = _environment(tmp_path)

    result = subprocess.run(
        [NOTIFY, "legacy message argument"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 2
    assert "message arguments are not supported" in result.stderr
    assert not capture.exists()


def test_help_prints_usage_without_sending(tmp_path):
    env, capture = _environment(tmp_path)

    result = subprocess.run(
        [NOTIFY, "--help"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0
    assert "notify --telegram CHAT_ID" in result.stdout
    assert not capture.exists()


def test_rejects_extra_arguments_without_sending(tmp_path):
    env, capture = _environment(tmp_path)

    result = subprocess.run(
        [NOTIFY, "--telegram", "123", "one", "two"],
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 2
    assert "message arguments are not supported" in result.stderr
    assert not capture.exists()


def test_rejects_empty_stdin_and_invalid_chat_id(tmp_path):
    env, capture = _environment(tmp_path)

    empty = subprocess.run(
        [NOTIFY],
        input="",
        text=True,
        capture_output=True,
        env=env,
    )
    invalid_chat = subprocess.run(
        [NOTIFY, "--telegram", "not-a-chat"],
        input="hello",
        text=True,
        capture_output=True,
        env=env,
    )

    assert empty.returncode == 2
    assert invalid_chat.returncode == 2
    assert not capture.exists()


def test_session_context_recommends_quoted_heredocs_for_explicit_routes():
    from sessions import _adapter_context

    telegram = _adapter_context(
        {
            "adapter": "telegram",
            "chat_id": "123",
            "session_url": "https://lodge.test/tui?session=one",
        }
    )
    slack = _adapter_context(
        {
            "adapter": "slack",
            "chat_id": "C123",
            "thread_ts": "123.456",
            "session_url": "https://lodge.test/tui?session=two",
        }
    )

    assert "single-quoted heredoc to: notify --telegram 123" in telegram
    assert "single-quoted heredoc to: notify --slack C123 123.456" in slack
    assert '"your message"' not in telegram + slack
