"""enterprise ACL: groups, user_groups, collections, document_collections, document_acl

Canonical DDL - core/storage_schema.md §2.4 (items 20-24). An optional layer behind the
`enterprise_acl`/`collections` feature flag; tables are additive-only, under tenant-RLS (§4).

Revision ID: 0003_enterprise_acl
Revises: 0002_access_cascade
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_enterprise_acl"
down_revision: str | None = "0002_access_cascade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 20. groups
    op.execute("""
    CREATE TABLE IF NOT EXISTS groups (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        access_level INTEGER NOT NULL DEFAULT 0,
        external_ref TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_groups_tenant_name UNIQUE (tenant_id, name)
    );
    CREATE INDEX IF NOT EXISTS idx_groups_tenant_id ON groups(tenant_id);
    """)

    # 21. user_groups (m2m)
    op.execute("""
    CREATE TABLE IF NOT EXISTS user_groups (
        user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (user_id, group_id)
    );
    CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_id);
    CREATE INDEX IF NOT EXISTS idx_user_groups_tenant ON user_groups(tenant_id);
    """)

    # 22. collections (hierarchy)
    op.execute("""
    CREATE TABLE IF NOT EXISTS collections (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        access_level INTEGER NOT NULL DEFAULT 0,
        parent_id UUID REFERENCES collections(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_collections_tenant_name UNIQUE (tenant_id, name)
    );
    CREATE INDEX IF NOT EXISTS idx_collections_tenant_id ON collections(tenant_id);
    """)

    # 23. document_collections (m2m)
    op.execute("""
    CREATE TABLE IF NOT EXISTS document_collections (
        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        PRIMARY KEY (document_id, collection_id)
    );
    CREATE INDEX IF NOT EXISTS idx_document_collections_collection ON document_collections(collection_id);
    """)

    # 24. document_acl (fine-grained ACL "principal -> document")
    op.execute("""
    CREATE TABLE IF NOT EXISTS document_acl (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        principal_type TEXT NOT NULL,
        principal_id UUID NOT NULL,
        permission TEXT NOT NULL DEFAULT 'read',
        granted_by UUID,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_document_acl UNIQUE (tenant_id, document_id, principal_type, principal_id, permission)
    );
    CREATE INDEX IF NOT EXISTS idx_document_acl_document ON document_acl(document_id);
    CREATE INDEX IF NOT EXISTS idx_document_acl_principal ON document_acl(tenant_id, principal_type, principal_id);
    """)


def downgrade() -> None:
    for table in (
        "document_acl",
        "document_collections",
        "collections",
        "user_groups",
        "groups",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
