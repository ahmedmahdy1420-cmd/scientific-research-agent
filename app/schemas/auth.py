"""Authentication schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMModel


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - OAuth2 scheme name
    expires_at: dt.datetime
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class RoleResponse(ORMModel):
    id: uuid.UUID
    name: str
    description: str
    permissions: list[str]


class UserResponse(ORMModel):
    id: uuid.UUID
    email: str
    full_name: str
    is_active: bool
    department: str | None = None
    last_login_at: dt.datetime | None = None
    created_at: dt.datetime
    roles: list[RoleResponse] = Field(default_factory=list)


class CurrentUserResponse(UserResponse):
    """`GET /me`: identity plus the effective permission set."""

    permissions: list[str] = Field(default_factory=list)
    max_access_level: str
