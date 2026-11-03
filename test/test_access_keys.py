"""Cloudflare's signing keys: an outage of its certs endpoint doesn't stall
every request, and a clock slightly behind Cloudflare's doesn't refuse a login."""

import asyncio
import sys
from pathlib import Path

import jwt
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import access  # noqa: E402

TEAM = "team.cloudflareaccess.com"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(access.time, "monotonic", c)
    return c


def _verifier(fetches, results):
    v = access.AccessTokenVerifier(ttl_seconds=3600, retry_seconds=60, stale_limit=24 * 3600)

    async def refresh(team):
        fetches.append(team)
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        v._team_domain, v._keys = team, result
        v._expires_at = v._fresh_until = access.time.monotonic() + v.ttl_seconds
        v._down_team, v._down_until = "", 0.0
        return result
    v._refresh = refresh
    return v


def test_an_outage_keeps_the_last_good_keys_and_waits_before_retrying(clock):
    fetches = []
    v = _verifier(fetches, [{"k": 1}, OSError("down"), OSError("down")])
    assert asyncio.run(v._get_keys(TEAM)) == {"k": 1}
    clock.now += 3601                                   # the hour is over; Cloudflare is down
    assert asyncio.run(v._get_keys(TEAM)) == {"k": 1}
    assert asyncio.run(v._get_keys(TEAM)) == {"k": 1}  # no second fetch inside the wait
    assert len(fetches) == 2
    clock.now += 61
    assert asyncio.run(v._get_keys(TEAM)) == {"k": 1} and len(fetches) == 3


def test_with_no_good_keys_an_outage_fails_at_once(clock):
    fetches = []
    v = _verifier(fetches, [OSError("down")])
    with pytest.raises(OSError):
        asyncio.run(v._get_keys(TEAM))
    with pytest.raises(jwt.InvalidTokenError):
        asyncio.run(v._get_keys(TEAM))
    assert len(fetches) == 1


def test_old_keys_stop_being_trusted_after_a_day(clock):
    fetches = []
    v = _verifier(fetches, [{"k": 1}, OSError("down")])
    asyncio.run(v._get_keys(TEAM))
    clock.now += 3600 + 24 * 3600 + 1
    with pytest.raises(OSError):
        asyncio.run(v._get_keys(TEAM))


def _token(key, **claims):
    import time
    now = int(time.time())
    body = {"aud": "apps-aud", "iss": f"https://{TEAM}", "email": "o@example.com",
            "iat": now, "exp": now + 600, **claims}
    return jwt.encode(body, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.mark.parametrize("ahead,ok", [(30, True), (300, False)])
def test_a_login_from_a_clock_a_little_ahead_is_accepted(ahead, ok):
    import time

    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    v = access.AccessTokenVerifier()

    async def keys(_team, force=False):
        return {"k1": private.public_key()}
    v._get_keys = keys
    settings = access.AccessSettings(TEAM, "apps-aud", "o@example.com")
    now = int(time.time())
    token = _token(private, iat=now + ahead, nbf=now + ahead)
    if ok:
        assert asyncio.run(v.verify(token, settings, "apps-aud"))["email"] == "o@example.com"
    else:
        with pytest.raises(jwt.PyJWTError):
            asyncio.run(v.verify(token, settings, "apps-aud"))
