"""Web-layer auth — Cloudflare Access JWT + per-user wolt allow-lists.

Two modes, picked at boot via WOLTSPACE_AUTH:
  - "none"        (default): no-op. Every request allowed. Today's behavior.
  - "cloudflare":            Validate Cf-Access-Jwt-Assertion, look up email
                             in users.json, gate access to wolts (and apps,
                             via their keeper wolt).

Data model — wolts/.space/auth/users.json:

    {
      "users": [
        {"email": "alice@example.com", "wolts": ["*"]},
        {"email": "bob@example.com",   "wolts": ["bloggo", "shared"]}
      ]
    }

The wildcard "*" in wolts means "every wolt" — a convenience, not a role.
This MVP has no admin concept; access is purely allow-list. An app is
accessible iff its keeper wolt is.

See: github.com/jerpint/woltspace/issues/353
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


AUTH_HEADER = "Cf-Access-Jwt-Assertion"


# --- Mode + config ---

def _env(key: str) -> str:
    """Read env var. Falls back to the shared wolts/.env so auth settings
    can be flipped without restarting the server process."""
    v = os.environ.get(key)
    if v:
        return v
    # Fallback: parse the shared wolts root .env directly. Auth config lives
    # here (next to CLOUDFLARE_* etc) and may have been edited after server
    # boot.
    try:
        wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
        env_file = wolts_dir / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, val = line.partition("=")
                if k.strip() == key:
                    return val.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def auth_mode() -> str:
    """Return 'cloudflare' or 'none' (default)."""
    return (_env("WOLTSPACE_AUTH") or "none").strip().lower()


def is_enabled() -> bool:
    return auth_mode() == "cloudflare"


def _team_domain() -> str:
    """e.g. 'jerpint.cloudflareaccess.com'. Set via env."""
    return _env("WOLTSPACE_CF_TEAM_DOMAIN").strip()


def _aud_tag() -> str:
    """The Application AUD tag for the lodge Access app. Set via env."""
    return _env("WOLTSPACE_CF_AUD").strip()


# --- users.json ---

def _users_path() -> Path:
    # Imported lazily so tests can patch WOLTS_DIR via the env var.
    wolts_dir = Path(os.environ.get("WOLTS_DIR", "/workspace/wolts"))
    return wolts_dir / ".space" / "auth" / "users.json"


def load_users() -> list[dict[str, Any]]:
    """Read users.json. Returns [] if missing or unparseable."""
    p = _users_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data.get("users", []) if isinstance(data, dict) else []
    except Exception:
        return []


def save_users(users: list[dict[str, Any]]) -> None:
    p = _users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"users": users}, indent=2) + "\n")


def find_user(email: str) -> dict[str, Any] | None:
    if not email:
        return None
    email = email.lower()
    for u in load_users():
        if (u.get("email") or "").lower() == email:
            return u
    return None


def add_user(email: str, wolts: list[str] | None = None) -> dict:
    """Add a user or return the existing entry. Idempotent."""
    email = email.strip().lower()
    users = load_users()
    for u in users:
        if (u.get("email") or "").lower() == email:
            return u
    entry = {"email": email, "wolts": wolts or [], "added_at": int(time.time())}
    users.append(entry)
    save_users(users)
    return entry


def grant_wolt(email: str, wolt_name: str) -> None:
    """Append wolt_name to email's allow-list if not already there. Idempotent.

    Creates the user entry if it doesn't exist (used by auto-onboarding on
    wolt creation)."""
    email = email.strip().lower()
    users = load_users()
    u = None
    for entry in users:
        if (entry.get("email") or "").lower() == email:
            u = entry
            break
    if u is None:
        u = {"email": email, "wolts": [], "added_at": int(time.time())}
        users.append(u)
    allow = u.get("wolts") or []
    if "*" in allow or wolt_name in allow:
        return
    allow.append(wolt_name)
    u["wolts"] = allow
    save_users(users)


# --- Permission resolution ---

def can_access_wolt(email: str | None, wolt_name: str) -> bool:
    """Auth disabled → True. Otherwise: user must exist and the wolt must be
    in their list (or they hold the wildcard).

    The synthetic '__local__' email is granted full access — see is_loopback
    in the middleware. This is the in-container localhost safety net.
    """
    if not is_enabled():
        return True
    if not email:
        return False
    if email == "__local__":
        return True
    u = find_user(email)
    if not u:
        return False
    allowed = u.get("wolts") or []
    return "*" in allowed or wolt_name in allowed


def can_access_app(email: str | None, app_name: str) -> bool:
    """An app is accessible iff its keeper wolt is."""
    if not is_enabled():
        return True
    # Resolve keeper. Lazy import — avoids circular dep at module load.
    try:
        import sys
        from pathlib import Path as _P
        lib = _P(__file__).resolve().parent.parent / "container" / "lib"
        if str(lib) not in sys.path:
            sys.path.insert(0, str(lib))
        from apps import get_app  # type: ignore
        app = get_app(app_name)
    except Exception:
        return False
    if not app:
        return False
    return can_access_wolt(email, app.keeper)


def visible_wolts(email: str | None, all_wolts: list[dict]) -> list[dict]:
    """Filter a list of wolt dicts (with 'dir' or 'name' key) to those the user can see."""
    if not is_enabled():
        return all_wolts
    if not email:
        return []
    if email == "__local__":
        return all_wolts
    u = find_user(email)
    if not u:
        return []
    allowed = set(u.get("wolts") or [])
    if "*" in allowed:
        return all_wolts
    return [w for w in all_wolts if (w.get("dir") or w.get("name")) in allowed]


# --- JWT validation ---

_jwks_cache: dict[str, Any] = {"keys": None, "fetched_at": 0}
_JWKS_TTL = 3600  # 1h


def _fetch_jwks() -> list[dict] | None:
    """Fetch and cache Cloudflare Access public keys."""
    td = _team_domain()
    if not td:
        return None
    now = time.time()
    if _jwks_cache["keys"] and now - _jwks_cache["fetched_at"] < _JWKS_TTL:
        return _jwks_cache["keys"]
    try:
        import urllib.request
        url = f"https://{td}/cdn-cgi/access/certs"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read())
        keys = data.get("keys") or []
        _jwks_cache["keys"] = keys
        _jwks_cache["fetched_at"] = now
        return keys
    except Exception as e:
        print(f"[auth] failed to fetch JWKS: {e}")
        return _jwks_cache["keys"]  # fall back to stale cache if any


_last_error: str = ""


def last_error() -> str:
    """Most recent JWT validation failure (for /auth/debug endpoint)."""
    return _last_error


def _fail(msg: str) -> None:
    """Record an auth failure loudly — to stderr and to last_error()."""
    global _last_error
    _last_error = msg
    print(f"[auth] {msg}", flush=True)


def extract_email(request: Request) -> str | None:
    """Validate the CF Access JWT on the request and return the email claim.

    Returns None if no JWT, invalid JWT, or auth disabled.
    """
    if not is_enabled():
        return None
    token = request.headers.get(AUTH_HEADER) or request.headers.get(AUTH_HEADER.lower())
    if not token:
        return None
    try:
        import jwt as pyjwt  # PyJWT[crypto]
        from jwt.algorithms import RSAAlgorithm
    except ImportError as e:
        _fail(f"PyJWT not installed — cannot validate JWT ({e}). Run 'uv sync --project server' or install PyJWT[crypto].")
        return None

    keys = _fetch_jwks()
    if not keys:
        _fail(f"JWKS empty — team_domain={_team_domain() or '<UNSET>'}")
        return None

    aud = _aud_tag()

    try:
        unverified = pyjwt.get_unverified_header(token)
    except Exception as e:
        _fail(f"JWT header parse failed: {e}")
        return None
    kid = unverified.get("kid")
    key_data = next((k for k in keys if k.get("kid") == kid), None)
    if not key_data:
        _fail(f"JWT kid={kid!r} not in JWKS (have {[k.get('kid') for k in keys]})")
        return None

    try:
        public_key = RSAAlgorithm.from_jwk(json.dumps(key_data))
        claims = pyjwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            audience=aud if aud else None,
            options={"verify_aud": bool(aud)},
        )
    except Exception as e:
        _fail(f"JWT decode failed: {e!r} aud_set={bool(aud)} aud_tail={aud[-8:] if aud else ''}")
        return None

    email = (claims.get("email") or "").strip().lower()
    return email or None


def is_loopback(request: Request) -> bool:
    """True if the request originated from in-container loopback.

    In auth=cloudflare mode, in-container localhost callers don't go through
    Cloudflare Access — they have no JWT. Trust them as a safety net so the
    operator can never lock themselves out of their own machine.

    Threat model: the OS already protects against unauthorized in-container
    access. When #354 (filesystem isolation) lands, this assumption gets
    stronger; for now, it just codifies what was already true.
    """
    client = request.client
    if not client:
        return False
    return client.host in ("127.0.0.1", "::1", "localhost")


# --- HTTP helpers ---

def forbid(detail: str = "forbidden") -> JSONResponse:
    return JSONResponse({"error": detail}, status_code=403)


def pending_approval(email: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": "pending approval",
            "email": email,
            "detail": (
                "You're authenticated via Cloudflare Access but haven't been "
                "granted access to any wolts yet. Ask the admin to add you to "
                "users.json."
            ),
        },
        status_code=403,
    )


def require_wolt(request: Request, wolt_name: str) -> JSONResponse | None:
    """Return a 403 response if request user can't access wolt_name. None if OK."""
    if not is_enabled():
        return None
    email = getattr(request.state, "user_email", None)
    if not email:
        return forbid("not authenticated")
    if not can_access_wolt(email, wolt_name):
        return forbid(f"access to wolt '{wolt_name}' denied")
    return None


