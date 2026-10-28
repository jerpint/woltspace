"""Who may use the lodge API: keys for programs, a one-time pairing for browsers.

Opt-in with WOLTSPACE_LODGE_AUTH=1. Everything here is machine-local state
under `<wolts>/.space/run/lodge/` (folder 0700, files 0600): it never syncs
and never goes in a backup.

- owner.key  the owner's key: the CLI in a terminal, and the lodge's own calls.
- wolt.key   the key a wolt session's tools (`notify`, `push-view`,
             `woltspace ...`) send. It can't do the owner-only things in
             OWNER_ONLY below.
- pairing.json  outstanding one-time pairing codes, hashed, with expiry.
- browsers.json paired browsers' session tokens, hashed, with expiry.

Only hashes are stored for codes and browser tokens. A key file holds the key
itself, because its owner needs to send it.

The limit: a program running as the owner's user can read these files. Keys
stop browser attacks, other users, and a wolt's tools stepping on owner-only
settings; they don't contain hostile code running as the owner.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path

KEY_HEADER = "x-woltspace-key"
COOKIE_SECURE = "__Host-woltspace-lodge"
COOKIE_PLAIN = "woltspace-lodge"

OWNER, WOLT = "owner", "wolt"
PAIRING_TTL = 10 * 60
BROWSER_TTL = 30 * 24 * 3600

# A wolt's key can't grant itself auto mode, publish an app, or change the
# lodge's settings. Matched against (method, path).
OWNER_ONLY = (
    ("POST", re.compile(r"^/auto-grants/(grant|revoke)$")),
    ("POST", re.compile(r"^/apps/[^/]+/share$")),
    ("POST", re.compile(r"^/settings/")),
    ("POST", re.compile(r"^/harness/(default|tiers)$")),
)


def enabled(env=os.environ) -> bool:
    return (env.get("WOLTSPACE_LODGE_AUTH") or "").strip().lower() in {"1", "true", "yes", "on"}


def auth_dir(wolts_dir) -> Path:
    return Path(wolts_dir) / ".space" / "run" / "lodge"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _prepare(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    return directory


def _write_private(path: Path, text: str) -> None:
    """Write atomically, readable by the owner only from the first byte."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        with _suppress():
            os.unlink(tmp)
        raise


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], OSError)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def ensure_keys(wolts_dir) -> dict[str, str]:
    """Create the owner and wolt keys if missing; return {scope: key}."""
    directory = _prepare(auth_dir(wolts_dir))
    keys = {}
    for scope in (OWNER, WOLT):
        path = directory / f"{scope}.key"
        try:
            key = path.read_text().strip()
        except OSError:
            key = ""
        if len(key) < 32:
            key = secrets.token_urlsafe(32)
            _write_private(path, key + "\n")
        keys[scope] = key
    return keys


def key_scope(wolts_dir, presented: str) -> str | None:
    """The scope of a presented key, or None. Compared in constant time."""
    if not presented:
        return None
    found = None
    for scope in (OWNER, WOLT):
        try:
            key = (auth_dir(wolts_dir) / f"{scope}.key").read_text().strip()
        except OSError:
            continue
        if key and hmac.compare_digest(key.encode(), presented.encode()):
            found = scope
    return found


def owner_only(method: str, path: str) -> bool:
    return any(method.upper() == m and pattern.match(path) for m, pattern in OWNER_ONLY)


# --- Pairing a browser --------------------------------------------------------

def new_pairing_code(wolts_dir, ttl: int = PAIRING_TTL, now: float | None = None) -> str:
    """A single-use code; the lodge turns it into a browser session."""
    now = time.time() if now is None else now
    path = _prepare(auth_dir(wolts_dir)) / "pairing.json"
    codes = {h: exp for h, exp in _read_json(path).items() if isinstance(exp, (int, float)) and exp > now}
    code = secrets.token_urlsafe(24)
    codes[_hash(code)] = now + ttl
    _write_private(path, json.dumps(codes))
    return code


def redeem_pairing_code(wolts_dir, code: str, now: float | None = None) -> str | None:
    """Spend a pairing code; return a new browser token, or None."""
    now = time.time() if now is None else now
    if not code or len(code) > 128:
        return None
    path = auth_dir(wolts_dir) / "pairing.json"
    codes = _read_json(path)
    expires = codes.pop(_hash(code), None)
    if expires is None:
        return None
    _write_private(path, json.dumps({h: e for h, e in codes.items() if isinstance(e, (int, float)) and e > now}))
    if not isinstance(expires, (int, float)) or expires <= now:
        return None
    token = secrets.token_urlsafe(32)
    browsers_path = auth_dir(wolts_dir) / "browsers.json"
    browsers = {h: e for h, e in _read_json(browsers_path).items() if isinstance(e, (int, float)) and e > now}
    browsers[_hash(token)] = now + BROWSER_TTL
    _write_private(browsers_path, json.dumps(browsers))
    return token


def browser_paired(wolts_dir, token: str, now: float | None = None) -> bool:
    if not token or len(token) > 128:
        return False
    now = time.time() if now is None else now
    expires = _read_json(auth_dir(wolts_dir) / "browsers.json").get(_hash(token))
    return isinstance(expires, (int, float)) and expires > now


def forget_browsers(wolts_dir) -> None:
    """Unpair every browser."""
    path = auth_dir(wolts_dir) / "browsers.json"
    if path.exists():
        _write_private(path, "{}")


class FailureLimit:
    """At most `limit` failed pairing attempts per `window` seconds, lodge-wide."""

    def __init__(self, limit: int = 10, window: float = 60.0):
        self.limit, self.window, self.failures = limit, window, []

    def blocked(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.failures = [t for t in self.failures if now - t < self.window]
        return len(self.failures) >= self.limit

    def record(self, now: float | None = None) -> None:
        self.failures.append(time.monotonic() if now is None else now)


# --- Clients ------------------------------------------------------------------

def client_key(env=os.environ, wolts_dir=None) -> str:
    """The key this process sends to its lodge, or "" when there is none.

    WOLTSPACE_LODGE_KEY wins. Otherwise a process inside a wolt session reads
    the wolt key, and anything else (the owner's terminal, the lodge itself)
    the owner key. Keys are read from files, never put on a command line.
    """
    explicit = (env.get("WOLTSPACE_LODGE_KEY") or "").strip()
    if explicit:
        return explicit
    root = wolts_dir or env.get("WOLTSPACE_WOLTS_DIR") or env.get("WOLTS_DIR")
    if not root:
        return ""
    in_session = bool(env.get("WOLTSPACE_WOLT_SESSION") or env.get("WOLT_SESSION"))
    try:
        return (auth_dir(root) / f"{WOLT if in_session else OWNER}.key").read_text().strip()
    except OSError:
        return ""


def curl_config(env=os.environ, wolts_dir=None) -> str:
    """curl `-K -` input carrying the key, so it never shows in a process list."""
    key = client_key(env, wolts_dir)
    return f'header = "X-Woltspace-Key: {key}"\n' if key else ""


def client_headers(env=os.environ, wolts_dir=None) -> dict[str, str]:
    key = client_key(env, wolts_dir)
    return {"X-Woltspace-Key": key} if key else {}
