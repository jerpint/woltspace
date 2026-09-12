"""Build shell-safe, self-contained instructions for replying through notify."""

from __future__ import annotations

import re
import secrets
import shlex


_DELIMITER_RE = re.compile(r"WOLTSPACE_NOTIFY_[A-F0-9]{16}")
_SLACK_CHANNEL_RE = re.compile(r"[A-Za-z0-9_-]+")
_SLACK_THREAD_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_TELEGRAM_CHAT_ID_RE = re.compile(r"-?[0-9]+")
_REPLY_PLACEHOLDER = "YOUR_REPLY"


def _validate_route_args(route_args: tuple[str, ...]) -> None:
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


def notify_heredoc(
    *route_args: object,
    body: str = _REPLY_PLACEHOLDER,
    delimiter: str | None = None,
) -> str:
    """Return an executable quoted heredoc for ``notify``.

    Route arguments are shell-quoted and the delimiter is generated afresh so
    message text cannot accidentally terminate the heredoc. ``body`` exists so
    tests can execute the exact formatter used in prompts with adversarial text.
    """
    route_args = tuple(str(arg) for arg in route_args)
    _validate_route_args(route_args)
    body = str(body)
    if delimiter is not None:
        if _DELIMITER_RE.fullmatch(delimiter) is None:
            raise ValueError("invalid notify heredoc delimiter")
        if delimiter in body.splitlines():
            raise ValueError("notify heredoc delimiter collides with message body")
        marker = delimiter
    else:
        while True:
            marker = f"WOLTSPACE_NOTIFY_{secrets.token_hex(8).upper()}"
            if marker not in body.splitlines():
                break

    command = shlex.join(["notify", *route_args])
    marker_separator = "" if body.endswith("\n") else "\n"
    return f"{command} <<'{marker}'\n{body}{marker_separator}{marker}"


def notify_reply_instruction(*route_args: object) -> str:
    """Tell an agent exactly how to send a reply without loading a skill."""
    try:
        heredoc = notify_heredoc(*route_args)
    except ValueError:
        # Bad adapter metadata should not break message routing. The unrouted
        # command can still resolve the destination through the session registry.
        heredoc = notify_heredoc()
    return (
        f"Reply by replacing {_REPLY_PLACEHOLDER} in this exact single-quoted "
        "heredoc, then run it:\n"
        f"{heredoc}"
    )