def require_app(request: Request, app_name: str) -> JSONResponse | None:
    if not is_enabled():
        return None
    email = getattr(request.state, "user_email", None)
    if not email:
        return forbid("not authenticated")
    if not can_access_app(email, app_name):
        return forbid(f"access to app '{app_name}' denied")
    return None


def user_email(request: Request) -> str | None:
    return getattr(request.state, "user_email", None)


# --- WebSocket auth ---
# The @app.middleware("http") hook does NOT run for websocket connections, so
# request.state.user_email is never populated for them. These helpers re-derive
# the caller's identity directly from the upgrade request's headers/client
# (extract_email + is_loopback only touch .headers / .client, which WebSocket
# objects also expose).

def ws_email(ws) -> str | None:
    """Resolve the caller email for a websocket upgrade, applying the same
    loopback safety net as the http middleware."""
    email = extract_email(ws)
    if not email and is_enabled() and is_loopback(ws):
        return "__local__"
    return email


def ws_can_access_wolt(ws, wolt_name: str) -> bool:
    """True if the websocket caller may access wolt_name (or auth disabled)."""
    if not is_enabled():
        return True
    return can_access_wolt(ws_email(ws), wolt_name)


def wolt_from_session(session_name: str) -> str:
    """Extract the wolt name from a session slug ({wolt}-{adj}-{noun}-{hex})."""
    if session_name.count("-") >= 3:
        return session_name.rsplit("-", 3)[0]
    return session_name
