"""Ways into the lodge from elsewhere: Cloudflare today, anyone's tomorrow.

Locally, the lodge knows its owner: a paired browser or a loopback caller.
From elsewhere, something in between (a tunnel, a VPN, a proxy) has to vouch
for the visitor. Each such way in answers four questions, and nothing else in
the lodge or the Mill depends on which way a request came:

1. `lodge_hosts()`: the names this way gives the lodge itself, whose requests
   go to the lodge's routes under lodge auth as usual.
2. `content_host(host)`: whether a Host names a wolt's site or an app through
   this way, as a `PublicHost`. Each site and app must get an authority (host
   and port) of its own, so each keeps its own origin.
3. `owner(headers, scope)`: whether this request is the owner, proven this
   way's own way. Return a `Visitor`, or None for anyone else. Only "owner"
   exists today; a shared app lets anyone else in as "public" whatever the
   way in (see `container/lib/app_sharing.py`).
4. `credential_headers`: the request headers this way uses to say who the
   visitor is. They're stripped before anything reaches an app, on every
   request and whichever way it came, so nobody can forge them to an app.

`public_base(lodge_host, kind, name)` says where a browser that reached the
lodge at `lodge_host` through this way opens a site or app (for `/sites/open`
and `/apps/open`), or None when `lodge_host` isn't this way's.

Ways in are the built-ins below plus any installed package that declares an
entry point in the `woltspace.ways_in` group, whose object is a way in (or a
callable returning one). Installing it is the opt-in. `docs/ways-in.md`
explains how to write one.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PublicHost:
    kind: str        # "site" or "app"
    name: str        # the wolt, for a site; the app's name, for an app
    authority: str   # the public host[:port], as the browser sends it


@dataclass(frozen=True)
class Visitor:
    identity: str             # "owner"
    expires_at: float | None  # when this proof ends (open sockets close then), or None


class WayIn(Protocol):
    name: str
    credential_headers: frozenset[str]

    def lodge_hosts(self) -> set[str]: ...

    def content_host(self, host: str) -> PublicHost | None: ...

    async def owner(self, headers: dict[str, str], scope: dict) -> Visitor | None: ...

    def public_base(self, lodge_host: str, kind: str, name: str) -> str | None: ...


def _split(host_header: str):
    from .content_hosts import split_host
    return split_host(host_header)


class CloudflareAccess:
    """Cloudflare: the lodge at the tunnel's host, sites at
    `site-<wolt>.<domain>` and apps at `<app>.<domain>`, and Cloudflare
    Access's token, checked for the apps' audience and the owner's email."""

    name = "cloudflare"
    credential_headers = frozenset({
        "cf-access-jwt-assertion", "cf-access-token", "cf-access-client-id",
        "cf-access-client-secret", "cf-access-authenticated-user-email",
    })

    @staticmethod
    def _tunnel():
        from . import tunnel
        return tunnel

    def lodge_hosts(self) -> set[str]:
        from urllib.parse import urlsplit
        tunnel = self._tunnel()
        hosts = {tunnel.get_tunnel_hostname().lower().rstrip(".")}
        # Quick tunnels have no wildcard domain; their exact runtime URL is
        # still a name of the lodge.
        try:
            hosts.add((urlsplit(tunnel.get_tunnel_url()).hostname or "").lower().rstrip("."))
        except ValueError:
            pass
        return {h for h in hosts if h}

    def content_host(self, host_header: str) -> PublicHost | None:
        from .content_hosts import parse_app_host, parse_site_host
        split = _split(host_header)
        tunnel = self._tunnel()
        domain = tunnel.get_tunnel_domain().lower().rstrip(".")
        if split is None or not domain:
            return None
        host, _port = split
        if host == tunnel.get_tunnel_hostname().lower().rstrip(".") or not host.endswith(f".{domain}"):
            return None
        label = host.removesuffix(f".{domain}")
        if label.startswith("site-"):
            wolt = label.removeprefix("site-")
            if parse_site_host(f"{wolt}.sites.localhost") is None:
                return None
            return PublicHost("site", wolt, host)
        if parse_app_host(f"{label}.localhost") is None:
            return None
        return PublicHost("app", label, host)

    async def owner(self, headers: dict[str, str], scope: dict) -> Visitor | None:
        """Cloudflare Access let the visitor in; the lodge checks the token it
        attached (signature, audience, issuer, expiry) against the apps'
        audience and the owner's email. Without Access settings, nobody."""
        from pathlib import Path

        from . import access
        from .config import WOLTS_DIR
        try:
            settings = access.load_access_settings(Path(WOLTS_DIR))
        except RuntimeError:
            return None
        token = headers.get("cf-access-jwt-assertion", "").strip()
        if settings is None or not token:
            return None
        try:
            claims = await access.access_token_verifier.verify(token, settings, settings.apps_aud)
        except Exception:
            return None
        if str(claims.get("email") or "").strip().lower() != settings.owner_email:
            return None
        exp = claims.get("exp")
        return Visitor("owner", float(exp) if isinstance(exp, (int, float)) else time.time())

    def public_base(self, lodge_host: str, kind: str, name: str) -> str | None:
        tunnel = self._tunnel()
        domain = tunnel.get_tunnel_domain().lower().rstrip(".")
        if lodge_host and domain and lodge_host == tunnel.get_tunnel_hostname().lower().rstrip("."):
            return f"https://{'site-' + name if kind == 'site' else name}.{domain}"
        return None


