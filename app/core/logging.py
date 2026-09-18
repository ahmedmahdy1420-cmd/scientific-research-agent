"""Structured logging.

structlog emits key/value events. Locally they render as colourised console
lines; in staging/production they render as single-line JSON, which is what the
CloudWatch Logs `awslogs` driver ships and what CloudWatch Logs Insights can
query directly (`fields @timestamp, agent_run_id, tool_name | filter ...`).

A context-var bound request_id/user_id/agent_run_id is attached to every event
emitted while handling a request, so one run can be reconstructed from logs
without a tracing backend.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
from structlog.contextvars import (
    bind_contextvars,
    clear_contextvars,
    merge_contextvars,
    unbind_contextvars,
)

_CONFIGURED = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Configure structlog + stdlib logging once per process."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    shared: list[Any] = [
        merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    if fmt == "json":
        shared.append(structlog.processors.format_exc_info)
    else:
        shared.append(structlog.processors.ExceptionPrettyPrinter())

    structlog.configure(
        processors=[*shared, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        # stdlib factory (not PrintLogger): `add_logger_name` needs a named
        # logger, and it routes uvicorn/celery records through the same handler.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper(), force=True)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "botocore", "urllib3", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


class LogContext:
    """Bind fields for the duration of a block, then restore.

    >>> with LogContext(agent_run_id=str(run.id)):
    ...     log.info("agent.started")
    """

    def __init__(self, **fields: Any) -> None:
        self._fields: MutableMapping[str, Any] = {k: v for k, v in fields.items() if v is not None}

    def __enter__(self) -> LogContext:
        bind_contextvars(**self._fields)
        return self

    def __exit__(self, *exc: object) -> None:
        unbind_contextvars(*self._fields.keys())


__all__ = [
    "LogContext",
    "bind_contextvars",
    "clear_contextvars",
    "configure_logging",
    "get_logger",
]
