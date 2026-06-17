"""Web-layer auth — Cloudflare Access JWT + per-user wolt allow-lists.

Two modes, picked at boot via WOLTSPACE_AUTH:
  - "none"        (default): no-op. Every request allowed. Today's behavior.
  - "cloudflare":            Validate Cf-Access-Jwt-Assertion, look up email
                             in users.json, gate access to wolts (and apps,
                             via their keeper wolt).

Data model — wolts/.space/auth/users.json:

    {
      "users": [
        {"email": "admin@example.com", "wolts": ["*"]},
        {"email": "user@example.com",  "wolts": ["bloggo", "shared"]}
      ]
    }

A user with wolts == ["*"] is an admin (sees and controls everything).
An app is accessible iff its keeper wolt is.

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

def auth_mode() -> str:
    """Return 'cloudflare' or 'none' (default)."""
    return (os.environ.get("WOLTSPACE_AUTH") or "none").strip().lower()


def is_enabled() -> bool:
    return auth_mode() == "cloudflare"


def _team_domain() -> str:
    """e.g. 'jerpint.cloudflareaccess.com'. Set via env."""
    return (os.environ.get("WOLTSPACE_CF_TEAM_DOMAIN") or "").strip()


def _aud_tag() -> str:
    """The Application AUD tag for the lodge Access app. Set via env."""
    return (os.environ.get("WOLTSPACE_CF_AUD") or "").strip()


def _admin_email() -> str:
    """Bootstrap admin — auto-added to users.json on first sight."""
    return (os.environ.get("WOLTSPACE_ADMIN_EMAIL") or "").strip().lower()


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


def bootstrap_admin() -> None:
    """Ensure WOLTSPACE_ADMIN_EMAIL exists in users.json with wolts=['*']."""
    if not is_enabled():
        return
    email = _admin_email()
    if not email:
        return
    users = load_users()
    for u in users:
        if (u.get("email") or "").lower() == email:
            # Promote: ensure wildcard
            if u.get("wolts") != ["*"]:
                u["wolts"] = ["*"]
                save_users(users)
            return
    users.append({
        "email": email,
        "wolts": ["*"],
        "added_at": int(time.time()),
        "added_by": "bootstrap",
    })
    save_users(users)


# --- Permission resolution ---

def is_admin(email: str | None) -> bool:
    if not email:
        return False
    u = find_user(email)
    return bool(u and "*" in (u.get("wolts") or []))


def can_access_wolt(email: str | None, wolt_name: str) -> bool:
    """Auth disabled → True. Otherwise: user must exist and the wolt must be
    in their list (or they hold the wildcard)."""
    if not is_enabled():
        return True
    if not email:
        return False
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
    if not is_enabled() or is_admin(email):
        return all_wolts
    if not email:
        return []
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
    except ImportError:
        print("[auth] PyJWT not installed — cannot validate JWT")
        return None

    keys = _fetch_jwks()
    if not keys:
        return None

    aud = _aud_tag()

    try:
        unverified = pyjwt.get_unverified_header(token)
    except Exception:
        return None
    kid = unverified.get("kid")
    key_data = next((k for k in keys if k.get("kid") == kid), None)
    if not key_data:
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
        print(f"[auth] JWT decode failed: {e}")
        return None

    email = (claims.get("email") or "").strip().lower()
    return email or None


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
