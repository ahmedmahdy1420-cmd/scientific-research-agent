"""Password hashing and JWT handling."""

from __future__ import annotations

import datetime as dt

import jwt
import pytest

from app.core.config import get_settings
from app.core.errors import InvalidTokenError
from app.core.security import (
    create_token,
    decode_token,
    hash_password,
    verify_password,
)

pytestmark = pytest.mark.unit
settings = get_settings()


class TestPasswordHashing:
    def test_hash_is_argon2id_and_verifies(self):
        hashed = hash_password("correct horse battery staple")
        assert hashed.startswith("$argon2id$")
        assert verify_password("correct horse battery staple", hashed)

    def test_wrong_password_rejected(self):
        assert not verify_password("wrong", hash_password("right"))

    def test_hash_is_salted(self):
        # Two hashes of the same password must differ, or the store is
        # vulnerable to precomputation.
        assert hash_password("same") != hash_password("same")

    def test_malformed_hash_returns_false_rather_than_raising(self):
        # A corrupt row must not 500 the login endpoint.
        assert verify_password("anything", "not-a-hash") is False


class TestTokens:
    def test_round_trip_carries_claims(self):
        token, expires = create_token(
            subject="user-1", roles=["researcher"], scopes=["documents:read"]
        )
        payload = decode_token(token)
        assert payload["sub"] == "user-1"
        assert payload["roles"] == ["researcher"]
        assert payload["scopes"] == ["documents:read"]
        assert payload["typ"] == "access"
        assert expires > dt.datetime.now(dt.UTC)

    def test_every_token_has_a_unique_jti(self):
        first, _ = create_token(subject="u")
        second, _ = create_token(subject="u")
        assert decode_token(first)["jti"] != decode_token(second)["jti"]

    def test_refresh_token_rejected_where_access_expected(self):
        token, _ = create_token(subject="u", token_type="refresh")
        with pytest.raises(InvalidTokenError, match="access"):
            decode_token(token, expected_type="access")

    def test_expired_token_rejected(self):
        payload = {
            "sub": "u",
            "typ": "access",
            "jti": "x",
            "iss": settings.app_name,
            "iat": int((dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(InvalidTokenError, match="expired"):
            decode_token(token)

    def test_token_signed_with_another_key_rejected(self):
        payload = {
            "sub": "u",
            "typ": "access",
            "jti": "x",
            "iss": settings.app_name,
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(payload, "a-completely-different-secret", algorithm="HS256")
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_alg_none_attack_rejected(self):
        """The classic JWT attack: an unsigned token claiming alg=none.

        We pass an explicit algorithm allowlist to `jwt.decode`, so the header
        cannot select the algorithm.
        """
        payload = {
            "sub": "attacker",
            "typ": "access",
            "jti": "x",
            "iss": settings.app_name,
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
            "roles": ["admin"],
        }
        forged = jwt.encode(payload, key="", algorithm="none")
        with pytest.raises(InvalidTokenError):
            decode_token(forged)

    def test_wrong_issuer_rejected(self):
        payload = {
            "sub": "u",
            "typ": "access",
            "jti": "x",
            "iss": "some-other-service",
            "iat": int(dt.datetime.now(dt.UTC).timestamp()),
            "exp": int((dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).timestamp()),
        }
        token = jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
        with pytest.raises(InvalidTokenError):
            decode_token(token)

    def test_garbage_rejected(self):
        with pytest.raises(InvalidTokenError):
            decode_token("not.a.token")
