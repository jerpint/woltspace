import json
import os
from pathlib import Path
import re
import runpy
import shlex
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


def test_transport_failure_reports_curl_diagnosis_without_secrets(tmp_path):
    env, capture = _environment(tmp_path)
    curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    message = "harmless but private delivery text"
    token = "xoxb-test-secret-token"
    curl.write_text(
        f"""#!/bin/bash
printf '%s\\n' 'curl: (7) could not connect; {message}; {token}' >&2
exit 7
"""
    )
    env["SLACK_BOT_TOKEN"] = token

    result = subprocess.run(
        [NOTIFY, "--slack", "C0123ABC", "123.456"],
        input=message,
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 1
    assert "transport failed (curl exit 7)" in result.stderr
    assert "could not connect" in result.stderr
    assert "<redacted>" in result.stderr
    assert message not in result.stderr
    assert token not in result.stderr
    assert not capture.exists()


def test_empty_successful_transport_response_is_diagnosed(tmp_path):
    env, _ = _environment(tmp_path)
    curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    curl.write_text("#!/bin/bash\nexit 0\n")

    result = subprocess.run(
        [NOTIFY, "--telegram", "123"],
        input="delivery text",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 1
    assert "invalid or empty API response" in result.stderr


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


# --- notify --file ----------------------------------------------------------

def _file_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    env, capture = _environment(tmp_path)
    env["WOLTSPACE_WOLTS_DIR"] = str(tmp_path / "wolts")
    # A lodge that knows about files answers with what it sent.
    curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    curl.write_text(
        """#!/bin/bash
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-d" ]; then
    printf '%s' "$2" > "$NOTIFY_CAPTURE"
    shift 2
  else
    shift
  fi
done
printf '%s\\n' '{"ok":true,"adapter":"telegram","attachments":[{"name":"a.html","content_type":"text/html","size":11}]}'
"""
    )
    return env, capture, tmp_path / "wolts" / ".space" / "outbox"


def _entries(box: Path) -> list[Path]:
    return sorted(box.iterdir()) if box.exists() else []


def test_file_flag_stages_the_file_and_names_it_in_the_payload(tmp_path):
    env, capture, box = _file_environment(tmp_path)
    source = tmp_path / "weekly update.html"
    source.write_bytes(b"<h1>hi</h1>")

    result = subprocess.run(
        [NOTIFY, "--file", source],
        input="Weekly update is ready.\n",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(capture.read_text())
    (attachment,) = payload["attachments"]
    assert re.fullmatch(r"[0-9a-f]{32}", attachment["id"])
    assert attachment == {"id": attachment["id"], "name": "weekly update.html"}
    assert (box / attachment["id"]).read_bytes() == b"<h1>hi</h1>"
    assert payload["message"] == "🦫 testwolt: Weekly update is ready.\n"
    assert result.stdout.rstrip("\n").endswith("[file: weekly update.html]")


def test_file_path_may_be_relative_to_the_current_directory(tmp_path):
    env, capture, box = _file_environment(tmp_path)
    (tmp_path / "sparks").mkdir()
    (tmp_path / "sparks" / "weekly-update.html").write_bytes(b"<h1>hi</h1>")

    result = subprocess.run(
        [NOTIFY, "--file", "sparks/weekly-update.html"],
        input="ready\n",
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr
    (attachment,) = json.loads(capture.read_text())["attachments"]
    assert attachment["name"] == "weekly-update.html"
    assert (box / attachment["id"]).read_bytes() == b"<h1>hi</h1>"


@pytest.mark.parametrize("file_first", [True, False])
@pytest.mark.parametrize(
    "route_args",
    [["--telegram", "-100123"], ["--slack", "C0123", "1711234567.890123"]],
)
def test_file_flag_works_before_and_after_route_flags(tmp_path, route_args, file_first):
    env, capture, _ = _file_environment(tmp_path)
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")
    plain = subprocess.run(
        [NOTIFY, *route_args], input="hello\n", text=True, capture_output=True, env=env,
    )
    assert plain.returncode == 0, plain.stderr
    without_file = json.loads(capture.read_text())

    file_args = ["--file", str(source)]
    result = subprocess.run(
        [NOTIFY, *(file_args + route_args if file_first else route_args + file_args)],
        input="hello\n",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(capture.read_text())
    attachments = payload.pop("attachments")
    assert payload == without_file
    assert [attachment["name"] for attachment in attachments] == ["a.html"]


def test_file_alone_needs_no_message(tmp_path):
    env, capture, _ = _file_environment(tmp_path)
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")

    result = subprocess.run(
        [NOTIFY, "--file", source], input="", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text())["message"] == "🦫 testwolt: "


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", "file not found: {path}"),
        ("directory", "not a regular file: {path}"),
        ("empty", "file is empty: {path}"),
        ("credential", "refusing to send a credential file: .env"),
    ],
)
def test_file_problems_exit_2_without_staging_or_sending(tmp_path, kind, expected):
    env, capture, box = _file_environment(tmp_path)
    path = tmp_path / (".env" if kind == "credential" else "report.pdf")
    if kind == "directory":
        path.mkdir()
    elif kind == "empty":
        path.write_bytes(b"")
    elif kind == "credential":
        path.write_text("TOKEN=1")

    result = subprocess.run(
        [NOTIFY, "--file", path], input="hello\n", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 2
    assert result.stderr == f"notify: {expected.format(path=path)}\n"
    assert not capture.exists()
    assert _entries(box) == []


def test_a_bad_file_is_refused_before_stdin_is_read(tmp_path):
    env, capture, _ = _file_environment(tmp_path)

    with subprocess.Popen(
        [NOTIFY, "--file", tmp_path / "nope.pdf"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    ) as process:
        # Nothing is written and stdin stays open: a notify that read it first
        # would still be waiting.
        assert process.wait(timeout=20) == 2
        assert "file not found" in process.stderr.read()
    assert not capture.exists()


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--file"], "notify: --file requires PATH"),
        (["--telegram", "123", "--file"], "notify: --file requires PATH"),
        (["--file", "a.html", "--file", "b.html"], "notify: only one --file per notify"),
        (["--file", "a.html", "--slack", "C0123", "1.2", "--file", "b.html"],
         "notify: only one --file per notify"),
    ],
)
def test_second_file_flag_and_missing_value_are_usage_errors(tmp_path, argv, expected):
    env, capture, box = _file_environment(tmp_path)
    for name in ("a.html", "b.html"):
        (tmp_path / name).write_bytes(b"<p>x</p>")

    result = subprocess.run(
        [NOTIFY, *argv], input="hello\n", text=True, capture_output=True, env=env,
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[0] == expected
    assert "Usage:" in result.stderr
    assert not capture.exists()
    assert _entries(box) == []


@pytest.mark.parametrize(
    ("curl_tail", "expected"),
    [
        ("printf '%s\\n' '{\"ok\":false,\"error\":\"boom\"}'", "notify: failed: boom"),
        ("echo 'curl: (7) could not connect' >&2\nexit 7", "transport failed (curl exit 7)"),
        ("echo 'not json'", "invalid or empty API response"),
    ],
)
def test_failed_request_removes_the_staged_entry(tmp_path, curl_tail, expected):
    env, _, box = _file_environment(tmp_path)
    seen = tmp_path / "entries-at-request-time"
    curl = Path(env["PATH"].split(":", 1)[0]) / "curl"
    curl.write_text(f'#!/bin/bash\nls "{box}" | wc -l > "{seen}"\n{curl_tail}\n')
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")

    result = subprocess.run(
        [NOTIFY, "--file", source], input="hello\n", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 1
    assert expected in result.stderr
    assert seen.read_text().strip() == "1"      # it was staged when the request went out
    assert _entries(box) == []


def test_without_file_the_payload_has_no_attachments_key(tmp_path):
    env, capture, box = _file_environment(tmp_path)

    result = subprocess.run(
        [NOTIFY], input="hello\n", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(capture.read_text()) == {
        "session": "", "message": "🦫 testwolt: hello\n",
    }
    assert result.stdout == "[notify] → telegram: hello\n\n"
    assert not box.exists()


def test_positional_message_is_still_rejected_with_file_flag(tmp_path):
    env, capture, box = _file_environment(tmp_path)
    source = tmp_path / "weekly update.html"
    source.write_bytes(b"<h1>hi</h1>")

    with_file = subprocess.run(
        [NOTIFY, "--file", source, "hello"], text=True, capture_output=True, env=env,
    )
    without_file = subprocess.run(
        [NOTIFY, "hello"], text=True, capture_output=True, env=env,
    )

    assert with_file.returncode == 2
    assert "message arguments are not supported" in with_file.stderr
    assert not capture.exists()
    assert _entries(box) == []
    # The recovery text is the existing one, apart from its random delimiter.
    delimiter = re.compile(r"WOLTSPACE_NOTIFY_[A-F0-9]{16}")
    recovery, note = with_file.stderr.rsplit("\n", 2)[0] + "\n", with_file.stderr.splitlines()[-1]
    assert delimiter.sub("D", recovery) == delimiter.sub("D", without_file.stderr)
    # That heredoc cannot carry the flag, so one more line says how to put it back.
    assert note.startswith("# notify: ")
    assert shlex.split(note.split(" add: ", 1)[1]) == ["--file", str(source)]


def test_recovery_text_with_file_flag_is_still_executable(tmp_path):
    env, capture, _ = _file_environment(tmp_path)
    source = tmp_path / "it's $(touch pwned) `id`.html"
    source.write_bytes(b"<h1>hi</h1>")

    rejected = subprocess.run(
        [NOTIFY, "--file", source, "hello"], text=True, capture_output=True, env=env,
        cwd=tmp_path,
    )
    rerun = subprocess.run(
        ["sh", "-c", rejected.stderr.split("Run this instead:\n\n", 1)[1]],
        text=True,
        capture_output=True,
        env=env,
        cwd=tmp_path,
    )

    assert rerun.returncode == 0, rerun.stderr
    assert json.loads(capture.read_text())["message"] == "🦫 testwolt: hello\n"
    assert not (tmp_path / "pwned").exists()


def test_several_positional_arguments_get_no_file_note(tmp_path):
    env, capture, _ = _file_environment(tmp_path)
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")

    result = subprocess.run(
        [NOTIFY, "--file", source, "one", "two"], text=True, capture_output=True, env=env,
    )

    assert result.returncode == 2
    assert "Run this instead:" not in result.stderr
    assert "--file" not in result.stderr       # there is no command to add it to
    assert not capture.exists()


def test_unusable_outbox_is_a_clean_error_and_nothing_is_sent(tmp_path):
    env, capture, _ = _file_environment(tmp_path)
    (tmp_path / "wolts").mkdir()
    (tmp_path / "wolts" / ".space").write_text("not a directory")
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")

    result = subprocess.run(
        [NOTIFY, "--file", source], input="hello\n", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 1
    assert result.stderr.startswith("notify: could not stage the file: ")
    assert "Traceback" not in result.stderr
    assert not capture.exists()


def test_file_flag_after_a_double_dash_is_message_text_as_before(tmp_path):
    env, capture, box = _file_environment(tmp_path)

    result = subprocess.run(
        [NOTIFY, "--", "--file"], text=True, capture_output=True, env=env,
    )

    assert result.returncode == 2
    assert "message arguments are not supported" in result.stderr
    assert not capture.exists()
    assert _entries(box) == []


def test_help_names_the_file_flag(tmp_path):
    env, capture, _ = _file_environment(tmp_path)

    result = subprocess.run(
        [NOTIFY, "--help"], text=True, capture_output=True, env=env,
    )

    assert result.returncode == 0
    assert "[--file PATH]" in result.stdout
    assert "--file sends one file with the message" in result.stdout
    assert not capture.exists()


def test_ok_reply_without_attachments_means_the_file_was_not_sent(tmp_path):
    # A lodge older than this command ignores the field: it sends the text and
    # answers ok. The plain fake curl is that lodge.
    env, capture = _environment(tmp_path)
    env["WOLTSPACE_WOLTS_DIR"] = str(tmp_path / "wolts")
    box = tmp_path / "wolts" / ".space" / "outbox"
    source = tmp_path / "a.html"
    source.write_bytes(b"<p>a</p>")

    result = subprocess.run(
        [NOTIFY, "--file", source], input="hello\n", text=True, capture_output=True, env=env,
    )

    assert result.returncode == 1
    assert result.stderr == (
        "notify: the lodge sent the text but not the file; "
        "restart the lodge so it matches this command\n"
    )
    assert result.stdout == ""
    assert json.loads(capture.read_text())["attachments"][0]["name"] == "a.html"
    assert _entries(box) == []


@pytest.mark.parametrize("file_first", [True, False])
def test_file_flag_accepts_the_equals_form(tmp_path, file_first):
    env, capture, box = _file_environment(tmp_path)
    source = tmp_path / "a=b report.html"
    source.write_bytes(b"<p>a</p>")
    route_args, file_args = ["--telegram", "-100123"], [f"--file={source}"]

    result = subprocess.run(
        [NOTIFY, *(file_args + route_args if file_first else route_args + file_args)],
        input="hello\n",
        text=True,
        capture_output=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(capture.read_text())
    (attachment,) = payload["attachments"]
    assert attachment["name"] == "a=b report.html"
    assert (box / attachment["id"]).read_bytes() == b"<p>a</p>"
    assert (payload["adapter"], payload["chat_id"]) == ("telegram", "-100123")
    assert payload["message"] == "🦫 testwolt: hello\n"    # the flag is never message text


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--file="], "notify: --file requires PATH"),
        (["--telegram", "123", "--file="], "notify: --file requires PATH"),
        (["--file=a.html", "--file", "b.html"], "notify: only one --file per notify"),
        (["--file", "a.html", "--file=b.html"], "notify: only one --file per notify"),
    ],
)
def test_equals_form_has_the_same_usage_errors(tmp_path, argv, expected):
    env, capture, box = _file_environment(tmp_path)
    for name in ("a.html", "b.html"):
        (tmp_path / name).write_bytes(b"<p>x</p>")

    result = subprocess.run(
        [NOTIFY, *argv], input="hello\n", text=True, capture_output=True, env=env,
        cwd=tmp_path,
    )

    assert result.returncode == 2
    assert result.stderr.splitlines()[0] == expected
    assert not capture.exists()
    assert _entries(box) == []
