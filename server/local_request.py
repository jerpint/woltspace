"""Shared owner-local trust boundary for the lodge and app gateway."""

from __future__ import annotations

import ipaddress
import os


FORWARDING_HEADERS = {
    "cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host",
    "forwarded", "cf-access-jwt-assertion",
}


def _headers(scope) -> set[str]:
    return {
        key.decode("latin-1").lower()
        for key, _value in scope.get("headers", [])
    }


def owner_local_request(
    scope: dict, hostname: str, *, allow_localhost_subdomain: bool = False,
) -> bool:
    """Recognize a request reachable only from the owning machine.

    External/container mode deliberately cannot check the ASGI peer: Docker's
    bridge address varies by host. Its safety depends on the launcher publishing
    both lodge and gateway ports on host 127.0.0.1 only. Forwarding, Cloudflare,
    and Access headers always disqualify the local exemption.
    """
    hostname = hostname.lower().rstrip(".")
    loopback_host = hostname in {"localhost", "127.0.0.1", "::1"}
    if allow_localhost_subdomain:
        loopback_host = loopback_host or hostname.endswith(".localhost")
    if not loopback_host or FORWARDING_HEADERS.intersection(_headers(scope)):
        return False
    if os.environ.get("WOLTSPACE_ISOLATION") == "external":
        return True
    peer = scope.get("client")
    try:
        return bool(peer and ipaddress.ip_address(peer[0]).is_loopback)
    except ValueError:
        return False
