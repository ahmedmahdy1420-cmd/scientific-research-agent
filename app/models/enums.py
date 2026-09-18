"""Domain enumerations.

Stored as VARCHAR + CHECK constraint (`native_enum=False`) rather than native
Postgres ENUM types: adding a value to a native enum requires a migration that
cannot run inside a transaction, which is a poor fit for zero-downtime deploys.
"""

from __future__ import annotations

from enum import StrEnum


class RoleName(StrEnum):
    RESEARCHER = "researcher"
    SENIOR_RESEARCHER = "senior_researcher"
    ADMIN = "admin"


class AccessLevel(StrEnum):
    """Document sensitivity. Enforced in SQL, never by the model."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class DocumentType(StrEnum):
    PAPER = "paper"
    PREPRINT = "preprint"
    REVIEW = "review"
    CLINICAL_REPORT = "clinical_report"
    INTERNAL_REPORT = "internal_report"
    PROTOCOL = "protocol"


class IngestionStatus(StrEnum):
    UPLOADED = "uploaded"
    PROCESSING = "processing"
    EXTRACTED = "extracted"
    CHUNKED = "chunked"
    EMBEDDED = "embedded"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"
    BUDGET_EXCEEDED = "budget_exceeded"


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ToolCallStatus(StrEnum):
    REQUESTED = "requested"
    AUTHORIZED = "authorized"
    DENIED = "denied"
    EXECUTED = "executed"
    FAILED = "failed"
    TIMEOUT = "timeout"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class TrialPhase(StrEnum):
    PHASE_1 = "Phase 1"
    PHASE_2 = "Phase 2"
    PHASE_3 = "Phase 3"
    PHASE_4 = "Phase 4"
    NA = "N/A"


class TrialStatus(StrEnum):
    RECRUITING = "recruiting"
    ACTIVE = "active_not_recruiting"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    WITHDRAWN = "withdrawn"


class ExperimentStatus(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class EvidenceStrength(StrEnum):
    STRONG = "strong"
    MODERATE = "moderate"
    WEAK = "weak"
    INCONCLUSIVE = "inconclusive"


class QuestionCategory(StrEnum):
    """What the classifier decides; drives which branch of the graph runs."""

    LITERATURE = "literature"  # RAG over ingested documents
    CLINICAL_TRIALS = "clinical_trials"  # external REST API
    STRUCTURED_DATA = "structured_data"  # experiments / compounds via SQL tools
    COMPARISON = "comparison"  # multi-source, multi-step
    SENSITIVE_ACTION = "sensitive_action"  # requires human approval
    SMALL_TALK = "small_talk"  # answer directly, no tools
    OUT_OF_SCOPE = "out_of_scope"
