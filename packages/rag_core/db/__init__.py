"""Storage layer of the `rag_core` core (Stage B - DB schema, migrations, RLS).

Source of truth for DDL/policies is `core/storage_schema.md`. Published here:
- declarative ORM models (`models`) on top of SQLAlchemy 2.0;
- tenant/access-level isolation contract via the `SET LOCAL` GUC
  (`session`, storage_schema.md §4.0) - a mandatory condition for RLS to work with a
  connection pool.

Alembic migrations (additive-only, storage_schema.md §5) live in `migrations/`.
"""

from __future__ import annotations

from rag_core.db.models import (
    DEFAULT_LICENSE_FEATURES,
    DENSE_DIM,
    SPARSE_DIM,
    AuditLog,
    Base,
    ChildChunk,
    Collection,
    Conversation,
    Document,
    DocumentAcl,
    DocumentCollection,
    Group,
    License,
    Message,
    ParentChunk,
    StorageCredential,
    Tenant,
    User,
    UserGroup,
)
from rag_core.db.session import (
    TENANT_GUC,
    USER_ACCESS_LEVEL_GUC,
    register_tenant_guc_listener,
    tenant_session_scope,
)

__all__ = [
    "DEFAULT_LICENSE_FEATURES",
    "DENSE_DIM",
    "SPARSE_DIM",
    "TENANT_GUC",
    "USER_ACCESS_LEVEL_GUC",
    "AuditLog",
    "Base",
    "ChildChunk",
    "Collection",
    "Conversation",
    "Document",
    "DocumentAcl",
    "DocumentCollection",
    "Group",
    "License",
    "Message",
    "ParentChunk",
    "StorageCredential",
    "Tenant",
    "User",
    "UserGroup",
    "register_tenant_guc_listener",
    "tenant_session_scope",
]
