"""Which wolt site a host names, under each way into the lodge.

A wolt's site lives on an origin of its own, so a page a wolt wrote never runs
with the lodge's origin. How that origin is spelled depends on the way in, so
naming is a list of schemes rather than one pattern. Each way in brings its
own:

    local        <wolt>.sites.localhost[:port]

Remote ways in (Cloudflare, Tailscale) add theirs when they start serving
sites; nothing in the Mill or the lodge's router changes when they do.

A site label is a strict DNS label (lowercase letters, digits and hyphens, at
most 63 characters, no leading or trailing hyphen). A wolt whose directory
name doesn't fit has no site host; `site_host_label` says why. Nothing here
renames anything.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

DNS_LABEL_MAX = 63
_DNS_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
# Directories kept beside wolts; never a wolt.
_NOT_WOLTS = {"apps", "projects"}


class UnsupportedWoltName(ValueError):
    """The wolt's directory name can't be a site host label."""


@dataclass(frozen=True)
class SiteHost:
    label: str      # the wolt's host label (lowercase)
    way_in: str     # which naming scheme matched: "local", ...


class SiteNaming(Protocol):
    way_in: str

    def label(self, host: str, port: int | None) -> str | None:
        """The site label this host and port name, or None."""


@dataclass(frozen=True)
class SuffixNaming:
    """`<label><suffix>`, on any port."""

    way_in: str
    suffix: str

    def label(self, host: str, port: int | None) -> str | None:
        if not host.endswith(self.suffix):
            return None
        label = host.removesuffix(self.suffix)
        return label if label_ok(label) else None


LOCAL = SuffixNaming("local", ".sites.localhost")


def site_namings() -> tuple[SiteNaming, ...]:
    """The naming schemes in force. Only the local one serves sites so far."""
    return (LOCAL,)


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


def parse_site_host(host_header: str, namings=None) -> SiteHost | None:
    split = split_host(host_header)
    if split is None:
        return None
    host, port = split
    for naming in (site_namings() if namings is None else namings):
        label = naming.label(host, port)
        if label is not None:
            return SiteHost(label, naming.way_in)
    return None


def label_ok(label: str) -> bool:
    return 1 <= len(label) <= DNS_LABEL_MAX and bool(_DNS_LABEL_RE.fullmatch(label))


def site_host_label(wolt_dir_name: str) -> str:
    """The host label of a wolt directory, or why it has none.

    Lowercasing is DNS normalization, not a rename: `Astre` serves as `astre`.
    """
    label = wolt_dir_name.lower()
    if not label:
        raise UnsupportedWoltName("empty wolt name")
    if len(label) > DNS_LABEL_MAX:
        raise UnsupportedWoltName(
            f"wolt name {wolt_dir_name!r} is {len(label)} characters; a host label "
            f"holds at most {DNS_LABEL_MAX}"
        )
    if not _DNS_LABEL_RE.fullmatch(label):
        raise UnsupportedWoltName(
            f"wolt name {wolt_dir_name!r} is not a DNS label: a site host uses "
            "letters, digits and hyphens only, not starting or ending with a hyphen"
        )
    return label


def find_wolt_dir(wolts_dir: Path, label: str) -> Path | None:
    """The one wolt directory whose host label is `label`, or None.

    Two directories that differ only in case would share a host, so neither
    gets one.
    """
    try:
        candidates = [
            entry for entry in Path(wolts_dir).iterdir()
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


def anchored_site_root(wolt_dir: Path) -> Path | None:
    """The real directory `<wolt>/wolt/site`, or None.

    A site directory that is a symlink is refused rather than followed,
    wherever it points, so the root later paths are checked against is always
    the intended tree.
    """
    site = Path(wolt_dir) / "wolt" / "site"
    if os.path.islink(site) or not os.path.isdir(site):
        return None
    root = os.path.realpath(site)
    if root != os.path.join(os.path.realpath(Path(wolt_dir) / "wolt"), "site"):
        return None
    return Path(root)
