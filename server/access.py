"""Cloudflare Access token verification for lodge and app hosts."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx
import jwt
from jwt.algorithms import RSAAlgorithm


_DOMAIN_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONFIG_CACHE: dict[Path, tuple[tuple[int, int] | None, AccessSettings | None | Exception]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class AccessSettings:
    team_domain: str
    lodge_aud: str
    apps_aud: str
    owner_email: str


def access_settings_dict(settings: AccessSettings | None) -> dict | None:
    return asdict(settings) if settings else None


def _config_path(wolts_dir: Path) -> Path:
    return wolts_dir / "woltspace.json"


def _read_root(wolts_dir: Path) -> dict:
    path = _config_path(wolts_dir)
    try:
        value = json.loads(path.read_text()) if path.exists() else {}
    except (json.JSONDecodeError, OSError) as exc:
        raise RuntimeError("woltspace.json unreadable") from exc
    if not isinstance(value, dict):
        raise RuntimeError("woltspace.json unreadable")
    return value


def load_access_settings(wolts_dir: Path) -> AccessSettings | None:
    path = _config_path(wolts_dir)
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
    except FileNotFoundError:
        signature = None
    with _CONFIG_CACHE_LOCK:
        cached = _CONFIG_CACHE.get(path)
        if cached and cached[0] == signature:
            if isinstance(cached[1], Exception):
                raise RuntimeError(str(cached[1]))
            return cached[1]
    try:
        result = _load_access_settings_uncached(wolts_dir)
    except RuntimeError as exc:
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[path] = (signature, exc)
        raise
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE[path] = (signature, result)
    return result


def _load_access_settings_uncached(wolts_dir: Path) -> AccessSettings | None:
    raw = _read_root(wolts_dir).get("access")
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise RuntimeError("woltspace.json access settings are invalid")
    try:
        return validate_access_settings(raw)
    except ValueError as exc:
        raise RuntimeError("woltspace.json access settings are invalid") from exc


def validate_access_settings(raw: object) -> AccessSettings:
    if not isinstance(raw, dict) or set(raw) != {
        "team_domain", "lodge_aud", "apps_aud", "owner_email",
    }:
        raise ValueError("team_domain, lodge_aud, apps_aud, and owner_email are required")
    team_domain = str(raw["team_domain"]).strip().lower().rstrip(".")
    lodge_aud = str(raw["lodge_aud"]).strip()
    apps_aud = str(raw["apps_aud"]).strip()
    owner_email = str(raw["owner_email"]).strip().lower()
    if (not _DOMAIN_RE.fullmatch(team_domain) or "." not in team_domain
            or ".." in team_domain):
        raise ValueError("team_domain must be a domain name without a scheme, path, or port")
    if not lodge_aud or len(lodge_aud) > 512:
        raise ValueError("lodge_aud must be a non-empty Access audience")
    if not apps_aud or len(apps_aud) > 512:
        raise ValueError("apps_aud must be a non-empty Access audience")
    if lodge_aud == apps_aud:
        raise ValueError("lodge_aud and apps_aud must be different")
    if not _EMAIL_RE.fullmatch(owner_email):
        raise ValueError("owner_email must be an email address")
    return AccessSettings(team_domain, lodge_aud, apps_aud, owner_email)


def save_access_settings(wolts_dir: Path, raw: object) -> AccessSettings | None:
    settings = None if raw in (None, {}) else validate_access_settings(raw)
    cfg = _read_root(wolts_dir)
    if settings is None:
        cfg.pop("access", None)
    else:
        cfg["access"] = asdict(settings)
    path = _config_path(wolts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(cfg, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, path.stat().st_mode & 0o7777)
    except FileNotFoundError:
        pass
    os.replace(tmp, path)
    with _CONFIG_CACHE_LOCK:
        _CONFIG_CACHE.pop(path, None)
    return settings


class AccessTokenVerifier:
    def __init__(self, ttl_seconds: int = 3600, forced_refresh_interval: int = 60):
        self.ttl_seconds = ttl_seconds
        self.forced_refresh_interval = forced_refresh_interval
        self._team_domain = ""
        self._keys: dict[str, object] = {}
        self._expires_at = 0.0
        self._last_forced_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self, team_domain: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"https://{team_domain}/cdn-cgi/access/certs")
            response.raise_for_status()
            payload = response.json()
        keys = {}
        for item in payload.get("keys", []):
            if isinstance(item, dict) and item.get("kid"):
                keys[item["kid"]] = RSAAlgorithm.from_jwk(json.dumps(item))
        if not keys:
            raise jwt.InvalidTokenError("Access certificate response contained no keys")
        self._team_domain = team_domain
        self._keys = keys
        self._expires_at = time.monotonic() + self.ttl_seconds
        return keys

    async def _get_keys(self, team_domain: str, force: bool = False) -> dict[str, object]:
        if (not force and self._team_domain == team_domain and self._keys
                and time.monotonic() < self._expires_at):
            return self._keys
        async with self._lock:
            now = time.monotonic()
            if (force and self._team_domain == team_domain and self._keys
                    and now - self._last_forced_at < self.forced_refresh_interval):
                return self._keys
            if (not force and self._team_domain == team_domain and self._keys
                    and time.monotonic() < self._expires_at):
                return self._keys
            if force:
                self._last_forced_at = now
            return await self._refresh(team_domain)

    async def verify(self, token: str, settings: AccessSettings, audience: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise jwt.InvalidTokenError("invalid Access token header") from exc
        if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
            raise jwt.InvalidTokenError("Access token must use RS256 with a key id")
        kid = header["kid"]
        keys = await self._get_keys(settings.team_domain)
        key = keys.get(kid)
        if key is None:
            key = (await self._get_keys(settings.team_domain, force=True)).get(kid)
        if key is None:
            raise jwt.InvalidTokenError("unknown Access signing key")
        kwargs = dict(
            algorithms=["RS256"],
            audience=audience,
            issuer=f"https://{settings.team_domain}",
            options={"require": ["exp", "iat", "aud", "iss"]},
        )
        try:
            return jwt.decode(token, key, **kwargs)
        except jwt.InvalidSignatureError:
            rotated = (await self._get_keys(settings.team_domain, force=True)).get(kid)
            if rotated is None:
                raise
            return jwt.decode(token, rotated, **kwargs)


access_token_verifier = AccessTokenVerifier()
