"""Connect a lodge to Cloudflare with one code.

An admin who runs the Cloudflare side hands the owner one connection code; the
owner gives it to a wolt or runs `woltspace connect cloudflare` themselves. This
saves what the code holds. Everything Cloudflare-specific about connecting stays
in this file, the way `server/ways_in.py` keeps the rest of it.

The code holds the tunnel token, so it never goes on a command line, in a log or
in output.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .doctor import _access_settings_problem
from .layout import RuntimeLayout

PREFIX = "woltspace-cloudflare-1."


class BadCode(ValueError):
    """A code that can't be used. The message never repeats any of it."""


@dataclass(frozen=True)
class Connection:
    url: str
    tunnel_token: str
    team_domain: str
    apps_aud: str
    owner_email: str


def decode(code: str) -> Connection:
    code = code.strip()
    if not code.startswith(PREFIX):
        raise BadCode("that isn't a Cloudflare connection code")
    body = code[len(PREFIX):]
    try:
        raw = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (binascii.Error, ValueError) as exc:
        raise BadCode("that connection code is damaged; copy it again") from exc
    names = ("url", "tunnel_token", "team_domain", "apps_aud", "owner_email")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(name), str) and raw[name].strip() for name in names):
        raise BadCode("that connection code is incomplete")
    connection = Connection(*(raw[name].strip() for name in names))
    # Every field may end up in `.env` or woltspace.json: one that could start a
    # new line or end a quoted value is refused, whichever field it is.
    for name in names:
        value = getattr(connection, name)
        if any(c.isspace() or not c.isprintable() or c in "\"'#" for c in value):
            raise BadCode(f"that connection code has a bad {name.replace('_', ' ')}")
    parts = urlsplit(connection.url)
    # Apps live one level under the lodge's parent: drop the first label of the
    # lodge's name and something of the owner's must be left.
    if parts.scheme != "https" or (parts.hostname or "").count(".") < 2 or parts.path not in ("", "/"):
        raise BadCode("that connection code has a bad lodge address")
    problem = _access_settings_problem({
        "team_domain": connection.team_domain, "apps_aud": connection.apps_aud,
        "owner_email": connection.owner_email})
    if problem:
        raise BadCode(f"that connection code is wrong: {problem}")
    return connection


def _write_private(path: Path, text: str, mode: int) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _with_values(text: str, values: dict[str, str]) -> str:
    """The env file with these keys set. Only whole `KEY=` lines change, and
    never a line inside a quoted value that spans several lines. dotenv takes
    the last of a repeated key, so each key is set where it first appears and
    every later assignment of it goes."""
    lines = text.splitlines()
    out: list[str] = []
    done: set[str] = set()
    quote = ""  # the quote a value spanning lines is still waiting for
    dropping = False  # that value belongs to a key set here: its lines go too
    for line in lines:
        if quote:
            if not dropping:
                out.append(line)
            if _closes(line, quote):
                quote = ""
            continue
        key, eq, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.lstrip()
        if eq and value[:1] in ("'", '"') and not _closes(value[1:], value[0]):
            quote = value[0]
        dropping = bool(eq and key in values)
        if dropping:
            if key not in done:
                out.append(f"{key}={values[key]}")
                done.add(key)
            continue  # a later assignment of a key set above
        out.append(line)
    out += [f"{key}={value}" for key, value in values.items() if key not in done]
    return "\n".join(out) + "\n"


def _closes(text: str, quote: str) -> bool:
    """Whether `text` holds the closing `quote`. dotenv lets a backslash
    escape a double quote, never a single one."""
    escaped = False
    for char in text:
        if escaped:
            escaped = False
        elif char == "\\" and quote == '"':
            escaped = True
        elif char == quote:
            return True
    return False


def _known_urls(layout: RuntimeLayout) -> list[str]:
    """Every address the lodge has now, in the order startup reads them: what
    the process was given, then `.env`, then the tunnel it's running."""
    from dotenv import dotenv_values
    from .lifecycle import read_tunnel_url
    env_file = layout.wolts_dir / ".env"
    found = [os.environ.get("CLOUDFLARE_TUNNEL_URL"),
             dotenv_values(env_file).get("CLOUDFLARE_TUNNEL_URL") if env_file.is_file() else None,
             read_tunnel_url(layout)]
    return [url.strip() for url in found if url and url.strip()]


def current_url(layout: RuntimeLayout) -> str:
    return next(iter(_known_urls(layout)), "")


def apply(layout: RuntimeLayout, connection: Connection) -> dict:
    """Save the connection. Running it twice changes nothing the second time.

    Who may come in is saved before the address that lets them: if the second
    write fails, the lodge has Access settings and no tunnel, never a tunnel
    without them, and running it again finishes the job."""
    settings = layout.wolts_dir / "woltspace.json"
    with _settings_lock(layout, settings):
        _save_access(settings, connection)

    env_file = layout.wolts_dir / ".env"
    text = env_file.read_text() if env_file.is_file() else ""
    _write_private(env_file, _with_values(text, {
        "WOLTSPACE_PUBLIC_TUNNEL": "true",
        "CLOUDFLARE_TUNNEL_URL": connection.url,
        "CLOUDFLARE_TUNNEL_TOKEN": connection.tunnel_token,
    }), 0o600)
    return {"url": connection.url, "owner_email": connection.owner_email}


def _settings_lock(layout: RuntimeLayout, settings: Path):
    """The lock every writer of woltspace.json holds from its read to its
    rename (container/lib/settings_files.py), so a save made meanwhile by the
    lodge or a bot isn't undone by this one."""
    from .doctor import _graft_runtime_lib
    _graft_runtime_lib(layout)
    from settings_files import settings_lock
    return settings_lock(settings)


def _save_access(settings: Path, connection: Connection) -> None:
    config = json.loads(settings.read_text()) if settings.is_file() else {}
    ways_in = config.get("ways_in") if isinstance(config.get("ways_in"), dict) else {}
    # A setting the code doesn't carry (a separate `lodge_aud`) stays while the
    # Access team is the same one; another team's settings are never kept.
    before = ways_in.get("cloudflare") if isinstance(ways_in.get("cloudflare"), dict) else {}
    kept = before if before.get("team_domain") == connection.team_domain else {}
    ways_in["cloudflare"] = {
        **kept,
        "team_domain": connection.team_domain,
        "apps_aud": connection.apps_aud,
        "owner_email": connection.owner_email,
    }
    config["ways_in"] = ways_in
    mode = settings.stat().st_mode & 0o777 if settings.is_file() else 0o644
    _write_private(settings, json.dumps(config, indent=2) + "\n", mode)
