"""Read and validate the connector's existing private, per-key creator binding."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path


class BindingError(ValueError):
    """The saved binding needs operator attention."""


def account_for_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def valid_participant(value: object) -> bool:
    return (isinstance(value, str) and value.startswith("user:") and 5 < len(value) <= 128
            and value.isascii() and all(char.isprintable() and not char.isspace() for char in value))


def binding_path(state: Path, account: str) -> Path:
    return state / f"owner-{account}.json"


def read_creator(state: Path, account: str, *, expected: str | None = None) -> str | None:
    """Return the saved creator, or None when the key has yet to enroll."""
    try:
        fd = os.open(binding_path(state, account), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise BindingError("WhatsApp creator binding is unreadable") from None
    try:
        with os.fdopen(fd, encoding="utf-8") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
                raise ValueError("invalid private binding")
            data = json.load(source)
        if (not isinstance(data, dict) or set(data) != {"account", "participant"}
                or data["account"] != account or not valid_participant(data["participant"])):
            raise ValueError("invalid creator binding")
    except (OSError, ValueError, TypeError, RecursionError):
        raise BindingError("WhatsApp creator binding is invalid; inspect private state") from None
    if expected is not None and data["participant"] != expected:
        raise BindingError(
            "This WhatsApp key is already bound to a different creator. "
            "Re-run setup and use the bound creator or a fresh key."
        )
    return data["participant"]
