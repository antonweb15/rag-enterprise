"""admin_users table (fastapi-users auth and RBAC for admin panel)

Canonical DDL - core/admin_panel.md §4, core/storage_schema.md §2.
Additive-only migration.

Revision ID: 0009_admin_users
Revises: 0008_schema_grants
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_admin_users"
down_revision: str | None = "0008_schema_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS admin_users (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        email TEXT NOT NULL UNIQUE,
        hashed_password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'viewer' CHECK (role IN ('admin', 'operator', 'viewer')),
        is_active BOOLEAN NOT NULL DEFAULT true,
        is_superuser BOOLEAN NOT NULL DEFAULT false,
        is_verified BOOLEAN NOT NULL DEFAULT true,
        totp_secret TEXT,
        totp_enabled BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_admin_users_tenant_id ON admin_users(tenant_id);
    CREATE INDEX IF NOT EXISTS idx_admin_users_email ON admin_users(email);

    ALTER TABLE admin_users ENABLE ROW LEVEL SECURITY;

    DROP POLICY IF EXISTS service_all_admin_users ON admin_users;
    CREATE POLICY service_all_admin_users ON admin_users FOR ALL TO service_role USING (true);

    DROP POLICY IF EXISTS tenant_isolation_admin_users ON admin_users;
    CREATE POLICY tenant_isolation_admin_users ON admin_users
        FOR ALL TO authenticated, anon
        USING (tenant_id IS NULL OR tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY IF EXISTS tenant_isolation_admin_users ON admin_users;
    DROP POLICY IF EXISTS service_all_admin_users ON admin_users;
    DROP TABLE IF EXISTS admin_users CASCADE;
    """)
