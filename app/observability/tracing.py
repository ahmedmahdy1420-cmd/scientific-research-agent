"""Tracing abstraction.

Three backends behind one interface, chosen by environment:

* **LangSmith** when `LANGSMITH_TRACING=true` and a key is present. LangSmith is
  LLM-native: it records prompts, completions, token counts and the nesting of
  agent steps, which generic APM does not.
* **OpenTelemetry** when `OTEL_ENABLED=true`. This is what you want for the rest
  of the system (HTTP, database, external calls) and it exports to whatever the
  platform already runs — for the AWS setup here, the ADOT collector sidecar
  into CloudWatch/X-Ray.
* **Structured logs** otherwise — the default. Every span becomes a
  `span.started` / `span.finished` log event with a duration, so a local run is
  still fully traceable with no vendor, no key and no network.

That last point is the requirement: local execution must work without a
commercial tracing service.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

from app.core.config import Settings, get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_tracer: Any = None
_initialised = False


def init_tracing(settings: Settings | None = None) -> None:
    """Configure tracing backends once at process start."""
    global _tracer, _initialised
    if _initialised:
        return
    cfg = settings or get_settings()

    if cfg.langsmith_tracing and cfg.langsmith_api_key:
        # The LangSmith SDK is driven entirely by environment variables, which
        # LangGraph and the OpenAI wrapper pick up automatically.
        import os

        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = cfg.langsmith_api_key
        os.environ["LANGSMITH_PROJECT"] = cfg.langsmith_project
        log.info("tracing.langsmith_enabled", project=cfg.langsmith_project)
    elif cfg.langsmith_tracing:
        log.warning("tracing.langsmith_requested_without_key", falling_back_to="logs")

    if cfg.otel_enabled:
        try:
            from opentelemetry import trace
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": cfg.app_name,
                        "deployment.environment": cfg.environment,
                    }
                )
            )
            if cfg.otel_exporter_otlp_endpoint:
                provider.add_span_processor(
                    BatchSpanProcessor(OTLPSpanExporter(endpoint=cfg.otel_exporter_otlp_endpoint))
                )
            trace.set_tracer_provider(provider)
            _tracer = trace.get_tracer(cfg.app_name)
            log.info("tracing.otel_enabled", endpoint=cfg.otel_exporter_otlp_endpoint or "none")
        except Exception as exc:  # noqa: BLE001 - tracing must never block boot
            log.warning("tracing.otel_failed", error=str(exc))

    _initialised = True


def instrument_fastapi(app: Any, settings: Settings | None = None) -> None:
    """Attach OpenTelemetry ASGI instrumentation when enabled."""
    cfg = settings or get_settings()
    if not cfg.otel_enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready,metrics")
        log.info("tracing.fastapi_instrumented")
    except Exception as exc:  # noqa: BLE001
        log.warning("tracing.fastapi_instrumentation_failed", error=str(exc))


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    """Synchronous span. Always emits a duration, whatever the backend."""
    span_id = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    payload: dict[str, Any] = {"span_id": span_id, **attributes}
    otel_span = _tracer.start_span(name) if _tracer is not None else None
    try:
        if otel_span is not None:
            for key, value in attributes.items():
                otel_span.set_attribute(key, str(value))
        yield payload
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        log.warning("span.failed", span=name, duration_ms=elapsed, error=str(exc)[:300], **payload)
        if otel_span is not None:
            otel_span.record_exception(exc)
        raise
    else:
        elapsed = int((time.perf_counter() - started) * 1000)
        log.debug("span.finished", span=name, duration_ms=elapsed, **payload)
    finally:
        if otel_span is not None:
            otel_span.end()


@asynccontextmanager
async def aspan(name: str, **attributes: Any) -> AsyncIterator[dict[str, Any]]:
    """Async span with the same semantics."""
    with span(name, **attributes) as payload:
        yield payload
