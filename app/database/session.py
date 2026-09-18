"""Engine and session management.

Two engines, on purpose:

* the **async** engine backs the FastAPI request path (`psycopg` async);
* the **sync** engine backs Celery workers, Alembic and scripts, where an event
  loop buys nothing.

A third, *read-only* connection (a separate Postgres role) is what the SQL tool
layer uses. That is a database-enforced guarantee, not an application promise:
even a hypothetical SQL injection through a tool cannot write.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_async_engine: AsyncEngine | None = None
_async_session_factory: async_sessionmaker[AsyncSession] | None = None
_sync_engine: Engine | None = None
_sync_session_factory: sessionmaker[Session] | None = None
_readonly_engine: Engine | None = None


def _register_vector(engine: Engine | AsyncEngine) -> None:
    """Teach psycopg about the pgvector types on every new connection."""
    from pgvector.psycopg import register_vector

    target = engine.sync_engine if isinstance(engine, AsyncEngine) else engine

    @event.listens_for(target, "connect")
    def _on_connect(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        try:
            register_vector(dbapi_conn)
        except Exception as exc:  # noqa: BLE001 - extension may not exist yet at migrate time
            log.debug("pgvector.register_skipped", error=str(exc))


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    global _async_engine
    if _async_engine is None:
        cfg = settings or get_settings()
        _async_engine = create_async_engine(
            cfg.async_database_url,
            echo=cfg.db_echo,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            pool_pre_ping=True,  # survives RDS failover / idle reaping
            pool_recycle=1800,
        )
        _register_vector(_async_engine)
    return _async_engine


def get_async_session_factory(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    global _async_session_factory
    if _async_session_factory is None:
        _async_session_factory = async_sessionmaker(
            bind=get_async_engine(settings),
            expire_on_commit=False,
            autoflush=False,
        )
    return _async_session_factory


def get_sync_engine(settings: Settings | None = None) -> Engine:
    global _sync_engine
    if _sync_engine is None:
        cfg = settings or get_settings()
        _sync_engine = create_engine(
            cfg.sync_database_url,
            echo=cfg.db_echo,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        _register_vector(_sync_engine)
    return _sync_engine


def get_sync_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    global _sync_session_factory
    if _sync_session_factory is None:
        _sync_session_factory = sessionmaker(bind=get_sync_engine(settings), expire_on_commit=False)
    return _sync_session_factory


def get_readonly_engine(settings: Settings | None = None) -> Engine:
    """Engine bound to the least-privilege role used by SQL tools."""
    global _readonly_engine
    if _readonly_engine is None:
        cfg = settings or get_settings()
        url = str(cfg.database_readonly_url or cfg.database_url)
        _readonly_engine = create_engine(
            url,
            echo=False,
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,
            # Belt and braces: even if the role were mis-provisioned, the
            # session itself refuses writes.
            execution_options={"postgresql_readonly": True},
        )
    return _readonly_engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Async transactional scope for non-request code paths."""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@contextmanager
def sync_session_scope() -> Iterator[Session]:
    """Sync transactional scope (Celery tasks, scripts)."""
    factory = get_sync_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


async def check_database_ready() -> bool:
    """Readiness probe: can we round-trip a query *and* is pgvector installed?"""
    try:
        async with get_async_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
            row = await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            return row.first() is not None
    except Exception as exc:  # noqa: BLE001
        log.warning("database.not_ready", error=str(exc))
        return False


async def dispose_engines() -> None:
    global _async_engine, _async_session_factory
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        _async_session_factory = None
