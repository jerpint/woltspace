"""Which content a gateway host names: an app, a wolt's site, or nothing.

One parser for every name the app gateway answers, so a host can never be two
things at once:

    local    <app>.localhost              app
             <wolt>.sites.localhost       wolt site
    remote   <app>.<apps domain>          app
             site-<wolt>.<apps domain>    wolt site

The `site-` label prefix is reserved for sites, so an app named `site-…` is
never served: remotely that host is the wolt's site (and the gateway refuses
it while such an app exists, so a retired app origin is never reused for a
site), locally it is `reserved` and answers a clear 404. Anything else is
unknown, and the gateway answers 404.

A site host label is a strict DNS label (RFC 1123: lowercase letters, digits
and hyphens, at most 63 characters, no leading or trailing hyphen). The
remote label carries the `site-` prefix inside that limit, so a wolt
directory name may be at most 58 characters and may not contain `_`. A wolt
whose directory name does not fit has no site host; `site_host_label` says
why, so the owner can act on it. Nothing here renames anything.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

SITE_PREFIX = "site-"
LOCAL_SITES_SUFFIX = ".sites.localhost"
DNS_LABEL_MAX = 63
SITE_WOLT_MAX = DNS_LABEL_MAX - len(SITE_PREFIX)

_APP_LABEL_RE = re.compile(r"[a-z][a-z0-9_-]*")
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
# Directories the colony keeps beside wolts; never a wolt.
_NOT_WOLTS = {"apps", "projects"}


@dataclass(frozen=True)
class ContentHost:
    kind: str   # "app", "site" or "reserved"
    name: str   # app name, or the wolt's host label (lowercase)
    local: bool


class UnsupportedWoltName(ValueError):
    """The wolt's directory name cannot be a site host label."""


def host_name(host_header: str) -> str | None:
    try:
        return (urlsplit(f"//{host_header}").hostname or "").lower().rstrip(".")
    except ValueError:
        return None


def is_reserved_app_name(name: str) -> bool:
    return name.lower().startswith(SITE_PREFIX)


def _site_label_ok(label: str) -> bool:
    """A wolt label that fits a DNS label with the `site-` prefix in front."""
    return 1 <= len(label) <= SITE_WOLT_MAX and bool(_DNS_LABEL_RE.fullmatch(label))


def site_host_label(wolt_dir_name: str) -> str:
    """The host label of a wolt directory, or why it has none.

    Lowercasing is DNS normalization, not a rename: `Astre` serves as
    `astre`. Anything a DNS label cannot carry is refused with the reason.
    """
    label = wolt_dir_name.lower()
    if not label:
        raise UnsupportedWoltName("empty wolt name")
    if len(label) > SITE_WOLT_MAX:
        raise UnsupportedWoltName(
            f"wolt name {wolt_dir_name!r} is {len(label)} characters; a site host "
            f"label holds at most {SITE_WOLT_MAX} after the '{SITE_PREFIX}' prefix"
        )
    if not _DNS_LABEL_RE.fullmatch(label):
        raise UnsupportedWoltName(
            f"wolt name {wolt_dir_name!r} is not a DNS label: a site host uses "
            "letters, digits and hyphens only, not starting or ending with a hyphen"
        )
    return label


def parse_content_host(host_header: str, apps_domain: str | None) -> ContentHost | None:
    host = host_name(host_header)
    if not host:
        return None
    if host.endswith(LOCAL_SITES_SUFFIX):
        label = host.removesuffix(LOCAL_SITES_SUFFIX)
        return ContentHost("site", label, True) if _site_label_ok(label) else None
    if host.endswith(".localhost"):
        return _app_or_site(host.removesuffix(".localhost"), local=True)
    if apps_domain and host.endswith(f".{apps_domain}"):
        return _app_or_site(host.removesuffix(f".{apps_domain}"), local=False)
    return None


def _app_or_site(label: str, *, local: bool) -> ContentHost | None:
    if not label or "." in label or len(label) > DNS_LABEL_MAX:
        return None
    if label.startswith(SITE_PREFIX):
        wolt = label.removeprefix(SITE_PREFIX)
        if local:
            # Locally sites live under .sites.localhost only; the name is
            # still reserved so an app can never claim it.
            return ContentHost("reserved", label, True)
        return ContentHost("site", wolt, False) if _site_label_ok(wolt) else None
    if not _APP_LABEL_RE.fullmatch(label):
        return None
    return ContentHost("app", label, local)


def find_wolt_dir(wolts_dir: Path, label: str) -> Path | None:
    """The one wolt directory whose host label is `label`, or None.

    A host label is lowercase; directory names need not be. Two directories
    that differ only in case would share a host, so neither gets one.
    """
    try:
        candidates = [
            entry for entry in wolts_dir.iterdir()
            if entry.is_dir() and not entry.name.startswith(".")
            and entry.name not in _NOT_WOLTS and entry.name.lower() == label
        ]
    except OSError:
        return None
    if len(candidates) != 1:
        return None
    try:
        site_host_label(candidates[0].name)
    except UnsupportedWoltName:
        return None
    return candidates[0]


def site_root(wolts_dir: Path, label: str) -> Path | None:
    """The canonical site directory of the wolt with this host label, or None.

    The root is anchored to the wolt: it is the real directory at
    `<wolt>/wolt/site`, nothing else. A site directory that is a symlink is
    refused rather than followed, wherever it points (outside the wolt, at the
    wolt directory, at `memory/`, or at a sibling folder), so the realpath
    children are later checked against is always the intended tree.
    """
    wolt_dir = find_wolt_dir(wolts_dir, label)
    if wolt_dir is None:
        return None
    return anchored_site_root(wolt_dir)


def anchored_site_root(wolt_dir: Path) -> Path | None:
    site = wolt_dir / "wolt" / "site"
    if os.path.islink(site) or not os.path.isdir(site):
        return None
    root = os.path.realpath(site)
    if root != os.path.join(os.path.realpath(wolt_dir / "wolt"), "site"):
        return None
    return Path(root)
