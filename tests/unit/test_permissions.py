"""Role, permission and access-level logic.

These assertions are the security model written down. If one of them starts
failing, a permission boundary moved.
"""

from __future__ import annotations

import pytest

from app.auth.permissions import (
    ROLE_PERMISSIONS,
    Permission,
    owns_or_can_read_any,
)
from app.models.enums import AccessLevel, RoleName
from tests.conftest import make_principal

pytestmark = pytest.mark.unit


class TestRoleMatrix:
    def test_researcher_cannot_read_restricted(self):
        p = make_principal(RoleName.RESEARCHER)
        assert p.max_access_level == AccessLevel.INTERNAL
        assert not p.can_read_access_level(AccessLevel.RESTRICTED)
        assert p.can_read_access_level(AccessLevel.INTERNAL)
        assert p.can_read_access_level(AccessLevel.PUBLIC)

    def test_senior_researcher_can_read_restricted(self):
        p = make_principal(RoleName.SENIOR_RESEARCHER)
        assert p.max_access_level == AccessLevel.RESTRICTED
        assert p.can_read_access_level(AccessLevel.RESTRICTED)

    def test_researcher_cannot_decide_approvals(self):
        assert not make_principal(RoleName.RESEARCHER).has_permission(Permission.APPROVALS_DECIDE)

    def test_senior_and_admin_can_decide_approvals(self):
        for role in (RoleName.SENIOR_RESEARCHER, RoleName.ADMIN):
            assert make_principal(role).has_permission(Permission.APPROVALS_DECIDE)

    def test_only_admin_has_sensitive_tools_permission(self):
        assert make_principal(RoleName.ADMIN).has_permission(Permission.TOOLS_SENSITIVE)
        for role in (RoleName.RESEARCHER, RoleName.SENIOR_RESEARCHER):
            assert not make_principal(role).has_permission(Permission.TOOLS_SENSITIVE)

    def test_roles_are_strictly_nested(self):
        """Each role is a superset of the one below it.

        Not a universal rule for RBAC, but it is the rule for *this* model, and
        breaking it accidentally would be a silent privilege change.
        """
        researcher = set(ROLE_PERMISSIONS[RoleName.RESEARCHER])
        senior = set(ROLE_PERMISSIONS[RoleName.SENIOR_RESEARCHER])
        admin = set(ROLE_PERMISSIONS[RoleName.ADMIN])
        assert researcher < senior < admin

    def test_allowed_access_levels_used_as_a_sql_filter(self):
        levels = make_principal(RoleName.RESEARCHER).allowed_access_levels
        assert AccessLevel.RESTRICTED not in levels
        assert set(levels) == {AccessLevel.PUBLIC, AccessLevel.INTERNAL}


class TestResourceOwnership:
    def test_owner_can_read_own_resource(self):
        p = make_principal(RoleName.RESEARCHER, user_id="u1")
        assert owns_or_can_read_any(p, "u1")

    def test_non_owner_cannot_read_others(self):
        p = make_principal(RoleName.RESEARCHER, user_id="u1")
        assert not owns_or_can_read_any(p, "u2")

    def test_admin_can_read_any(self):
        p = make_principal(RoleName.ADMIN, user_id="u1")
        assert owns_or_can_read_any(p, "u2")

    def test_missing_owner_denies_non_privileged(self):
        p = make_principal(RoleName.RESEARCHER, user_id="u1")
        assert not owns_or_can_read_any(p, None)
