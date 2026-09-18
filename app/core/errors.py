"""Structured application errors.

Every failure mode the agent can hit has an explicit type and a stable
machine-readable `code`. Nothing is swallowed: the API maps these onto HTTP
responses and the agent maps them onto graph failure states, so a caller can
always tell *which* stage failed (LLM, tool, retrieval, database, auth).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    code: str = "internal_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


# --- Auth --------------------------------------------------------------------
class AuthenticationError(AppError):
    code = "authentication_failed"
    http_status = 401


class InvalidTokenError(AuthenticationError):
    code = "invalid_token"


class AuthorizationError(AppError):
    code = "authorization_failed"
    http_status = 403


class ResourceAccessDeniedError(AuthorizationError):
    code = "resource_access_denied"


# --- Input -------------------------------------------------------------------
class ValidationError(AppError):
    code = "validation_error"
    http_status = 422


class NotFoundError(AppError):
    code = "not_found"
    http_status = 404


class PayloadTooLargeError(AppError):
    code = "payload_too_large"
    http_status = 413


class RateLimitedError(AppError):
    code = "rate_limited"
    http_status = 429
    retryable = True


# --- LLM ---------------------------------------------------------------------
class LLMError(AppError):
    code = "llm_error"
    http_status = 502
    retryable = True


class LLMTimeoutError(LLMError):
    code = "llm_timeout"


class LLMRateLimitError(LLMError):
    code = "llm_rate_limited"


class LLMOutputValidationError(LLMError):
    """The model returned something that does not satisfy the Pydantic schema."""

    code = "llm_invalid_output"
    retryable = True


# --- Tools / external --------------------------------------------------------
class ToolError(AppError):
    code = "tool_error"
    http_status = 502


class ToolNotFoundError(ToolError):
    code = "tool_not_found"
    http_status = 404


class ToolTimeoutError(ToolError):
    code = "tool_timeout"
    retryable = True


class ToolInputValidationError(ToolError):
    code = "tool_input_invalid"
    http_status = 422


class ExternalServiceError(AppError):
    code = "external_service_error"
    http_status = 502
    retryable = True


class ExternalNotFoundError(AppError):
    """Upstream answered 404 - a real, non-retryable 'no such record'."""

    code = "external_not_found"
    http_status = 404


# --- Data --------------------------------------------------------------------
class DatabaseError(AppError):
    code = "database_error"
    http_status = 503
    retryable = True


class EmbeddingError(AppError):
    code = "embedding_error"
    http_status = 502
    retryable = True


class VectorSearchError(AppError):
    code = "vector_search_error"
    http_status = 503
    retryable = True


class UnsafeQueryError(AppError):
    """A generated SQL statement failed the safety gate."""

    code = "unsafe_query"
    http_status = 400


# --- Agent -------------------------------------------------------------------
class AgentError(AppError):
    code = "agent_error"
    http_status = 500


class AgentBudgetExceededError(AgentError):
    code = "agent_budget_exceeded"


class ApprovalRequiredError(AgentError):
    """The run paused: a human must approve before it can continue."""

    code = "approval_required"
    http_status = 202


class ApprovalRejectedError(AgentError):
    code = "approval_rejected"
    http_status = 403
