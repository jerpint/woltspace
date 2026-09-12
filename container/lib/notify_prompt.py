"""Build shell-safe heredocs and recovery messages for wolt communication."""

from __future__ import annotations

import re
import secrets
import shlex
from collections.abc import Callable, Sequence
from typing import TextIO


_PREFIX_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_SLACK_CHANNEL_RE = re.compile(r"[A-Za-z0-9_-]+")
_SLACK_THREAD_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TELEGRAM_CHAT_ID_RE = re.compile(r"-?[0-9]+")
_SESSION_TARGET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
_REPLY_PLACEHOLDER = "YOUR_REPLY"

RouteValidator = Callable[[tuple[str, ...]], None]
_ROUTE_VALIDATORS: dict[tuple[str, ...], RouteValidator] = {}


class MessageInputError(ValueError):
    """A message body was absent from stdin or supplied through argv."""


def register_route_validator(
    command_path: Sequence[str], validator: RouteValidator
) -> None:
    """Register validation for one executable and subcommand path."""
    path = tuple(command_path)
    if not path or any(not part for part in path):
        raise ValueError("message command path must not be empty")
    _ROUTE_VALIDATORS[path] = validator


def validate_argv(argv: Sequence[object]) -> tuple[str, ...]:
    """Normalize and validate an argv before placing it in a heredoc."""
    normalized = tuple(str(arg) for arg in argv)
    if not normalized or not normalized[0]:
        raise ValueError("message command must not be empty")
    matches = (
        (path, validator)
        for path, validator in _ROUTE_VALIDATORS.items()
        if normalized[: len(path)] == path
    )
    match = max(matches, key=lambda item: len(item[0]), default=None)
    if match is not None:
        match[1](normalized)
    return normalized


def _delimiter(prefix: str, body: str, explicit: str | None = None) -> str:
    if _PREFIX_RE.fullmatch(prefix) is None:
        raise ValueError("invalid heredoc delimiter prefix")
    marker_re = re.compile(rf"{re.escape(prefix)}_[A-F0-9]{{16}}")
    if explicit is not None:
        if marker_re.fullmatch(explicit) is None:
            raise ValueError("invalid heredoc delimiter")
        if explicit in body.splitlines():
            raise ValueError("heredoc delimiter collides with message body")
        return explicit
    while True:
        marker = f"{prefix}_{secrets.token_hex(8).upper()}"
        if marker not in body.splitlines():
            return marker


def quoted_heredoc(
    argv: Sequence[object],
    body: str = _REPLY_PLACEHOLDER,
    *,
    prefix: str = "WOLTSPACE_MSG",
    delimiter: str | None = None,
) -> str:
    """Return an executable single-quoted heredoc for ``argv``."""
    normalized = validate_argv(argv)
    body = str(body)
    marker = _delimiter(prefix, body, delimiter)
    marker_separator = "" if body.endswith("\n") else "\n"
    return (
        f"{shlex.join(normalized)} <<'{marker}'\n"
        f"{body}{marker_separator}{marker}"
    )


def reply_instruction(
    argv: Sequence[object], *, prefix: str = "WOLTSPACE_MSG"
) -> str:
    """Tell an agent exactly how to reply without loading a skill."""
    return (
        f"Reply by replacing {_REPLY_PLACEHOLDER} in this exact single-quoted "
        "heredoc, then run it:\n"
        f"{quoted_heredoc(argv, prefix=prefix)}"
    )


def recovery_message(
    argv: Sequence[object], rejected_body: str, *, prefix: str
) -> str:
    """Return a self-healing error for a rejected legacy message argument."""
    normalized = validate_argv(argv)
    return (
        f"{normalized[0]}: message arguments are not supported; "
        "pass message text on stdin\n"
        "Caution: this body is only what survived shell expansion; "
        "check it before sending.\n\n"
        "Run this instead:\n\n"
        f"{quoted_heredoc(normalized, str(rejected_body), prefix=prefix)}\n"
    )


def read_message_input(
    argv: Sequence[object],
    rejected_args: Sequence[object],
    stream: TextIO,
    *,
    prefix: str,
    allow_empty: bool = False,
) -> str:
    """Read a literal body from stdin or reject a legacy argv body safely."""
    normalized = validate_argv(argv)
    rejected = tuple(str(arg) for arg in rejected_args)
    if len(rejected) == 1:
        raise MessageInputError(
            recovery_message(normalized, rejected[0], prefix=prefix)
        )
    if rejected:
        raise MessageInputError(
            f"{normalized[0]}: message arguments are not supported; "
            "pass one message body on stdin\n"
        )
    if stream.isatty():
        if allow_empty:
            return ""
        raise MessageInputError(
            f"{normalized[0]}: message text must be provided on stdin; "
            "use a single-quoted heredoc\n"
        )
    body = stream.read()
    if not body and not allow_empty:
        raise MessageInputError(f"{normalized[0]}: message must not be empty\n")
    return body


