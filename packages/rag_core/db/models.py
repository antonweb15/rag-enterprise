"""Declarative ORM models of the core (SQLAlchemy 2.0 typed mapping).

Schema source of truth is `core/storage_schema.md` §2. These models are an application layer
on top of the same schema created by the Alembic migrations (the migrations carry the
canonical DDL from the `.md`). Described here are the **tenant-scoped** tables of the primary
contour; the vendor backoffice contour (§2.2) is intentionally not modeled with the ORM — it
lives on the vendor infrastructure and is managed separately (see storage_schema.md §2.2).

All access levels are an integer scale 0..3 (0: Public, 1: Internal, 2: Confidential, 3: Strict).
"""

from __future__ import annotations

import datetime
import uuid

from pgvector.sqlalchemy import SPARSEVEC, Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# BGE-M3 vector dimensions (storage_schema.md §1): dense 1024, sparse dictionary 250002.
DENSE_DIM = 1024
SPARSE_DIM = 250002

# Canonical default license features (storage_schema.md §5, the `licenses.features` DDL default).
# Keep in sync with the DDL default and CHECK constraint in migration 0001_core_schema.
DEFAULT_LICENSE_FEATURES: tuple[str, ...] = (
    "hybrid_search",
    "s3_connector",
    "flashrank_rerank",
)


class Base(DeclarativeBase):
    """Common declarative base class for all core ORM models."""


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Tenant(Base):
    """Client/tenant (multi-tenancy for SaaS and Box). storage_schema.md §2.2."""

    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = _uuid_pk()
    slug: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    public_key_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    storage_type: Mapped[str] = mapped_column(Text, nullable=False, default="s3")
    s3_bucket: Mapped[str | None] = mapped_column(Text)
    s3_prefix: Mapped[str | None] = mapped_column(Text)
    allowed_origins: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    config: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class License(Base):
    """Tenant license/subscription (signed Ed25519 token). storage_schema.md §3."""

    __tablename__ = "licenses"
    __table_args__ = (
        CheckConstraint("plan IN ('standard', 'pro', 'enterprise_box')", name="ck_licenses_plan"),
        CheckConstraint(
            "mode IN ('online_subscription', 'airgap_enterprise')", name="ck_licenses_mode"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    license_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(Text, nullable=False, default="standard")
    mode: Mapped[str] = mapped_column(Text, nullable=False, default="online_subscription")
    issued_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    max_documents: Mapped[int] = mapped_column(Integer, nullable=False, default=1000)
    max_users: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    is_revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    hardware_id: Mapped[str | None] = mapped_column(Text)
    # Default must match the DDL `licenses.features` default (storage_schema.md §5), NOT `[]`.
    features: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=lambda: list(DEFAULT_LICENSE_FEATURES)
    )
    last_heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    # Vendor-contour fields (added additively by migration 0004_backoffice, nullable).
    issued_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class User(Base):
    """Channel/tenant user with an access level. storage_schema.md §2 item 4."""

    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "channel_user_id", name="uq_tenant_channel_user"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    channel_user_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="telegram")
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    user_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Document(Base):
    """Source document metadata (access_level source of truth). storage_schema.md §2 item 5."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("tenant_id", "content_hash", name="uq_tenant_content_hash"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    source_type: Mapped[str] = mapped_column(Text, nullable=False, default="s3")
    source_uri: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    error_message: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False, default="ru")
    ingestion_job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    doc_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    parents: Mapped[list[ParentChunk]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )


class ParentChunk(Base):
    """Parent chunk (LLM context, 800-1200 tokens). storage_schema.md §2 item 6."""

    __tablename__ = "parent_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunk_metadata: Mapped[dict[str, object]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="parents")
    children: Mapped[list[ChildChunk]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class ChildChunk(Base):
    """Child chunk (precise dense/sparse search, 150-250 tokens). storage_schema.md §2 item 7."""

    __tablename__ = "child_chunks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    parent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("parent_chunks.id", ondelete="CASCADE"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DENSE_DIM))
    sparse_vector: Mapped[object | None] = mapped_column(SPARSEVEC(SPARSE_DIM))
    embedding_model_id: Mapped[str] = mapped_column(Text, nullable=False, default="BAAI/bge-m3")
    embedding_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    parent: Mapped[ParentChunk] = relationship(back_populates="children")


class Conversation(Base):
    """Dialog session. storage_schema.md §2 item 8."""

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="telegram")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan"
    )


class Message(Base):
    """Dialog message + feedback mark. storage_schema.md §2 item 9."""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    feedback: Mapped[int | None] = mapped_column(Integer)
    feedback_comment: Mapped[str | None] = mapped_column(Text)
    langfuse_trace_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class StorageCredential(Base):
    """Encrypted (AES-GCM) tenant storage credentials. storage_schema.md §2 item 10."""

    __tablename__ = "storage_credentials"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "bucket", name="uq_storage_credentials_tenant_provider"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(Text, nullable=False, default="s3")
    endpoint_url: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text, default="auto")
    bucket: Mapped[str | None] = mapped_column(Text)
    prefix: Mapped[str | None] = mapped_column(Text)
    access_key_id_enc: Mapped[bytes | None] = mapped_column()
    secret_access_key_enc: Mapped[bytes | None] = mapped_column()
    encryption_key_id: Mapped[str | None] = mapped_column(Text)
    nonce: Mapped[bytes | None] = mapped_column()
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class AuditLog(Base):
    """Tenant administrators action log (private contour). storage_schema.md §2 item 11."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE")
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    actor_email: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Group(Base):
    """Principal group (enterprise_acl). storage_schema.md §2.4 item 20."""

    __tablename__ = "groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class UserGroup(Base):
    """User membership in groups (m2m). storage_schema.md §2.4 item 21."""

    __tablename__ = "user_groups"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Collection(Base):
    """Collection (knowledge base/folder, with hierarchy). storage_schema.md §2.4 item 22."""

    __tablename__ = "collections"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_collections_tenant_name"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    access_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class DocumentCollection(Base):
    """Document-to-collection binding (m2m). storage_schema.md §2.4 item 23."""

    __tablename__ = "document_collections"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True
    )
    collection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("collections.id", ondelete="CASCADE"), primary_key=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )


class DocumentAcl(Base):
    """Fine-grained ACL "principal -> document" (enterprise_acl). storage_schema.md §2.4 item 24."""

    __tablename__ = "document_acl"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "document_id",
            "principal_type",
            "principal_id",
            "permission",
            name="uq_document_acl",
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(Text, nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    permission: Mapped[str] = mapped_column(Text, nullable=False, default="read")
    granted_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
