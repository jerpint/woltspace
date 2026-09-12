import json
import os
from pathlib import Path
import re
import runpy
import subprocess
from unittest.mock import patch

import pytest


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
    (bin_dir / "notify").symlink_to(NOTIFY)

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
    from notify_prompt import notify_heredoc

    env, capture = _environment(tmp_path)
    marker = tmp_path / "must-not-exist"
    delimiter = "WOLTSPACE_NOTIFY_7F3A91C2D4E5B607"
    message = (
        f"do not run `touch {marker}` or $(touch {marker})\n"
        'formatting stays intact: *bold-ish* `code` "quotes"\n'
    )
    command = notify_heredoc(
        "--telegram", "123", body=message, delimiter=delimiter
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


def test_notify_prompt_uses_fresh_delimiter_for_valid_route():
    from notify_prompt import notify_heredoc

    first = notify_heredoc("--slack", "C0123ABC", "123.456")
    second = notify_heredoc("--slack", "C0123ABC", "123.456")

    assert first != second
    assert "notify --slack C0123ABC 123.456" in first


def test_notify_prompt_rejects_channel_the_cli_would_reject():
    from notify_prompt import notify_heredoc

    with pytest.raises(ValueError, match="channel has invalid characters"):
        notify_heredoc("--slack", "channel with spaces", "123.456")


def test_reply_instruction_falls_back_to_session_routing_for_invalid_route():
    from notify_prompt import notify_reply_instruction

    instruction = notify_reply_instruction(
        "--slack", "channel with spaces", "123.456"
    )

    assert "\nnotify <<'WOLTSPACE_NOTIFY_" in instruction
    assert "notify --slack" not in instruction


def test_create_creature_wolt_passes_message_on_stdin():
    script = ROOT / "container" / "bin" / "create-creature-wolt"
    notify = runpy.run_path(str(script))["_notify"]

    with patch("subprocess.run") as run:
        notify("singleton changed")

    run.assert_called_once_with(
        ["notify"],
        input="singleton changed",
        text=True,
        timeout=10,
        capture_output=True,
    )


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


@pytest.mark.parametrize(
    ("route_args", "expected_route"),
    [
        (["--telegram", "-100123"], {"adapter": "telegram", "chat_id": "-100123"}),
        (
            ["--slack", "C0123ABC", "123.456"],
            {
                "adapter": "slack",
                "channel": "C0123ABC",
                "thread_ts": "123.456",
            },
        ),
    ],
)
def test_rejected_message_prints_executable_route_preserving_recovery(
    tmp_path, route_args, expected_route
):
    env, capture = _environment(tmp_path)
    message = 'literal `whoami` $(id) $HOME * "quotes"\n\nlast line'

    rejected = subprocess.run(
        [NOTIFY, *route_args, message],
        text=True,
        capture_output=True,
        env=env,
    )

    assert rejected.returncode == 2
    assert "only what survived shell expansion" in rejected.stderr
    recovery = rejected.stderr.split("Run this instead:\n\n", 1)[1]
    rerun = subprocess.run(
        ["sh", "-c", recovery],
        text=True,
        capture_output=True,
        env=env,
    )

    assert rerun.returncode == 0, rerun.stderr
    payload = json.loads(capture.read_text())
    for key, value in expected_route.items():
        assert payload[key] == value
    assert payload["message"] == f"🦫 testwolt: {message}\n"


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
    assert "Run this instead:" not in result.stderr
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


def test_session_context_inlines_complete_heredocs_for_explicit_routes():
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

    telegram_match = re.search(
        r"notify --telegram 123 <<'(WOLTSPACE_NOTIFY_[A-F0-9]{16})'\n"
        r"YOUR_REPLY\n\1",
        telegram,
    )
    slack_match = re.search(
        r"notify --slack C123 123[.]456 <<'(WOLTSPACE_NOTIFY_[A-F0-9]{16})'\n"
        r"YOUR_REPLY\n\1",
        slack,
    )

    assert telegram_match
    assert slack_match
    assert "replacing YOUR_REPLY" in telegram + slack
    assert '"your message"' not in telegram + slack