def _validate_notify_argv(argv: tuple[str, ...]) -> None:
    route_args = argv[1:]
    if not route_args:
        return
    if route_args[0] == "--slack" and len(route_args) == 3:
        if _SLACK_CHANNEL_RE.fullmatch(route_args[1]) is None:
            raise ValueError("Slack channel has invalid characters")
        if _SLACK_THREAD_RE.fullmatch(route_args[2]) is None:
            raise ValueError("Slack thread timestamp must be numeric")
        return
    if route_args[0] == "--telegram" and len(route_args) == 2:
        if _TELEGRAM_CHAT_ID_RE.fullmatch(route_args[1]) is None:
            raise ValueError("Telegram chat ID must be an integer")
        return
    raise ValueError("invalid notify route arguments")


register_route_validator(("notify",), _validate_notify_argv)


def _validate_session_send_argv(argv: tuple[str, ...]) -> None:
    if len(argv) < 4 or argv[1:3] != ("session", "send"):
        raise ValueError("invalid session send arguments")
    if _SESSION_TARGET_RE.fullmatch(argv[3]) is None:
        raise ValueError("session target has invalid characters")
    remaining = list(argv[4:])
    while remaining:
        option = remaining.pop(0)
        if option == "--json":
            continue
        if option == "--from" and remaining:
            sender = remaining.pop(0)
            if _SESSION_TARGET_RE.fullmatch(sender) is not None:
                continue
        raise ValueError("invalid session send arguments")


register_route_validator(
    ("woltspace", "session", "send"), _validate_session_send_argv
)


def _validate_session_spawn_argv(argv: tuple[str, ...]) -> None:
    if len(argv) < 4 or argv[1:3] != ("session", "spawn"):
        raise ValueError("invalid session spawn arguments")
    if _SESSION_TARGET_RE.fullmatch(argv[3]) is None:
        raise ValueError("wolt name has invalid characters")
    remaining = list(argv[4:])
    while remaining:
        option = remaining.pop(0)
        if option in {"--json", "--auto"}:
            continue
        if option in {"--from", "--workdir"} and remaining:
            value = remaining.pop(0)
            if option == "--workdir" or _SESSION_TARGET_RE.fullmatch(value):
                continue
        raise ValueError("invalid session spawn arguments")


register_route_validator(
    ("woltspace", "session", "spawn"), _validate_session_spawn_argv
)


def notify_heredoc(
    *route_args: object,
    body: str = _REPLY_PLACEHOLDER,
    delimiter: str | None = None,
) -> str:
    """Compatibility wrapper for a validated ``notify`` heredoc."""
    return quoted_heredoc(
        ["notify", *route_args],
        body,
        prefix="WOLTSPACE_NOTIFY",
        delimiter=delimiter,
    )


def notify_reply_instruction(*route_args: object) -> str:
    """Build a notify reply instruction, falling back to session routing."""
    try:
        return reply_instruction(
            ["notify", *route_args], prefix="WOLTSPACE_NOTIFY"
        )
    except ValueError:
        # Bad adapter metadata should not break message routing. The unrouted
        # command can still resolve the destination through the session registry.
        return reply_instruction(["notify"], prefix="WOLTSPACE_NOTIFY")


def session_send_argv(
    target: object, *, from_wolt: object = "", as_json: bool = False
) -> list[str]:
    """Return the canonical argv for an IWCL message."""
    argv = ["woltspace", "session", "send", str(target)]
    if from_wolt:
        argv.extend(["--from", str(from_wolt)])
    if as_json:
        argv.append("--json")
    validate_argv(argv)
    return argv


def session_send_reply_instruction(target: object) -> str:
    """Build an IWCL reply instruction for one exact sender session."""
    return reply_instruction(
        session_send_argv(target), prefix="WOLTSPACE_IWCL"
    )


def session_spawn_argv(
    wolt: object,
    *,
    from_wolt: object = "",
    as_json: bool = False,
    workdir: object = "",
    auto: bool = False,
) -> list[str]:
    """Return the canonical argv for spawning a wolt session."""
    argv = ["woltspace", "session", "spawn", str(wolt)]
    if from_wolt:
        argv.extend(["--from", str(from_wolt)])
    if as_json:
        argv.append("--json")
    if workdir:
        argv.extend(["--workdir", str(workdir)])
    if auto:
        argv.append("--auto")
    validate_argv(argv)
    return argv
