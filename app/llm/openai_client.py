"""OpenAI implementation of `LLMClient`.

Resilience choices worth defending in review:

* The SDK's own retry is switched **off** (`max_retries=0`) and retries are
  driven by tenacity instead, so every attempt is counted, logged and attributed
  to an agent run. A retry you cannot see is a latency mystery later.
* Only transient classes retry: timeouts, 429, 5xx and connection errors.
  A 400 (bad request) retries zero times, because it will fail identically.
* `structured_output` uses the SDK's native Pydantic parsing. A schema
  violation or a refusal raises `LLMOutputValidationError` rather than being
  passed downstream half-valid.
* Some reasoning models reject sampling parameters. Rather than hard-coding a
  model allowlist that rots, a 400 naming an unsupported parameter triggers one
  retry with that parameter dropped.
"""

from __future__ import annotations

import time
from typing import Any, TypeVar

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from app.core.config import Settings, get_settings
from app.core.errors import (
    EmbeddingError,
    LLMError,
    LLMOutputValidationError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.core.logging import get_logger
from app.llm.base import (
    EmbeddingResponse,
    LLMClient,
    LLMResponse,
    Message,
    ParsedResult,
    ProposedToolCall,
    TaskKind,
    ToolCallResponse,
    ToolSchema,
    Usage,
)
from app.llm.cost import estimate_cost
from app.llm.router import ModelRouter

log = get_logger(__name__)

TModel = TypeVar("TModel", bound=BaseModel)

#: Exceptions worth trying again.
_TRANSIENT = (
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _classify(exc: Exception) -> LLMError:
    """Map an SDK exception onto one of our structured errors."""
    if isinstance(exc, openai.APITimeoutError):
        return LLMTimeoutError("The model did not respond in time")
    if isinstance(exc, openai.RateLimitError):
        return LLMRateLimitError("Model provider rate limit reached")
    if isinstance(exc, openai.AuthenticationError):
        err = LLMError("Model provider rejected our credentials")
        err.retryable = False
        return err
    if isinstance(exc, openai.BadRequestError):
        err = LLMError(f"Malformed request to the model provider: {exc}")
        err.retryable = False
        return err
    if isinstance(exc, openai.APIConnectionError):
        return LLMError("Could not reach the model provider")
    if isinstance(exc, openai.InternalServerError):
        return LLMError("Model provider returned a server error")
    return LLMError(f"Unexpected model provider error: {exc}")


def _is_unsupported_param(exc: Exception, param: str) -> bool:
    text = str(exc).lower()
    return (
        isinstance(exc, openai.BadRequestError)
        and param in text
        and ("unsupported" in text or "not supported" in text or "does not support" in text)
    )


class OpenAILLMClient(LLMClient):
    provider = "openai"

    def __init__(
        self,
        settings: Settings | None = None,
        router: ModelRouter | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._router = router or ModelRouter(self._settings)
        self._client = client or AsyncOpenAI(
            api_key=self._settings.openai_api_key or "missing-key",
            base_url=self._settings.openai_base_url or None,
            timeout=self._settings.llm_timeout_seconds,
            max_retries=0,  # tenacity owns retry so attempts stay observable
        )

    # ------------------------------------------------------------------ util
    async def _call_with_retry(self, fn: Any, *, op: str, model: str, **kwargs: Any) -> Any:
        """Run `fn(**kwargs)` with bounded exponential backoff."""
        attempts = max(1, self._settings.llm_max_retries)
        delay = 0.5
        last: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await fn(**kwargs)
            except _TRANSIENT as exc:
                last = exc
                if attempt == attempts:
                    break
                log.warning(
                    "llm.retry",
                    op=op,
                    model=model,
                    attempt=attempt,
                    max_attempts=attempts,
                    error=type(exc).__name__,
                    sleep_s=delay,
                )
                import asyncio

                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
            except openai.BadRequestError as exc:
                # One targeted recovery: drop a parameter the model rejects.
                for param in ("temperature", "top_p"):
                    if param in kwargs and _is_unsupported_param(exc, param):
                        log.info("llm.param_dropped", op=op, model=model, param=param)
                        kwargs.pop(param)
                        return await self._call_with_retry(fn, op=op, model=model, **kwargs)
                raise _classify(exc) from exc
            except Exception as exc:
                raise _classify(exc) from exc

        assert last is not None
        log.error("llm.failed", op=op, model=model, attempts=attempts, error=str(last))
        raise _classify(last) from last

    def _usage_from(self, response: Any, model: str, elapsed_ms: int) -> Usage:
        raw = getattr(response, "usage", None)
        prompt = int(getattr(raw, "prompt_tokens", 0) or 0)
        completion = int(getattr(raw, "completion_tokens", 0) or 0)
        cached = 0
        details = getattr(raw, "prompt_tokens_details", None)
        if details is not None:
            cached = int(getattr(details, "cached_tokens", 0) or 0)
        return Usage(
            model=model,
            prompt_tokens=prompt,
            completion_tokens=completion,
            cached_prompt_tokens=cached,
            latency_ms=elapsed_ms,
            cost_usd=estimate_cost(model, prompt, completion, cached),
        )

    # ------------------------------------------------------------------ chat
    async def chat(
        self,
        messages: list[Message],
        *,
        task: TaskKind = "reasoning",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        model = self._router.model_for(task)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_openai() for m in messages],
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
        }
        if max_tokens is not None:
            payload["max_completion_tokens"] = max_tokens

        started = time.perf_counter()
        response = await self._call_with_retry(
            self._client.chat.completions.create, op="chat", model=model, **payload
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        choice = response.choices[0]
        usage = self._usage_from(response, model, elapsed)
        log.info(
            "llm.call",
            op="chat",
            model=model,
            task=task,
            latency_ms=elapsed,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
        )
        return LLMResponse(
            content=choice.message.content or "",
            usage=usage,
            finish_reason=choice.finish_reason or "stop",
        )

    # ----------------------------------------------------- structured output
    async def structured_output(
        self,
        messages: list[Message],
        response_model: type[TModel],
        *,
        task: TaskKind = "fast",
        temperature: float | None = None,
    ) -> ParsedResult[TModel]:
        model = self._router.model_for(task)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_openai() for m in messages],
            "response_format": response_model,
            "temperature": self._settings.llm_temperature if temperature is None else temperature,
        }

        started = time.perf_counter()
        response = await self._call_with_retry(
            self._client.chat.completions.parse,
            op="structured_output",
            model=model,
            **payload,
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        usage = self._usage_from(response, model, elapsed)

        message = response.choices[0].message
        refusal = getattr(message, "refusal", None)
        if refusal:
            log.warning("llm.refused", model=model, schema=response_model.__name__)
            raise LLMOutputValidationError(
                "The model refused to produce the requested structure",
                details={"refusal": refusal, "schema": response_model.__name__},
            )

        parsed = getattr(message, "parsed", None)
        if parsed is None:
            # Last-ditch: parse the raw content ourselves so a transport-level
            # quirk does not lose an otherwise valid answer.
            try:
                parsed = response_model.model_validate_json(message.content or "")
            except (ValidationError, ValueError) as exc:
                raise LLMOutputValidationError(
                    f"Model output did not satisfy {response_model.__name__}",
                    details={"schema": response_model.__name__, "error": str(exc)[:500]},
                ) from exc

        log.info(
            "llm.call",
            op="structured_output",
            model=model,
            task=task,
            schema=response_model.__name__,
            latency_ms=elapsed,
            cost_usd=usage.cost_usd,
        )
        return ParsedResult(parsed=parsed, usage=usage)

    # ------------------------------------------------------------ tool calls
    async def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        task: TaskKind = "reasoning",
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        model = self._router.model_for(task)
        payload: dict[str, Any] = {
            "model": model,
            "messages": [m.to_openai() for m in messages],
            "tools": [t.to_openai() for t in tools],
            "tool_choice": tool_choice,
            "parallel_tool_calls": True,
        }

        started = time.perf_counter()
        response = await self._call_with_retry(
            self._client.chat.completions.create,
            op="generate_with_tools",
            model=model,
            **payload,
        )
        elapsed = int((time.perf_counter() - started) * 1000)

        choice = response.choices[0]
        proposed: list[ProposedToolCall] = []
        for call in choice.message.tool_calls or []:
            import json

            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                # Malformed arguments are dropped here; the tool layer would
                # reject them anyway, and logging the raw string helps debug.
                log.warning(
                    "llm.tool_args_unparsable",
                    tool=call.function.name,
                    raw=(call.function.arguments or "")[:200],
                )
                continue
            proposed.append(ProposedToolCall(id=call.id, name=call.function.name, arguments=args))

        usage = self._usage_from(response, model, elapsed)
        log.info(
            "llm.call",
            op="generate_with_tools",
            model=model,
            task=task,
            proposed_tools=[c.name for c in proposed],
            latency_ms=elapsed,
            cost_usd=usage.cost_usd,
        )
        return ToolCallResponse(
            content=choice.message.content or "",
            tool_calls=proposed,
            usage=usage,
            finish_reason=choice.finish_reason or "stop",
        )

    # ------------------------------------------------------------- embedding
    async def embed(self, texts: list[str], *, dimensions: int | None = None) -> EmbeddingResponse:
        if not texts:
            return EmbeddingResponse(vectors=[], usage=Usage(), dimensions=0)

        model = self._settings.embedding_model
        dims = dimensions or self._settings.embedding_dim
        payload: dict[str, Any] = {"model": model, "input": texts, "dimensions": dims}

        started = time.perf_counter()
        try:
            response = await self._call_with_retry(
                self._client.embeddings.create, op="embed", model=model, **payload
            )
        except LLMError as exc:
            raise EmbeddingError(f"Embedding call failed: {exc.message}") from exc
        elapsed = int((time.perf_counter() - started) * 1000)

        vectors = [item.embedding for item in response.data]
        raw_usage = getattr(response, "usage", None)
        prompt_tokens = int(getattr(raw_usage, "prompt_tokens", 0) or 0)
        usage = Usage(
            model=model,
            prompt_tokens=prompt_tokens,
            latency_ms=elapsed,
            cost_usd=estimate_cost(model, prompt_tokens, 0),
        )
        log.info(
            "llm.call", op="embed", model=model, count=len(texts), dims=dims, latency_ms=elapsed
        )
        return EmbeddingResponse(vectors=vectors, usage=usage, model=model, dimensions=dims)

    async def aclose(self) -> None:
        await self._client.close()
