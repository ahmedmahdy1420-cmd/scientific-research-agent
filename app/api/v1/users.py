"""Identity endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from app.auth.dependencies import CurrentPrincipal, DbSession
from app.auth.service import AuthService
from app.core.errors import NotFoundError
from app.schemas.auth import CurrentUserResponse, RoleResponse

router = APIRouter(tags=["users"])


@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="The authenticated user, their roles and effective permissions",
)
async def me(principal: CurrentPrincipal, session: DbSession) -> CurrentUserResponse:
    """Returns what the caller *is* and what they may do.

    The frontend uses `permissions` to decide which controls to render. That is
    a usability decision, not a security one — every endpoint re-checks.
    """
    user = await AuthService(session).get_user_by_id(principal.user_id)
    if user is None:
        raise NotFoundError("User no longer exists")

    return CurrentUserResponse(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        department=user.department,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        roles=[RoleResponse.model_validate(r) for r in user.roles],
        permissions=sorted(principal.permissions),
        max_access_level=principal.max_access_level.value,
    )
