"""Authentication endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.auth.dependencies import DbSession
from app.auth.service import AuthService
from app.core.logging import get_logger
from app.core.ratelimit import RateLimiter
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    TokenResponse,
)
from app.schemas.common import ErrorResponse

router = APIRouter(prefix="/auth", tags=["auth"])
log = get_logger(__name__)


async def _login_rate_limit(request: Request) -> None:
    """Throttle credential attempts by client IP.

    Keyed on IP rather than the submitted email, because an attacker controls
    the email field and would otherwise get a fresh bucket per guess.
    """
    client = request.client.host if request.client else "unknown"
    await RateLimiter().check(client, bucket="login", limit=10, window=60)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Exchange credentials for a JWT pair",
    responses={401: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
)
async def login(
    payload: LoginRequest,
    session: DbSession,
    _throttle: Annotated[None, Depends(_login_rate_limit)],
) -> TokenResponse:
    service = AuthService(session)
    user = await service.authenticate(payload.email, payload.password)
    access, refresh, expires = service.issue_tokens(user)
    return TokenResponse(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires,
        expires_in=int((expires - user.last_login_at).total_seconds())
        if user.last_login_at
        else 3600,
    )


@router.post(
    "/token",
    response_model=TokenResponse,
    summary="OAuth2 password flow (used by the Swagger 'Authorize' button)",
    include_in_schema=True,
)
async def token(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: DbSession,
    _throttle: Annotated[None, Depends(_login_rate_limit)],
) -> TokenResponse:
    """Standard OAuth2 password grant.

    Exists alongside /login so FastAPI's interactive docs can authenticate:
    `username` is the email address.
    """
    service = AuthService(session)
    user = await service.authenticate(form.username, form.password)
    access, refresh, expires = service.issue_tokens(user)
    return TokenResponse(
        access_token=access, refresh_token=refresh, expires_at=expires, expires_in=3600
    )


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Exchange a refresh token for a new pair",
    responses={401: {"model": ErrorResponse}},
)
async def refresh(payload: RefreshRequest, session: DbSession) -> TokenResponse:
    service = AuthService(session)
    _user, access, new_refresh, expires = await service.refresh(payload.refresh_token)
    return TokenResponse(
        access_token=access, refresh_token=new_refresh, expires_at=expires, expires_in=3600
    )
