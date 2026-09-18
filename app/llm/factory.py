"""LLM client construction and dependency injection.

`LLM_PROVIDER=auto` (the default) picks the real OpenAI client when an API key
is present and the deterministic offline client when it is not. That single
rule is what lets `docker compose up` work with an empty `.env` while a real
key silently upgrades every code path.
"""

from __future__ import annotations

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.llm.base import LLMClient
from app.llm.deterministic import DeterministicLLMClient
from app.llm.openai_client import OpenAILLMClient

log = get_logger(__name__)

_client: LLMClient | None = None


def build_llm_client(settings: Settings | None = None) -> LLMClient:
    """Construct a fresh client according to configuration."""
    cfg = settings or get_settings()
    if cfg.use_real_openai:
        if not cfg.openai_api_key:
            log.warning("llm.provider_openai_without_key", hint="set OPENAI_API_KEY")
        log.info(
            "llm.provider_selected",
            provider="openai",
            reasoning_model=cfg.llm_reasoning_model,
            fast_model=cfg.llm_fast_model,
            embedding_model=cfg.embedding_model,
        )
        return OpenAILLMClient(cfg)

    log.info(
        "llm.provider_selected",
        provider="deterministic",
        reason="no OPENAI_API_KEY configured" if cfg.llm_provider == "auto" else "forced by config",
    )
    return DeterministicLLMClient(cfg)


def get_llm_client() -> LLMClient:
    """Process-wide singleton (FastAPI dependency and Celery task helper)."""
    global _client
    if _client is None:
        _client = build_llm_client()
    return _client


def set_llm_client(client: LLMClient | None) -> None:
    """Override the singleton. Used by tests to inject a scripted client."""
    global _client
    _client = client


async def close_llm_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