_BUILTIN: list = [CloudflareAccess()]
_registered: list = []
_installed: list | None = None


def register(way) -> None:
    """Add a way in from code (tests, or a lodge that wires its own)."""
    if not _valid(way):
        raise TypeError("not a way in (see docs/ways-in.md)")
    _registered.append(way)


_METHODS = ("lodge_hosts", "content_host", "owner", "public_base")


def _valid(way) -> bool:
    """A way in object, not a class: a str name, a set of header names, and
    the four methods, bound."""
    if isinstance(way, type) or not isinstance(getattr(way, "name", None), str) or not way.name:
        return False
    headers = getattr(way, "credential_headers", None)
    if not isinstance(headers, (set, frozenset)) or not all(isinstance(h, str) for h in headers):
        return False
    return all(callable(getattr(way, method, None)) for method in _METHODS)


def _from_entry_points() -> list:
    """Installed ways in: an entry point names a way in, or a class or
    function that makes one. Anything else is skipped and logged."""
    from importlib.metadata import entry_points
    found = []
    for entry in entry_points(group="woltspace.ways_in"):
        try:
            target = entry.load()
            way = target() if isinstance(target, type) or not _valid(target) else target
            if not _valid(way):
                raise TypeError("not a way in (see docs/ways-in.md)")
            if any(way.name == other.name for other in (*_BUILTIN, *found)):
                raise ValueError(f"a way in named {way.name!r} is already loaded")
            found.append(way)
        except Exception as exc:  # a broken plugin must never keep the lodge from starting
            print(f"[ways in] {entry.name} not loaded: {type(exc).__name__}: {exc}")
    return found


def all_ways() -> list:
    global _installed
    if _installed is None:
        _installed = _from_entry_points()
    return [*_BUILTIN, *_installed, *_registered]


def named(name: str):
    return next((way for way in all_ways() if way.name == name), None)


def _ask(way, method: str, *args, default=None):
    """A way in's answer; a plugin that raises answers nothing."""
    try:
        return getattr(way, method)(*args)
    except Exception as exc:
        print(f"[ways in] {getattr(way, 'name', '?')}.{method} failed: {type(exc).__name__}: {exc}")
        return default


def lodge_hosts() -> set[str]:
    hosts = set()
    for way in all_ways():
        answer = _ask(way, "lodge_hosts", default=set())
        hosts |= {h for h in (answer or ()) if isinstance(h, str) and h}
    return hosts


def content_host(host_header: str):
    """(way, PublicHost) for the first way in that claims this host, or (None, None)."""
    for way in all_ways():
        public = _ask(way, "content_host", host_header)
        if isinstance(public, PublicHost) and public.kind in ("site", "app"):
            return way, public
    return None, None


def credential_headers() -> frozenset[str]:
    return frozenset(h.lower() for way in all_ways() for h in way.credential_headers)


def public_base(lodge_host: str, kind: str, name: str) -> str:
    for way in all_ways():
        base = _ask(way, "public_base", lodge_host, kind, name)
        if isinstance(base, str) and base.startswith("https://"):
            return base
    return ""
