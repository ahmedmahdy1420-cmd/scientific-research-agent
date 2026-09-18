"""SQLAlchemy models.

Imported as a package so that `Base.metadata` is fully populated before Alembic
autogenerate or `create_all` runs.
"""

from app.database.base import Base
from app.models.agent import AgentMessage, AgentRun, ApprovalRequest, ToolAuditLog
from app.models.document import EMBEDDING_DIM, Document, DocumentChunk
from app.models.enums import (
    AccessLevel,
    AgentRunStatus,
    ApprovalStatus,
    DocumentType,
    EvidenceStrength,
    ExperimentStatus,
    IngestionStatus,
    MessageRole,
    QuestionCategory,
    RoleName,
    ToolCallStatus,
    TrialPhase,
    TrialStatus,
)
from app.models.evaluation import EvaluationCase, EvaluationResult
from app.models.research import (
    ClinicalTrial,
    Compound,
    Experiment,
    ExperimentResult,
    ResearchTopic,
)
from app.models.user import Role, User, user_roles

__all__ = [
    "EMBEDDING_DIM",
    "AccessLevel",
    "AgentMessage",
    "AgentRun",
    "AgentRunStatus",
    "ApprovalRequest",
    "ApprovalStatus",
    "Base",
    "ClinicalTrial",
    "Compound",
    "Document",
    "DocumentChunk",
    "DocumentType",
    "EvaluationCase",
    "EvaluationResult",
    "EvidenceStrength",
    "Experiment",
    "ExperimentResult",
    "ExperimentStatus",
    "IngestionStatus",
    "MessageRole",
    "QuestionCategory",
    "ResearchTopic",
    "Role",
    "RoleName",
    "ToolAuditLog",
    "ToolCallStatus",
    "TrialPhase",
    "TrialStatus",
    "User",
    "user_roles",
]
