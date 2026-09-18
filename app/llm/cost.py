"""Token pricing and cost estimation.

Prices are USD per 1M tokens and live in one table so that "what did this
answer cost?" is answerable per agent run, not just per month on an invoice.
Unknown models fall back to a conservative estimate and are logged, so a model
swap never silently stops being costed.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float = 0.0


# Indicative list prices; override via ops config when they change.
MODEL_PRICES: dict[str, ModelPrice] = {
    "gpt-5": ModelPrice(1.25, 10.00, 0.125),
    "gpt-5-mini": ModelPrice(0.25, 2.00, 0.025),
    "gpt-5-nano": ModelPrice(0.05, 0.40, 0.005),
    "gpt-4.1": ModelPrice(2.00, 8.00, 0.50),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, 0.10),
    "gpt-4o": ModelPrice(2.50, 10.00, 1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, 0.075),
    "text-embedding-3-large": ModelPrice(0.13, 0.0),
    "text-embedding-3-small": ModelPrice(0.02, 0.0),
}

_FALLBACK = ModelPrice(2.50, 10.00)
_warned: set[str] = set()


def _price_for(model: str) -> ModelPrice:
    if model in MODEL_PRICES:
        return MODEL_PRICES[model]
    # Tolerate dated snapshots such as "gpt-5-2026-01-01".
    for known, price in MODEL_PRICES.items():
        if model.startswith(known):
            return price
    if model not in _warned:
        _warned.add(model)
        log.warning("cost.unknown_model", model=model, using="fallback_price")
    return _FALLBACK


def estimate_cost(
    model: str, prompt_tokens: int, completion_tokens: int, cached_prompt_tokens: int = 0
) -> float:
    """USD cost of a single call. Cached prompt tokens are billed lower."""
    price = _price_for(model)
    billable_prompt = max(prompt_tokens - cached_prompt_tokens, 0)
    return round(
        (billable_prompt / 1_000_000) * price.input_per_million
        + (cached_prompt_tokens / 1_000_000) * price.cached_input_per_million
        + (completion_tokens / 1_000_000) * price.output_per_million,
        8,
    )


def approx_tokens(text: str) -> int:
    """Rough token count for budgeting before a call.

    ~4 characters per token for English prose. Deliberately not tiktoken: an
    exact count is not worth a heavyweight dependency for a pre-flight budget
    check, and real usage numbers come back from the API anyway.
    """
    return max(1, len(text) // 4)
