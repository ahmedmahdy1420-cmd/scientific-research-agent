"""Shared test configuration and fixtures.

Two deliberate choices:

1. **Environment is set before any app import.** `Settings` is an
   `lru_cache`d singleton read at import time, so pointing the suite at a test
   database has to happen here, at the top of conftest, or the app would
   already have bound to the development one.

2. **No test needs an OpenAI key.** `LLM_PROVIDER=fake` forces the
   deterministic provider, so unit tests assert on the agent's *control flow*
   (which tools ran, whether authorisation held, whether ungrounded answers are
   caught) rather than on model prose. Tests that genuinely need a real model
   live in `tests/live/` and are skipped unless `RUN_LIVE_AI_TESTS=1`.
"""

from __future__ import annotations

import os
import uuid

# --- must precede every app import -------------------------------------------
_DEFAULT_DB = "postgresql+psycopg://research:research@localhost:5432/research"
_base_db = os.environ.get("DATABASE_URL", _DEFAULT_DB)
_test_db = os.environ.get("TEST_DATABASE_URL") or _base_db.rsplit("/", 1)[0] + "/research_test"

os.environ["DATABASE_URL"] = _test_db
os.environ["DATABASE_READONLY_URL"] = _test_db
os.environ["ENVIRONMENT"] = "test"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["OPENAI_API_KEY"] = ""
os.environ["LOG_FORMAT"] = "console"
os.environ["LOG_LEVEL"] = "WARNING"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-validation-32"
os.environ["STORAGE_BACKEND"] = "local"
os.environ.setdefault("LOCAL_STORAGE_PATH", "/tmp/sra-test-storage")  # noqa: S108
os.environ["RATE_LIMIT_ENABLED"] = "false"
os.environ["MCP_ENABLED"] = os.environ.get("TEST_MCP_ENABLED", "false")
os.environ["EMBEDDING_DIM"] = "1536"
os.environ["MAX_AGENT_ITERATIONS"] = "2"
os.environ["MAX_TOOL_CALLS"] = "6"

import pytest  # noqa: E402
import sqlalchemy as sa  # noqa: E402

from app.auth.permissions import (  # noqa: E402
    ROLE_DESCRIPTIONS,
    ROLE_PERMISSIONS,
    Principal,
)
from app.core.config import get_settings  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.enums import RoleName  # noqa: E402

settings = get_settings()

DB_AVAILABLE = False
DB_SKIP_REASON = "postgres not reachable; start it with `docker compose up -d postgres`"


def _admin_dsn() -> str:
    """DSN for the maintenance connection used to create the test database."""
    return (
        _base_db.replace("postgresql+psycopg://", "postgresql://").rsplit("/", 1)[0] + "/postgres"
    )


def _ensure_test_database() -> bool:
    """Create the test database and its extensions. Returns availability."""
    global DB_AVAILABLE
    try:
        import psycopg

        name = _test_db.rsplit("/", 1)[-1]
        with psycopg.connect(_admin_dsn(), autocommit=True, connect_timeout=5) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{name}"')

        dsn = _test_db.replace("postgresql+psycopg://", "postgresql://")
        with psycopg.connect(dsn, autocommit=True, connect_timeout=5) as conn:
            for ext in ("vector", "pg_trgm", "pgcrypto"):
                conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        DB_AVAILABLE = True
    except Exception:  # noqa: BLE001 - absence of a DB is a skip, not an error
        DB_AVAILABLE = False
    return DB_AVAILABLE


_ensure_test_database()

requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason=DB_SKIP_REASON)


# =============================================================================
# Schema
# =============================================================================
@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Create the schema once for the whole session, drop it afterwards."""
    if not DB_AVAILABLE:
        yield
        return

    from app.database.session import get_sync_engine
    from app.models import Base

    engine = get_sync_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables(_schema):
    """Truncate between tests so each one starts from a known state.

    TRUNCATE ... CASCADE is dramatically faster than drop/create per test and
    resets every foreign key in one statement.
    """
    if not DB_AVAILABLE:
        yield
        return

    from app.database.session import get_sync_engine
    from app.models import Base

    yield
    engine = get_sync_engine()
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with engine.begin() as conn:
        conn.execute(sa.text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


# =============================================================================
# Users and principals
# =============================================================================
TEST_PASSWORD = "TestPassword123!"


@pytest.fixture
def seed_roles_and_users(_clean_tables):
    """Insert one user per role. Returns {role_name: user_id}."""
    if not DB_AVAILABLE:
        pytest.skip(DB_SKIP_REASON)

    from app.database.session import sync_session_scope
    from app.models.user import Role, User

    ids: dict[str, uuid.UUID] = {}
    with sync_session_scope() as session:
        roles = {}
        for name, permissions in ROLE_PERMISSIONS.items():
            role = Role(
                name=name,
                description=ROLE_DESCRIPTIONS.get(name, ""),
                permissions=list(permissions),
            )
            session.add(role)
            roles[name] = role
        session.flush()

        for name in ROLE_PERMISSIONS:
            user = User(
                email=f"{name}@example.com",
                full_name=f"Test {name}",
                hashed_password=hash_password(TEST_PASSWORD),
                department="oncology",
                is_active=True,
            )
            user.roles.append(roles[name])
            session.add(user)
            session.flush()
            ids[name] = user.id
    return ids


def make_principal(role: str = RoleName.RESEARCHER, user_id: str | None = None) -> Principal:
    """Build a Principal without touching the database.

    Used by unit tests that exercise authorisation logic in isolation.
    """
    return Principal(
        user_id=user_id or str(uuid.uuid4()),
        email=f"{role}@example.com",
        full_name=f"Test {role}",
        roles=frozenset({role}),
        permissions=frozenset(ROLE_PERMISSIONS[role]),
        department="oncology",
    )


@pytest.fixture
def researcher() -> Principal:
    return make_principal(RoleName.RESEARCHER)


@pytest.fixture
def senior_researcher() -> Principal:
    return make_principal(RoleName.SENIOR_RESEARCHER)


@pytest.fixture
def admin() -> Principal:
    return make_principal(RoleName.ADMIN)


# =============================================================================
# LLM
# =============================================================================
@pytest.fixture
def deterministic_llm():
    from app.llm.deterministic import DeterministicLLMClient

    return DeterministicLLMClient(settings)


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Clear process-wide singletons so tests cannot leak state into each other."""
    from app.llm.factory import set_llm_client
    from app.tools import set_tool_registry

    set_llm_client(None)
    set_tool_registry(None)
    yield
    set_llm_client(None)
    set_tool_registry(None)


# =============================================================================
# HTTP client
# =============================================================================
@pytest.fixture
async def api_client():
    """ASGI client bound to the real app (no network, no running server)."""
    if not DB_AVAILABLE:
        pytest.skip(DB_SKIP_REASON)

    import httpx

    from app.main import create_app

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
async def auth_headers(api_client, seed_roles_and_users):
    """Bearer headers for each seeded role."""
    headers: dict[str, dict[str, str]] = {}
    for role in ROLE_PERMISSIONS:
        response = await api_client.post(
            "/api/v1/auth/login",
            json={"email": f"{role}@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        headers[role] = {"Authorization": f"Bearer {token}"}
    return headers
