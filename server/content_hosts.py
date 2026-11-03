"""Which app a host names, under each way into the lodge.

An app lives on an origin of its own, so its pages never run with the lodge's
origin. How that origin is spelled depends on the way in, so naming is a list
of schemes rather than one pattern:

    local        <app>.localhost[:port]           an app

Remote ways in (Cloudflare, Tailscale) name apps their own way
(server/ways_in.py); nothing in the Mill or the lodge's router changes when
they do.

Wolt sites stay on the lodge at `/wolt/<name>/site/` for now. The `site-`
prefix stays reserved, so no app takes a name a wolt's site may need later.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_APP_LABEL_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}")
SITE_PREFIX = "site-"


@dataclass(frozen=True)
class AppHost:
    name: str
    way_in: str


@dataclass(frozen=True)
class LocalAppNaming:
    """`<app>.localhost`, on any port: one label, an app's name."""

    way_in: str = "local"
    suffix: str = ".localhost"

    def name(self, host: str, port: int | None) -> str | None:
        if not host.endswith(self.suffix):
            return None
        label = host.removesuffix(self.suffix)
        if label.startswith(SITE_PREFIX):
            return None  # reserved for wolt sites
        return label if _APP_LABEL_RE.fullmatch(label) else None


LOCAL_APPS = LocalAppNaming()


def app_namings() -> tuple:
    """App naming schemes the Mill serves. Remote app hosts still go through
    the lodge's own proxy until a remote way in hands them to the Mill."""
    return (LOCAL_APPS,)


def parse_app_host(host_header: str, namings=None) -> AppHost | None:
    split = split_host(host_header)
    if split is None:
        return None
    host, port = split
    for naming in (app_namings() if namings is None else namings):
        name = naming.name(host, port)
        if name is not None:
            return AppHost(name, naming.way_in)
    return None


def split_host(host_header: str) -> tuple[str, int | None] | None:
    try:
        parsed = urlsplit(f"//{host_header.strip()}")
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return None
    if not host or parsed.username is not None or parsed.path:
        return None
    return host, port
