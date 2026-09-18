"""Authentication service: credential checks and token issuing."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Principal
from app.core.errors import AuthenticationError
from app.core.logging import get_logger
from app.core.security import create_token, decode_token, hash_password, verify_password
from app.models.user import User

log = get_logger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_user_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.lower(), User.deleted_at.is_(None))
        return (await self._session.execute(stmt)).unique().scalar_one_or_none()

    async def get_user_by_id(self, user_id: uuid.UUID | str) -> User | None:
        try:
            uid = uuid.UUID(str(user_id))
        except (ValueError, AttributeError):
            return None
        stmt = select(User).where(User.id == uid, User.deleted_at.is_(None))
        return (await self._session.execute(stmt)).unique().scalar_one_or_none()

    async def authenticate(self, email: str, password: str) -> User:
        """Verify credentials.

        The same error is raised for "no such user" and "wrong password", and a
        dummy hash is verified in the missing-user branch so response timing
        does not reveal which accounts exist.
        """
        user = await self.get_user_by_email(email)
        if user is None:
            verify_password(password, hash_password("timing-equaliser"))
            log.info("auth.login_failed", reason="unknown_user", email=email)
            raise AuthenticationError("Incorrect email or password")

        if not verify_password(password, user.hashed_password):
            log.info("auth.login_failed", reason="bad_password", user_id=str(user.id))
            raise AuthenticationError("Incorrect email or password")

        if not user.is_active:
            log.info("auth.login_failed", reason="inactive", user_id=str(user.id))
            raise AuthenticationError("Account is disabled")

        user.last_login_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        log.info("auth.login_succeeded", user_id=str(user.id), roles=user.role_names)
        return user

    def issue_tokens(self, user: User) -> tuple[str, str, dt.datetime]:
        access, expires = create_token(
            subject=str(user.id),
            token_type="access",  # noqa: S106
            roles=user.role_names,
            scopes=sorted(user.permissions),
            extra={"email": user.email},
        )
        refresh, _ = create_token(subject=str(user.id), token_type="refresh")  # noqa: S106
        return access, refresh, expires

    async def refresh(self, refresh_token: str) -> tuple[User, str, str, dt.datetime]:
        """Exchange a refresh token for a new pair.

        Roles are re-read from the database, so a permission revoked while a
        refresh token was outstanding takes effect at the next refresh rather
        than persisting for the token's whole lifetime.
        """
        payload = decode_token(refresh_token, expected_type="refresh")
        user = await self.get_user_by_id(payload["sub"])
        if user is None or not user.is_active:
            raise AuthenticationError("Token subject is no longer valid")
        access, new_refresh, expires = self.issue_tokens(user)
        return user, access, new_refresh, expires


def principal_from_user(user: User) -> Principal:
    return Principal(
        user_id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        roles=frozenset(user.role_names),
        permissions=frozenset(user.permissions),
        department=user.department,
    )
