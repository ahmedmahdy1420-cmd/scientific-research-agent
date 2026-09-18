"""Agent run persistence: runs, messages, tool audit, approvals.

Everything the agent does is written down. This is what makes the system
auditable: for any answer you can reconstruct the plan, every tool call with
its arguments and latency, whether authorisation passed, what evidence was
retrieved, and what the verifier concluded.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AgentRunStatus, ApprovalStatus, MessageRole, ToolCallStatus

if TYPE_CHECKING:
    from app.models.user import User


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        Index("ix_agent_runs_user_id_created_at", "user_id", "created_at"),
        Index("ix_agent_runs_status_created_at", "status", "created_at"),
        CheckConstraint("iterations >= 0", name="iterations_non_negative"),
        CheckConstraint("tool_calls_used >= 0", name="tool_calls_non_negative"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    #: LangGraph checkpoint thread; how a paused run is resumed after approval.
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[AgentRunStatus] = mapped_column(
        Enum(AgentRunStatus, native_enum=False, length=24),
        default=AgentRunStatus.PENDING,
        nullable=False,
    )
    category: Mapped[str | None] = mapped_column(String(32))
    plan: Mapped[dict | None] = mapped_column(JSONB)
    final_answer: Mapped[str | None] = mapped_column(Text)
    #: The structured parts of the answer, kept alongside the prose so a stored
    #: run can be re-rendered (and re-evaluated) without re-running the graph.
    answer_confidence: Mapped[str | None] = mapped_column(String(16))
    answer_citations: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    answer_caveats: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    evidence: Mapped[list[dict]] = mapped_column(JSONB, default=list, nullable=False)
    verification: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    # -- budget / cost accounting -------------------------------------------
    iterations: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_calls_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    llm_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    models_used: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="agent_runs", lazy="joined")
    messages: Mapped[list[AgentMessage]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        lazy="noload",
        order_by="AgentMessage.sequence",
    )
    tool_calls: Mapped[list[ToolAuditLog]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )
    approvals: Mapped[list[ApprovalRequest]] = relationship(
        back_populates="run", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<AgentRun {self.id} {self.status}>"


class AgentMessage(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_messages"
    __table_args__ = (
        Index("ix_agent_messages_run_id_sequence", "run_id", "sequence", unique=True),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[MessageRole] = mapped_column(
        Enum(MessageRole, native_enum=False, length=16), nullable=False
    )
    #: Graph node that produced this message (classify / plan / analyze / ...).
    node: Mapped[str | None] = mapped_column(String(64))
    content: Mapped[str] = mapped_column(Text, default="")
    message_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)

    run: Mapped[AgentRun] = relationship(back_populates="messages", lazy="noload")


class ToolAuditLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per tool call the model *requested* - executed or not.

    Denied calls are recorded too; the audit trail is about intent as much as
    effect.
    """

    __tablename__ = "tool_audit_logs"
    __table_args__ = (
        Index("ix_tool_audit_logs_run_id_created_at", "run_id", "created_at"),
        Index("ix_tool_audit_logs_tool_name_status", "tool_name", "status"),
        Index("ix_tool_audit_logs_user_id_created_at", "user_id", "created_at"),
    )

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ToolCallStatus] = mapped_column(
        Enum(ToolCallStatus, native_enum=False, length=24), nullable=False
    )
    #: Validated arguments, after Pydantic parsing - not the raw model output.
    arguments: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    result_summary: Mapped[str | None] = mapped_column(Text)
    result_count: Mapped[int | None] = mapped_column(Integer)
    denial_reason: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    run: Mapped[AgentRun | None] = relationship(back_populates="tool_calls", lazy="noload")


class ApprovalRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Human-in-the-loop gate for sensitive actions."""

    __tablename__ = "approval_requests"
    __table_args__ = (
        Index("ix_approval_requests_status_created_at", "status", "created_at"),
        Index("ix_approval_requests_run_id", "run_id"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), default="high", nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        Enum(ApprovalStatus, native_enum=False, length=16),
        default=ApprovalStatus.PENDING,
        nullable=False,
    )
    decision_note: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    run: Mapped[AgentRun] = relationship(back_populates="approvals", lazy="noload")

    def __repr__(self) -> str:
        return f"<ApprovalRequest {self.action} {self.status}>"
