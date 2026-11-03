"""The one rule for which app names an address can hold.

An app's name is a host label in every way in: `<app>.localhost` here,
`<app>.<domain>` through a tunnel, and the share host built from it. So the
Mill, the ways in and sharing all ask this one function, and an app that can
be shared can also be opened.

A name is a DNS label: lowercase letters, digits and dashes, at most 63
characters, no dash at either end. Names starting with `site-` are kept for
wolt sites.
"""

from __future__ import annotations

import re

SITE_PREFIX = "site-"
_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def app_name_ok(name: str) -> bool:
    return bool(_LABEL_RE.fullmatch(name)) and not name.startswith(SITE_PREFIX)
