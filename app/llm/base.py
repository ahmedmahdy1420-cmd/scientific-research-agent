"""Provider-agnostic LLM interface.

Everything in the application talks to `LLMClient`, never to `openai` directly.
That is what makes "swap in Anthropic/Bedrock/a local model" a one-file change,
and it is what lets the entire test suite and the offline demo run without a
network call or an API key.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import Any, Literal, TypeVar

from pydantic import BaseModel

TModel = TypeVar("TModel", bound=BaseModel)

#: Logical task -> used by ModelRouter to pick a concrete model.
TaskKind = Literal["reasoning", "fast", "judge", "embedding"]

Role = Literal["system", "user", "assistant", "tool"]


@dataclasses.dataclass(slots=True)
class Message:
    role: Role
    content: str
    name: str | None = None
    tool_call_id: str | None = None

    def to_openai(self) -> dict[str, Any]:
        msg: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            msg["name"] = self.name
        if self.tool_call_id:
            msg["tool_call_id"] = self.tool_call_id
        return msg


@dataclasses.dataclass(slots=True)
class Usage:
    """Token and cost accounting for one model call."""

    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            model=other.model or self.model,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            cached_prompt_tokens=self.cached_prompt_tokens + other.cached_prompt_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclasses.dataclass(slots=True)
class LLMResponse:
    content: str
    usage: Usage
    finish_reason: str = "stop"
    raw: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(slots=True)
class ParsedResult[TModel: BaseModel]:
    """Carrier for a structured-output call: the validated model plus usage."""

    parsed: TModel
    usage: Usage
    refusal: str | None = None


@dataclasses.dataclass(slots=True)
class ToolSchema:
    """A tool as advertised to the model. Derived from a Pydantic model."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclasses.dataclass(slots=True)
class ProposedToolCall:
    """A tool call the model *asked* for. Nothing has executed yet."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclasses.dataclass(slots=True)
class ToolCallResponse:
    content: str
    tool_calls: list[ProposedToolCall]
    usage: Usage
    finish_reason: str = "stop"


@dataclasses.dataclass(slots=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    usage: Usage
    model: str = ""
    dimensions: int = 0


class LLMClient(abc.ABC):
    """The contract every provider implements."""

    #: Human-readable provider id, surfaced in logs and run metadata.
    provider: str = "abstract"

    @abc.abstractmethod
    async def chat(
        self,
        messages: list[Message],
        *,
        task: TaskKind = "reasoning",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Free-text completion."""

    @abc.abstractmethod
    async def structured_output(
        self,
        messages: list[Message],
        response_model: type[TModel],
        *,
        task: TaskKind = "fast",
        temperature: float | None = None,
    ) -> ParsedResult[TModel]:
        """Completion constrained to a Pydantic schema.

        Implementations must raise `LLMOutputValidationError` rather than
        returning a partially-valid object.
        """

    @abc.abstractmethod
    async def generate_with_tools(
        self,
        messages: list[Message],
        tools: list[ToolSchema],
        *,
        task: TaskKind = "reasoning",
        tool_choice: str = "auto",
    ) -> ToolCallResponse:
        """Completion that may propose tool calls. It never executes them."""

    @abc.abstractmethod
    async def embed(self, texts: list[str], *, dimensions: int | None = None) -> EmbeddingResponse:
        """Embed a batch of texts."""

    async def aclose(self) -> None:  # pragma: no cover - default no-op
        return None
