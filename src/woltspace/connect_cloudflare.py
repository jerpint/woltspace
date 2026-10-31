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
    parts = urlsplit(connection.url)
    # Apps live one level under the lodge's parent: drop the first label of the
    # lodge's name and something of the owner's must be left.
    if parts.scheme != "https" or (parts.hostname or "").count(".") < 2 or parts.path not in ("", "/"):
        raise BadCode("that connection code has a bad lodge address")
    if any(c.isspace() or c in "\"'#" for c in connection.tunnel_token):
        raise BadCode("that connection code has a bad tunnel token")
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
    """The env file with these keys set. Only whole `KEY=` lines change: other
    values may span several lines, and this never parses or rewrites them."""
    lines = text.splitlines()
    missing = dict(values)
    for index, line in enumerate(lines):
        key = line.split("=", 1)[0].strip()
        if "=" in line and key in missing:
            lines[index] = f"{key}={missing.pop(key)}"
    lines += [f"{key}={value}" for key, value in missing.items()]
    return "\n".join(lines) + "\n"


def current_url(layout: RuntimeLayout) -> str:
    env_file = layout.wolts_dir / ".env"
    if not env_file.is_file():
        return ""
    for line in env_file.read_text().splitlines():
        if line.startswith("CLOUDFLARE_TUNNEL_URL="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def _reserved_app_names(layout: RuntimeLayout) -> list[str]:
    apps = layout.wolts_dir / "apps"
    return sorted(p.name for p in apps.glob("site-*")) if apps.is_dir() else []


def apply(layout: RuntimeLayout, connection: Connection) -> dict:
    """Save the connection. Running it twice changes nothing the second time."""
    env_file = layout.wolts_dir / ".env"
    text = env_file.read_text() if env_file.is_file() else ""
    _write_private(env_file, _with_values(text, {
        "WOLTSPACE_PUBLIC_TUNNEL": "true",
        "CLOUDFLARE_TUNNEL_URL": connection.url,
        "CLOUDFLARE_TUNNEL_TOKEN": connection.tunnel_token,
    }), 0o600)

    settings = layout.wolts_dir / "woltspace.json"
    config = json.loads(settings.read_text()) if settings.is_file() else {}
    ways_in = config.get("ways_in") if isinstance(config.get("ways_in"), dict) else {}
    ways_in["cloudflare"] = {
        "team_domain": connection.team_domain,
        "apps_aud": connection.apps_aud,
        "owner_email": connection.owner_email,
    }
    config["ways_in"] = ways_in
    # This address is new to the lodge, so no app of its ever had a `site-` name
    # here. An app holding such a name now keeps the matching site off anyway.
    reserved = _reserved_app_names(layout)
    if not reserved:
        mill = config.get("mill") if isinstance(config.get("mill"), dict) else {}
        mill["remote_sites"] = True
        config["mill"] = mill
    mode = settings.stat().st_mode & 0o777 if settings.is_file() else 0o644
    _write_private(settings, json.dumps(config, indent=2) + "\n", mode)
    return {"url": connection.url, "owner_email": connection.owner_email, "remote_sites_held_by": reserved}
