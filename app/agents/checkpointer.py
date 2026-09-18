"""LangGraph checkpointer lifecycle.

Postgres, not memory. A human-approval pause can last minutes or hours, and the
API process may be replaced by a deploy in between — on ECS it certainly will
be eventually. An in-memory saver would lose every pending run at that moment.

The saver owns its own psycopg connection pool rather than sharing SQLAlchemy's,
because LangGraph drives raw psycopg with `autocommit=True` and its own row
factory, which are not the settings the ORM wants.

`InMemorySaver` is used for tests and for any environment where Postgres is not
reachable, so the graph still runs.
"""

from __future__ import annotations

from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_saver: Any | None = None
_pool: Any | None = None


async def init_checkpointer(settings: Settings | None = None) -> Any:
    """Create (once) and migrate the Postgres checkpointer."""
    global _saver, _pool
    if _saver is not None:
        return _saver

    cfg = settings or get_settings()
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        _pool = AsyncConnectionPool(
            conninfo=cfg.psycopg_dsn,
            max_size=8,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
        )
        await _pool.open()
        saver = AsyncPostgresSaver(_pool)
        await saver.setup()  # creates the checkpoint tables if absent
        _saver = saver
        log.info("checkpointer.ready", backend="postgres")
    except Exception as exc:  # noqa: BLE001 - degrade rather than refuse to boot
        log.warning(
            "checkpointer.postgres_unavailable",
            error=str(exc),
            fallback="memory",
            impact="pending approvals will not survive a restart",
        )
        from langgraph.checkpoint.memory import InMemorySaver

        _saver = InMemorySaver()
    return _saver


def get_checkpointer() -> Any:
    """Return the active saver, creating an in-memory one if not initialised."""
    global _saver
    if _saver is None:
        from langgraph.checkpoint.memory import InMemorySaver

        _saver = InMemorySaver()
    return _saver


def set_checkpointer(saver: Any | None) -> None:
    """Test hook."""
    global _saver
    _saver = saver


async def close_checkpointer() -> None:
    global _saver, _pool
    _saver = None
    if _pool is not None:
        await _pool.close()
        _pool = None
