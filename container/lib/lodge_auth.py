"""Who may use the lodge API: keys for programs, a one-time pairing for browsers.

Opt-in with WOLTSPACE_LODGE_AUTH=1. Everything here is machine-local state
under `<wolts>/.space/run/lodge/` (folder 0700, files 0600): it never syncs
and never goes in a backup.

- owner.key  the owner's key: the CLI in a terminal, and the lodge's own calls.
- wolt.key   the key a wolt session's tools (`notify`, `push-view`,
             `woltspace ...`) send. It reads, and writes only what
             WOLT_WRITES below allows; nothing else, and no WebSocket.
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

from env_compat import get_env

KEY_HEADER = "x-woltspace-key"
COOKIE_SECURE = "__Host-woltspace-lodge"
COOKIE_PLAIN = "woltspace-lodge"

OWNER, WOLT = "owner", "wolt"
PAIRING_TTL = 10 * 60
BROWSER_TTL = 30 * 24 * 3600

# What a wolt's key may change: its sessions, messages, the viewport, crons,
# running apps and tools. Everything else that writes is the owner's: lodge
# and harness settings, auto-mode grants, creatures, a wolt's settings,
# archive and deletion, app keepers and sharing. A route added later is the
# owner's until it is listed here. Matched against (method, path).
_SEG = r"[^/]+"
WOLT_WRITES = tuple((method, re.compile(pattern)) for method, pattern in (
    ("POST", r"^/current$"),
    ("POST", r"^/sessions/redirect$"),
    ("POST", r"^/notify$"),
    ("POST", r"^/memory/read$"),
    ("POST", rf"^/sessions/{_SEG}/(describe|hold|message|rest|resume|stop)$"),
    ("DELETE", rf"^/sessions/{_SEG}/hold$"),
    ("POST", r"^/sessions/new/(create|lodge|telegram|slack)$"),
    ("POST", rf"^/wolts/{_SEG}/message$"),
    ("POST", r"^/wolf/crons$"),
    ("PUT", rf"^/wolf/crons/{_SEG}/{_SEG}$"),
    ("DELETE", rf"^/wolf/crons/{_SEG}/{_SEG}$"),
    ("POST", rf"^/wolf/crons/{_SEG}/{_SEG}/fire$"),
    ("POST", r"^/auto-grants/check$"),
    ("POST", rf"^/apps/{_SEG}/(start|stop|restart)$"),
    ("POST", r"^/tools/spawn$"),
    ("POST", rf"^/tools/{_SEG}/.*$"),
    ("PUT", rf"^/tools/{_SEG}/.*$"),
    ("DELETE", rf"^/tools/{_SEG}/.*$"),
    ("PATCH", rf"^/tools/{_SEG}/.*$"),
))
_READS = {"GET", "HEAD", "OPTIONS"}


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


def wolt_may(method: str, path: str) -> bool:
    """Whether a wolt's key may make this HTTP request."""
    method = method.upper()
    if method in _READS:
        return True
    return any(method == m and pattern.match(path) for m, pattern in WOLT_WRITES)


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
    root = wolts_dir or get_env("WOLTSPACE_WOLTS_DIR", env=env)
    if not root:
        return ""
    in_session = bool(get_env("WOLTSPACE_WOLT_SESSION", env=env))
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


def owner_headers(wolts_dir) -> dict[str, str]:
    """The owner key's header, for the lodge's own calls to itself."""
    if not wolts_dir:
        return {}
    try:
        key = (auth_dir(wolts_dir) / f"{OWNER}.key").read_text().strip()
    except OSError:
        return {}
    return {"X-Woltspace-Key": key} if key else {}
