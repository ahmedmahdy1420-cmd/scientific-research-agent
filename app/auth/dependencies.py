"""FastAPI security dependencies.

`get_current_principal` is the single place a request becomes an identity.
Everything downstream — routers, services, the tool registry — receives a
`Principal` and never re-parses a token.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.permissions import Permission, Principal
from app.auth.service import AuthService, principal_from_user
from app.core.config import get_settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.core.logging import bind_contextvars, get_logger
from app.core.security import decode_token
from app.database.session import get_async_session_factory

log = get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{get_settings().api_v1_prefix}/auth/token",
    scheme_name="OAuth2PasswordBearer",
    auto_error=False,
)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Request-scoped database session (FastAPI dependency)."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


DbSession = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_principal(
    request: Request,
    session: DbSession,
    token: Annotated[str | None, Depends(oauth2_scheme)] = None,
) -> Principal:
    """Resolve the bearer token to a live, active user.

    The token carries roles, but the user row is still loaded: a disabled or
    deleted account stops working immediately rather than at token expiry.
    """
    if not token:
        raise AuthenticationError("Missing bearer token")

    payload = decode_token(token, expected_type="access")
    service = AuthService(session)
    user = await service.get_user_by_id(payload["sub"])
    if user is None:
        raise AuthenticationError("Token subject no longer exists")
    if not user.is_active:
        raise AuthenticationError("Account is disabled")

    principal = principal_from_user(user)
    request.state.principal = principal
    bind_contextvars(user_id=principal.user_id)
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_permissions(
    *required: str, mode: str = "all"
) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing permissions on a route.

    >>> @router.post("/documents", dependencies=[Depends(require_permissions(
    ...     Permission.DOCUMENTS_UPLOAD))])
    """

    async def _dependency(principal: CurrentPrincipal) -> Principal:
        ok = (
            all(principal.has_permission(p) for p in required)
            if mode == "all"
            else principal.has_any_permission(*required)
        )
        if not ok:
            log.warning(
                "authz.denied",
                user_id=principal.user_id,
                required=list(required),
                held=sorted(principal.permissions),
            )
            raise AuthorizationError(
                "Insufficient permissions",
                details={"required": list(required), "mode": mode},
            )
        return principal

    return _dependency


def require_roles(*roles: str) -> Callable[[Principal], Awaitable[Principal]]:
    async def _dependency(principal: CurrentPrincipal) -> Principal:
        if not any(principal.has_role(r) for r in roles):
            raise AuthorizationError("Insufficient role", details={"required_roles": list(roles)})
        return principal

    return _dependency


# Frequently used, pre-built dependencies.
RequireAgentRun = Annotated[Principal, Depends(require_permissions(Permission.AGENT_RUN))]
RequireDocumentRead = Annotated[Principal, Depends(require_permissions(Permission.DOCUMENTS_READ))]
RequireDocumentUpload = Annotated[
    Principal, Depends(require_permissions(Permission.DOCUMENTS_UPLOAD))
]
RequireApprovalDecision = Annotated[
    Principal, Depends(require_permissions(Permission.APPROVALS_DECIDE))
]
RequireEvaluationRun = Annotated[Principal, Depends(require_permissions(Permission.EVALUATION_RUN))]
