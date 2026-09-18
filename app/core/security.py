"""Password hashing and JWT issuing/validation.

Deliberate choices
------------------
* **Argon2id via pwdlib** - memory-hard, the current OWASP recommendation.
  pwdlib is the maintained successor to passlib (which is unmaintained and
  breaks on modern bcrypt).
* **PyJWT** rather than python-jose: python-jose has had unpatched advisories
  and is effectively unmaintained; PyJWT is actively released.
* Tokens carry `sub`, `jti`, `typ`, `roles` and `scopes`. Roles are embedded so
  the API does not need a DB round trip on every request, but *any* sensitive
  action re-reads the user row - a token is an authentication artefact, the
  database is the authority on authorisation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.config import Settings, get_settings
from app.core.errors import InvalidTokenError

_password_hash = PasswordHash((Argon2Hasher(),))

TokenType = Literal["access", "refresh"]


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time-ish verification; never raises on malformed hashes."""
    try:
        return _password_hash.verify(password, hashed)
    except Exception:  # noqa: BLE001 - a corrupt hash must not 500 the login route
        return False


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def create_token(
    *,
    subject: str,
    token_type: TokenType = "access",  # noqa: S107 - a token kind, not a password
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> tuple[str, dt.datetime]:
    """Issue a signed JWT. Returns (token, expires_at)."""
    cfg = settings or get_settings()
    minutes = (
        cfg.access_token_expire_minutes
        if token_type == "access"  # noqa: S105
        else cfg.refresh_token_expire_minutes
    )
    issued = _now()
    expires = issued + dt.timedelta(minutes=minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type,
        "iat": int(issued.timestamp()),
        "nbf": int(issued.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": cfg.app_name,
        "roles": roles or [],
        "scopes": scopes or [],
        **(extra or {}),
    }
    token = jwt.encode(payload, cfg.jwt_secret, algorithm=cfg.jwt_algorithm)
    return token, expires


def decode_token(
    token: str,
    *,
    expected_type: TokenType | None = "access",
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Verify signature, expiry and issuer. Raises InvalidTokenError."""
    cfg = settings or get_settings()
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            cfg.jwt_secret,
            algorithms=[cfg.jwt_algorithm],  # allowlist: never trust the header `alg`
            issuer=cfg.app_name,
            options={"require": ["exp", "sub", "jti", "iat"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise InvalidTokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidTokenError("Token is invalid") from exc

    if expected_type is not None and payload.get("typ") != expected_type:
        raise InvalidTokenError(f"Expected a {expected_type} token")
    return payload
