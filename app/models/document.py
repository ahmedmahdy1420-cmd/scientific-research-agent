"""Documents and their embedded chunks (the RAG corpus)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import get_settings
from app.database.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from app.models.enums import AccessLevel, DocumentType, IngestionStatus

if TYPE_CHECKING:
    from app.models.user import User

#: Embedding width. Kept in one place so the ORM column, the Alembic migration
#: and the embedding provider can never drift apart.
EMBEDDING_DIM = get_settings().embedding_dim


class Document(UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("content_sha256", name="uq_documents_content_sha256"),
        # The filters the retriever actually uses, as one composite index.
        Index(
            "ix_documents_filters",
            "access_level",
            "research_area",
            "document_type",
            "publication_date",
        ),
        Index("ix_documents_status", "ingestion_status"),
        CheckConstraint("char_length(title) > 0", name="title_not_empty"),
    )

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Original upload filename; the storage key is separate and opaque.
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    #: Deduplication key - re-uploading the same bytes is a no-op.
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    document_type: Mapped[DocumentType] = mapped_column(
        Enum(DocumentType, native_enum=False, length=32),
        default=DocumentType.PAPER,
        nullable=False,
    )
    access_level: Mapped[AccessLevel] = mapped_column(
        Enum(AccessLevel, native_enum=False, length=16),
        default=AccessLevel.INTERNAL,
        nullable=False,
    )

    # -- bibliographic metadata (also used as retrieval filters) -------------
    authors: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)
    source: Mapped[str | None] = mapped_column(String(255))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    journal: Mapped[str | None] = mapped_column(String(255))
    publication_date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    research_area: Mapped[str | None] = mapped_column(String(128), index=True)
    abstract: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[list[str]] = mapped_column(JSONB, default=list, nullable=False)

    # -- ingestion state machine --------------------------------------------
    ingestion_status: Mapped[IngestionStatus] = mapped_column(
        Enum(IngestionStatus, native_enum=False, length=24),
        default=IngestionStatus.UPLOADED,
        nullable=False,
    )
    ingestion_error: Mapped[str | None] = mapped_column(Text)
    ingestion_task_id: Mapped[str | None] = mapped_column(String(128))
    ingestion_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    owner: Mapped[User | None] = relationship(
        back_populates="documents", lazy="joined", foreign_keys=[owner_id]
    )
    chunks: Mapped[list[DocumentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan", lazy="noload"
    )

    def __repr__(self) -> str:
        return f"<Document {self.title[:40]!r} {self.ingestion_status}>"


class DocumentChunk(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A retrievable passage plus its embedding.

    The vector index is HNSW with `vector_cosine_ops`: OpenAI embeddings are
    L2-normalised, so cosine distance is the right metric, and HNSW gives much
    better recall-at-latency than IVFFlat without needing a trained list count.
    """

    __tablename__ = "document_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_id_chunk_index"
        ),
        Index(
            "ix_document_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_document_chunks_document_id_chunk_index", "document_id", "chunk_index"),
        CheckConstraint("chunk_index >= 0", name="chunk_index_non_negative"),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    page_number: Mapped[int | None] = mapped_column(Integer)
    section: Mapped[str | None] = mapped_column(String(255))
    #: Denormalised copy of the parent's filters so similarity search can filter
    #: without a join on the hot path.
    chunk_metadata: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None] = mapped_column(String(128))

    document: Mapped[Document] = relationship(back_populates="chunks", lazy="joined")

    def __repr__(self) -> str:
        return f"<DocumentChunk {self.document_id}#{self.chunk_index}>"
