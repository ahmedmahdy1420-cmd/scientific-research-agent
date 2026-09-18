"""Authentication and authorisation over HTTP."""

from __future__ import annotations

import pytest

from app.models.enums import RoleName
from tests.conftest import TEST_PASSWORD, requires_db

pytestmark = [pytest.mark.integration, requires_db]


class TestLogin:
    async def test_valid_credentials_return_a_token_pair(self, api_client, seed_roles_and_users):
        response = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "researcher@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["access_token"] and body["refresh_token"]
        assert body["token_type"] == "bearer"

    async def test_wrong_password_rejected(self, api_client, seed_roles_and_users):
        response = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "researcher@example.com", "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_failed"

    async def test_unknown_user_gives_the_same_error_as_a_wrong_password(
        self, api_client, seed_roles_and_users
    ):
        """User enumeration defence: the two cases must be indistinguishable."""
        unknown = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.com", "password": TEST_PASSWORD},
        )
        wrong = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "researcher@example.com", "password": "nope-nope-nope"},
        )
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"] == wrong.json()["error"]

    async def test_malformed_email_is_a_validation_error(self, api_client):
        response = await api_client.post(
            "/api/v1/auth/login", json={"email": "not-an-email", "password": TEST_PASSWORD}
        )
        assert response.status_code == 422

    async def test_short_password_rejected_before_any_lookup(self, api_client):
        response = await api_client.post(
            "/api/v1/auth/login", json={"email": "a@b.com", "password": "short"}
        )
        assert response.status_code == 422


class TestTokenUsage:
    async def test_me_returns_identity_and_permissions(self, api_client, auth_headers):
        response = await api_client.get("/api/v1/me", headers=auth_headers[RoleName.RESEARCHER])
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "researcher@example.com"
        assert "documents:read" in body["permissions"]
        assert "tools:sensitive" not in body["permissions"]
        assert body["max_access_level"] == "internal"

    async def test_admin_sees_elevated_permissions(self, api_client, auth_headers):
        body = (await api_client.get("/api/v1/me", headers=auth_headers[RoleName.ADMIN])).json()
        assert "tools:sensitive" in body["permissions"]
        assert body["max_access_level"] == "restricted"

    async def test_missing_token_rejected(self, api_client):
        response = await api_client.get("/api/v1/me")
        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    async def test_garbage_token_rejected(self, api_client):
        response = await api_client.get(
            "/api/v1/me", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    async def test_refresh_rotates_the_pair(self, api_client, seed_roles_and_users):
        login = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "researcher@example.com", "password": TEST_PASSWORD},
        )
        refresh_token = login.json()["refresh_token"]
        response = await api_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": refresh_token}
        )
        assert response.status_code == 200
        assert response.json()["access_token"] != login.json()["access_token"]

    async def test_access_token_cannot_be_used_as_a_refresh_token(
        self, api_client, seed_roles_and_users
    ):
        login = await api_client.post(
            "/api/v1/auth/login",
            json={"email": "researcher@example.com", "password": TEST_PASSWORD},
        )
        response = await api_client.post(
            "/api/v1/auth/refresh", json={"refresh_token": login.json()["access_token"]}
        )
        assert response.status_code == 401


class TestOpsEndpoints:
    async def test_health_is_unauthenticated(self, api_client):
        response = await api_client.get("/api/v1/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_ready_reports_dependency_state(self, api_client):
        response = await api_client.get("/api/v1/ready")
        assert response.status_code in (200, 503)
        assert "database" in response.json()["checks"]

    async def test_request_id_is_echoed(self, api_client):
        response = await api_client.get("/api/v1/health", headers={"X-Request-ID": "trace-me-123"})
        assert response.headers["X-Request-ID"] == "trace-me-123"

    async def test_security_headers_present(self, api_client):
        response = await api_client.get("/api/v1/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
