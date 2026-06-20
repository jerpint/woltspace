"""Web-layer auth (issue #353) — both modes.

Pure-Python unit tests. No live server needed. Patches WOLTSPACE_AUTH +
WOLTS_DIR env vars so users.json is read from tmp_path.

Usage: uv run --extra test pytest test/test_auth.py -v
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def auth_env(tmp_path, monkeypatch):
    """Point WOLTS_DIR at a tmp dir, enable cloudflare auth."""
    monkeypatch.setenv("WOLTSPACE_AUTH", "cloudflare")
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
    # Reload module so module-level state honors new env
    import server.auth as auth
    import importlib
    importlib.reload(auth)
    return auth


@pytest.fixture
def auth_off(tmp_path, monkeypatch):
    """Default mode — auth disabled."""
    monkeypatch.setenv("WOLTSPACE_AUTH", "none")
    monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
    import server.auth as auth
    import importlib
    importlib.reload(auth)
    return auth


class TestAuthMode:
    def test_default_is_none(self, tmp_path, monkeypatch):
        monkeypatch.delenv("WOLTSPACE_AUTH", raising=False)
        # Point WOLTS_DIR at an empty tmp so the .env fallback can't see
        # the real /workspace/wolts/.env where WOLTSPACE_AUTH may be set.
        monkeypatch.setenv("WOLTS_DIR", str(tmp_path))
        import server.auth as auth
        import importlib
        importlib.reload(auth)
        assert auth.auth_mode() == "none"
        assert auth.is_enabled() is False

    def test_cloudflare_mode(self, auth_env):
        assert auth_env.auth_mode() == "cloudflare"
        assert auth_env.is_enabled() is True


class TestUsersFile:
    def test_empty_when_missing(self, auth_env):
        assert auth_env.load_users() == []

    def test_roundtrip(self, auth_env):
        users = [{"email": "a@x.com", "wolts": ["foo"]}]
        auth_env.save_users(users)
        assert auth_env.load_users() == users

    def test_find_user_case_insensitive(self, auth_env):
        auth_env.save_users([{"email": "Mixed@Case.com", "wolts": ["foo"]}])
        assert auth_env.find_user("mixed@case.com") is not None
        assert auth_env.find_user("MIXED@CASE.COM") is not None
        assert auth_env.find_user("other@x.com") is None

    def test_corrupt_users_json_returns_empty(self, auth_env, tmp_path):
        (tmp_path / ".space" / "auth").mkdir(parents=True)
        (tmp_path / ".space" / "auth" / "users.json").write_text("not json {{{")
        assert auth_env.load_users() == []


class TestAddUser:
    def test_add_new(self, auth_env):
        auth_env.add_user("alice@x.com", ["foo"])
        u = auth_env.find_user("alice@x.com")
        assert u is not None
        assert u["wolts"] == ["foo"]

    def test_add_existing_returns_same(self, auth_env):
        auth_env.add_user("alice@x.com", ["foo"])
        auth_env.add_user("alice@x.com", ["bar"])
        # Existing entry preserved, not overwritten
        users = auth_env.load_users()
        assert len(users) == 1
        assert users[0]["wolts"] == ["foo"]


class TestGrantWolt:
    def test_grant_creates_entry_if_missing(self, auth_env):
        auth_env.grant_wolt("alice@x.com", "foo")
        u = auth_env.find_user("alice@x.com")
        assert u is not None
        assert u["wolts"] == ["foo"]

    def test_grant_appends_to_existing(self, auth_env):
        auth_env.save_users([{"email": "alice@x.com", "wolts": ["foo"]}])
        auth_env.grant_wolt("alice@x.com", "bar")
        u = auth_env.find_user("alice@x.com")
        assert u["wolts"] == ["foo", "bar"]

    def test_grant_is_idempotent(self, auth_env):
        auth_env.grant_wolt("alice@x.com", "foo")
        auth_env.grant_wolt("alice@x.com", "foo")
        assert auth_env.find_user("alice@x.com")["wolts"] == ["foo"]

    def test_grant_noop_on_wildcard(self, auth_env):
        auth_env.save_users([{"email": "alice@x.com", "wolts": ["*"]}])
        auth_env.grant_wolt("alice@x.com", "foo")
        assert auth_env.find_user("alice@x.com")["wolts"] == ["*"]


class TestPermissions:
    def test_auth_off_allows_everything(self, auth_off):
        assert auth_off.can_access_wolt(None, "anything") is True
        assert auth_off.can_access_wolt("any@x.com", "anything") is True

    def test_auth_on_unknown_user_denied(self, auth_env):
        assert auth_env.can_access_wolt("unknown@x.com", "foo") is False
        assert auth_env.can_access_wolt(None, "foo") is False

    def test_auth_on_wildcard_sees_all(self, auth_env):
        auth_env.save_users([{"email": "alice@x.com", "wolts": ["*"]}])
        assert auth_env.can_access_wolt("alice@x.com", "anything") is True

    def test_auth_on_user_scoped_to_allow_list(self, auth_env):
        auth_env.save_users([{"email": "u@x.com", "wolts": ["foo", "bar"]}])
        assert auth_env.can_access_wolt("u@x.com", "foo") is True
        assert auth_env.can_access_wolt("u@x.com", "bar") is True
        assert auth_env.can_access_wolt("u@x.com", "baz") is False


class TestVisibleWolts:
    def test_wildcard_sees_all(self, auth_env):
        auth_env.save_users([{"email": "alice@x.com", "wolts": ["*"]}])
        wolts = [{"dir": "a"}, {"dir": "b"}, {"dir": "c"}]
        assert auth_env.visible_wolts("alice@x.com", wolts) == wolts

    def test_user_filtered_to_allow_list(self, auth_env):
        auth_env.save_users([{"email": "u@x.com", "wolts": ["a", "c"]}])
        wolts = [{"dir": "a"}, {"dir": "b"}, {"dir": "c"}]
        result = auth_env.visible_wolts("u@x.com", wolts)
        assert [w["dir"] for w in result] == ["a", "c"]

    def test_unknown_sees_nothing(self, auth_env):
        wolts = [{"dir": "a"}]
        assert auth_env.visible_wolts("ghost@x.com", wolts) == []
        assert auth_env.visible_wolts(None, wolts) == []

    def test_auth_off_passes_through(self, auth_off):
        wolts = [{"dir": "a"}, {"dir": "b"}]
        assert auth_off.visible_wolts(None, wolts) == wolts


class TestJWTRoundtrip:
    """End-to-end: self-sign a JWT with a known key, serve it as the
    'JWKS' response, and verify the middleware decodes the email correctly.

    This catches bugs in the actual decode path that 'garbage in, None out'
    tests miss — e.g. PyJWT API drift, audience claim shape, kid lookup.
    """

    def test_valid_jwt_extracts_email(self, auth_env, monkeypatch):
        try:
            import jwt as pyjwt
            from jwt.algorithms import RSAAlgorithm
        except ImportError:
            import pytest
            pytest.skip("PyJWT[crypto] not installed")

        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import json as _json

        # Generate an ephemeral RSA keypair for this test
        privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pubkey = privkey.public_key()
        priv_pem = privkey.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk = _json.loads(RSAAlgorithm.to_jwk(pubkey))
        jwk["kid"] = "test-kid-1"

        # Pre-seed the JWKS cache so the middleware doesn't hit the network
        auth_env._jwks_cache["keys"] = [jwk]
        auth_env._jwks_cache["fetched_at"] = 9_999_999_999

        monkeypatch.setenv("WOLTSPACE_CF_TEAM_DOMAIN", "test.cloudflareaccess.com")
        monkeypatch.setenv("WOLTSPACE_CF_AUD", "test-aud")

        token = pyjwt.encode(
            {"email": "alice@x.com", "aud": "test-aud", "iss": "test"},
            priv_pem,
            algorithm="RS256",
            headers={"kid": "test-kid-1"},
        )

        class FakeReq:
            headers = {"Cf-Access-Jwt-Assertion": token}

        result = auth_env.extract_email(FakeReq())
        assert result == "alice@x.com"

    def test_wrong_audience_rejected(self, auth_env, monkeypatch):
        try:
            import jwt as pyjwt
            from jwt.algorithms import RSAAlgorithm
        except ImportError:
            import pytest
            pytest.skip("PyJWT[crypto] not installed")

        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        import json as _json

        privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        priv_pem = privkey.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        jwk = _json.loads(RSAAlgorithm.to_jwk(privkey.public_key()))
        jwk["kid"] = "test-kid-1"

        auth_env._jwks_cache["keys"] = [jwk]
        auth_env._jwks_cache["fetched_at"] = 9_999_999_999
        monkeypatch.setenv("WOLTSPACE_CF_TEAM_DOMAIN", "test.cloudflareaccess.com")
        monkeypatch.setenv("WOLTSPACE_CF_AUD", "expected-aud")

        token = pyjwt.encode(
            {"email": "alice@x.com", "aud": "WRONG-aud"},
            priv_pem,
            algorithm="RS256",
            headers={"kid": "test-kid-1"},
        )

        class FakeReq:
            headers = {"Cf-Access-Jwt-Assertion": token}

        assert auth_env.extract_email(FakeReq()) is None


class TestJWTExtraction:
    def test_extract_none_when_auth_disabled(self, auth_off):
        from fastapi import Request
        # auth disabled → always None regardless of header
        class FakeReq:
            headers = {"Cf-Access-Jwt-Assertion": "anything"}
        assert auth_off.extract_email(FakeReq()) is None

    def test_extract_none_when_no_header(self, auth_env):
        class FakeReq:
            headers = {}
        assert auth_env.extract_email(FakeReq()) is None

    def test_extract_none_when_no_team_domain(self, auth_env, monkeypatch):
        monkeypatch.delenv("WOLTSPACE_CF_TEAM_DOMAIN", raising=False)
        class FakeReq:
            headers = {"Cf-Access-Jwt-Assertion": "garbage.token.here"}
        assert auth_env.extract_email(FakeReq()) is None

    def test_extract_rejects_invalid_token(self, auth_env, monkeypatch):
        # Even with team domain set, garbage token returns None (doesn't raise)
        monkeypatch.setenv("WOLTSPACE_CF_TEAM_DOMAIN", "example.cloudflareaccess.com")
        # Avoid network — pre-seed the cache
        auth_env._jwks_cache["keys"] = [{"kid": "fake", "kty": "RSA", "n": "x", "e": "AQAB"}]
        auth_env._jwks_cache["fetched_at"] = 9_999_999_999
        class FakeReq:
            headers = {"Cf-Access-Jwt-Assertion": "not.a.real.jwt"}
        assert auth_env.extract_email(FakeReq()) is None


class TestLocalLoopback:
    def test_local_pseudo_user_can_access_anything(self, auth_env):
        assert auth_env.can_access_wolt("__local__", "any-wolt") is True

    def test_local_pseudo_user_sees_all_wolts(self, auth_env):
        wolts = [{"dir": "a"}, {"dir": "b"}]
        assert auth_env.visible_wolts("__local__", wolts) == wolts

    def test_is_loopback_127(self, auth_env):
        from types import SimpleNamespace
        req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))
        assert auth_env.is_loopback(req) is True

    def test_is_loopback_external(self, auth_env):
        from types import SimpleNamespace
        req = SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"))
        assert auth_env.is_loopback(req) is False

    def test_is_loopback_no_client(self, auth_env):
        from types import SimpleNamespace
        req = SimpleNamespace(client=None)
        assert auth_env.is_loopback(req) is False


class TestRequireHelpers:
    def test_require_wolt_passes_when_disabled(self, auth_off):
        from types import SimpleNamespace
        req = SimpleNamespace(state=SimpleNamespace(user_email=None))
        assert auth_off.require_wolt(req, "anything") is None

    def test_require_wolt_blocks_unknown(self, auth_env):
        from types import SimpleNamespace
        req = SimpleNamespace(state=SimpleNamespace(user_email=None))
        resp = auth_env.require_wolt(req, "foo")
        assert resp is not None
        assert resp.status_code == 403

    def test_require_wolt_allows_wildcard(self, auth_env):
        from types import SimpleNamespace
        auth_env.save_users([{"email": "a@x.com", "wolts": ["*"]}])
        req = SimpleNamespace(state=SimpleNamespace(user_email="a@x.com"))
        assert auth_env.require_wolt(req, "any") is None

    def test_require_wolt_blocks_wrong_wolt(self, auth_env):
        from types import SimpleNamespace
        auth_env.save_users([{"email": "u@x.com", "wolts": ["foo"]}])
        req = SimpleNamespace(state=SimpleNamespace(user_email="u@x.com"))
        resp = auth_env.require_wolt(req, "bar")
        assert resp is not None
        assert resp.status_code == 403
