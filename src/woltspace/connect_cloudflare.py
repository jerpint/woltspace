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
import io
import json
import os
import tempfile
from dataclasses import dataclass, replace
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
    # new line, end a quoted value, or that dotenv would expand (`$`) or
    # unescape (`\\`) is refused, whichever field it is.
    for name in names:
        value = getattr(connection, name)
        if any(c.isspace() or not c.isprintable() or c in "\"'#$\\" for c in value):
            raise BadCode(f"that connection code has a bad {name.replace('_', ' ')}")
    parts = urlsplit(connection.url)
    # Apps live one level under the lodge's parent: drop the first label of the
    # lodge's name and something of the owner's must be left. The address is a
    # name and nothing else: no login, port, query or fragment.
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (parts.scheme != "https" or (parts.hostname or "").count(".") < 2 or parts.path not in ("", "/")
            or parts.username is not None or parts.password is not None or port is not None
            or parts.query or parts.fragment or "?" in connection.url or "#" in connection.url):
        raise BadCode("that connection code has a bad lodge address")
    connection = replace(connection, url=f"https://{parts.hostname}")
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
    """The env file with these keys set. The file is split with dotenv's own
    parser, so a binding is exactly what dotenv reads (quoted or multi-line
    values, `export`, quoted keys) and nothing inside another value changes.
    dotenv takes the last of a repeated key, so each key is set where it first
    appears and every later binding of it goes."""
    from dotenv.parser import parse_stream
    out: list[str] = []
    done: set[str] = set()
    for binding in parse_stream(io.StringIO(text)):
        key = binding.key
        if key in values and not binding.error:
            if key not in done:
                tail = "\n" if binding.original.string.endswith("\n") else ""
                out.append(f"{key}={values[key]}{tail}")
                done.add(key)
            continue  # a later binding of a key set above
        out.append(binding.original.string)
    body = "".join(out)
    if body and not body.endswith("\n"):
        body += "\n"
    return body + "".join(f"{key}={value}\n" for key, value in values.items() if key not in done)


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
