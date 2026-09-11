"""Build shell-safe, self-contained instructions for replying through notify."""

from __future__ import annotations

import re
import secrets
import shlex


_DELIMITER_RE = re.compile(r"WOLTSPACE_NOTIFY_[A-F0-9]{16}")
_REPLY_PLACEHOLDER = "YOUR_REPLY"


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

    command = shlex.join(["notify", *(str(arg) for arg in route_args)])
    marker_separator = "" if body.endswith("\n") else "\n"
    return f"{command} <<'{marker}'\n{body}{marker_separator}{marker}"


def notify_reply_instruction(*route_args: object) -> str:
    """Tell an agent exactly how to send a reply without loading a skill."""
    return (
        f"Reply by replacing {_REPLY_PLACEHOLDER} in this exact single-quoted "
        "heredoc, then run it:\n"
        f"{notify_heredoc(*route_args)}"
    )
