"""Application settings.

Single source of truth for configuration. Everything is environment driven
(12-factor) so the exact same image runs locally, in CI and on ECS/Fargate --
only the injected environment differs. In AWS the secret-bearing values come
from Secrets Manager via the ECS task definition, never from a baked-in file.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "test", "staging", "production"]
LLMProvider = Literal["auto", "openai", "fake"]
StorageBackend = Literal["local", "s3"]
RerankerProvider = Literal["none", "cohere"]


class Settings(BaseSettings):
    """Typed, validated application configuration."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "fahem"
    environment: Environment = "local"
    debug: bool = False
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "json"
    api_v1_prefix: str = "/api/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # --- Database ------------------------------------------------------------
    database_url: PostgresDsn = Field(
        default="postgresql+psycopg://research:research@localhost:5432/research",  # type: ignore[assignment]
    )
    database_readonly_url: PostgresDsn | None = None
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_echo: bool = False

    # --- Redis ---------------------------------------------------------------
    redis_url: RedisDsn = Field(default="redis://localhost:6379/0")  # type: ignore[assignment]
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"
    cache_ttl_seconds: int = 900

    # --- Auth ----------------------------------------------------------------
    # Dev-only defaults; `validate_for_environment()` rejects them in production.
    jwt_secret: str = "dev-only-insecure-secret-change-me"  # noqa: S105
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60
    refresh_token_expire_minutes: int = 60 * 24 * 7

    # --- LLM -----------------------------------------------------------------
    llm_provider: LLMProvider = "auto"
    openai_api_key: str = ""
    openai_base_url: str = ""
    llm_reasoning_model: str = "gpt-5"
    llm_fast_model: str = "gpt-5-mini"
    llm_judge_model: str = "gpt-5"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_temperature: float = 0.1

    # --- Embeddings ----------------------------------------------------------
    embedding_model: str = "text-embedding-3-large"
    embedding_dim: int = 1536
    embedding_batch_size: int = 64

    # --- Retrieval -----------------------------------------------------------
    rag_top_k: int = 8
    rag_candidate_k: int = 24
    rag_min_similarity: float = 0.05
    reranker_provider: RerankerProvider = "none"
    cohere_api_key: str = ""

    # --- Agent bounds --------------------------------------------------------
    max_agent_iterations: int = 3
    max_tool_calls: int = 8
    max_runtime_seconds: float = 90.0
    agent_require_approval_for_sensitive: bool = True

    # --- Ingestion -----------------------------------------------------------
    chunk_size_tokens: int = 550
    chunk_overlap_tokens: int = 80
    max_upload_mb: int = 25
    allowed_upload_types: list[str] = Field(default_factory=lambda: ["application/pdf"])

    # --- Storage -------------------------------------------------------------
    storage_backend: StorageBackend = "local"
    local_storage_path: str = "./storage"
    s3_bucket: str = "fahem-documents"
    s3_endpoint_url: str = ""
    aws_region: str = "eu-west-1"

    # --- External services ---------------------------------------------------
    clinical_trials_api_url: str = "http://localhost:8001"
    clinical_trials_api_key: str = "demo-clinical-api-key"
    external_http_timeout_seconds: float = 10.0
    external_http_max_retries: int = 3

    # --- MCP -----------------------------------------------------------------
    mcp_server_host: str = "0.0.0.0"
    mcp_server_port: int = 8020
    mcp_server_url: str = "http://localhost:8020/mcp"
    mcp_auth_token: str = "dev-only-mcp-token-change-me"  # noqa: S105
    mcp_enabled: bool = True
    #: Host header allowlist for the MCP server's DNS-rebinding protection.
    #: Must include every hostname clients use to reach it (the compose service
    #: name, localhost, and the ALB/service DNS name in AWS).
    mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: [
            "localhost:8020",
            "127.0.0.1:8020",
            "mcp:8020",
        ]
    )

    # --- Rate limiting -------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_requests: int = 60
    rate_limit_window_seconds: int = 60
    agent_rate_limit_requests: int = 10
    agent_rate_limit_window_seconds: int = 60

    # --- Observability -------------------------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: str = ""
    langsmith_project: str = "fahem"
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""

    # --- Seed ----------------------------------------------------------------
    seed_default_password: str = "Research123!"  # noqa: S105 - local seed accounts only

    # ------------------------------------------------------------------ hooks
    @field_validator("cors_origins", "allowed_upload_types", "mcp_allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        """Accept both a JSON list and a plain comma-separated string."""
        if isinstance(value, str) and not value.strip().startswith("["):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("jwt_secret")
    @classmethod
    def _reject_default_secret_in_prod(cls, value: str, info: object) -> str:
        # Full cross-field check happens in `validate_for_environment`; this is
        # only a cheap guard against an obviously empty secret.
        if not value:
            raise ValueError("JWT_SECRET must not be empty")
        return value

    # ------------------------------------------------------------- properties
    @property
    def sync_database_url(self) -> str:
        """psycopg sync DSN - used by Alembic, Celery and the LangGraph saver."""
        return str(self.database_url)

    @property
    def async_database_url(self) -> str:
        """psycopg async DSN - used by the FastAPI request path."""
        return str(self.database_url)

    @property
    def psycopg_dsn(self) -> str:
        """Bare libpq DSN (no SQLAlchemy dialect prefix)."""
        return str(self.database_url).replace("postgresql+psycopg://", "postgresql://")

    @property
    def readonly_psycopg_dsn(self) -> str:
        url = (
            str(self.database_readonly_url)
            if self.database_readonly_url
            else str(self.database_url)
        )
        return url.replace("postgresql+psycopg://", "postgresql://")

    @property
    def use_real_openai(self) -> bool:
        """Whether calls should go to the real OpenAI API."""
        if self.llm_provider == "fake":
            return False
        if self.llm_provider == "openai":
            return True
        return bool(self.openai_api_key)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def validate_for_environment(self) -> list[str]:
        """Return a list of production-readiness problems (empty == fine).

        Called at startup: in production these are fatal, locally they are
        logged as warnings so the demo still runs.
        """
        problems: list[str] = []
        if "change-me" in self.jwt_secret or "dev-only" in self.jwt_secret:
            problems.append("JWT_SECRET is still the development default")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET should be at least 32 characters")
        if "dev-only" in self.mcp_auth_token:
            problems.append("MCP_AUTH_TOKEN is still the development default")
        if self.debug:
            problems.append("DEBUG must be false in production")
        if self.storage_backend == "local":
            problems.append("STORAGE_BACKEND=local is not durable; use s3")
        if self.database_readonly_url is None:
            problems.append("DATABASE_READONLY_URL is unset; SQL tools would run as owner")
        return problems


@functools.lru_cache
def get_settings() -> Settings:
    """Cached settings accessor (also the FastAPI dependency)."""
    return Settings()


settings = get_settings()
