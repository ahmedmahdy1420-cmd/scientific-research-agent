# syntax=docker/dockerfile:1
# =============================================================================
# One image, several roles. The api, celery worker, MCP server and the mock
# clinical-trials API are all the same artefact with a different command, so
# what CI tested is byte-for-byte what ECS runs.
# =============================================================================

# --- builder -----------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-dev.txt ./

# `dev` build arg installs test/lint tooling; production images leave it off.
ARG INSTALL_DEV=false
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && if [ "$INSTALL_DEV" = "true" ]; then \
         /opt/venv/bin/pip install -r requirements-dev.txt; \
       else \
         /opt/venv/bin/pip install -r requirements.txt; \
       fi

# --- runtime -----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser alembic.ini ./
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser mock_services ./mock_services
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser data ./data
COPY --chown=appuser:appuser pyproject.toml ./

RUN mkdir -p /app/storage && chown -R appuser:appuser /app/storage

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD curl -fsS http://localhost:8000/api/v1/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
