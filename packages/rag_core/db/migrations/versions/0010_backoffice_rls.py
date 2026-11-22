"""enable RLS on backoffice tables and alembic_version

Canonical DDL - vendor backoffice tables isolation (Supabase rls_disabled_in_public remediation).
Fail-closed: service_role only, no anon/authenticated client policies.

Revision ID: 0010_backoffice_rls
Revises: 0009_admin_users
Create Date: 2026-11-22
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_backoffice_rls"
down_revision: str | None = "0009_admin_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKOFFICE_TABLES = (
    "customers",
    "backoffice_users",
    "orders",
    "activation_codes",
    "box_deployments",
    "backoffice_audit_log",
)


def upgrade() -> None:
    for table in _BACKOFFICE_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        op.execute(f"DROP POLICY IF EXISTS service_all_{table} ON {table};")
        op.execute(
            f"CREATE POLICY service_all_{table} ON {table} FOR ALL TO service_role USING (true);"
        )

    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version') THEN
            ALTER TABLE alembic_version ENABLE ROW LEVEL SECURITY;
            DROP POLICY IF EXISTS service_all_alembic_version ON alembic_version;
            CREATE POLICY service_all_alembic_version ON alembic_version FOR ALL TO service_role USING (true);
        END IF;
    END $$;
    """)


def downgrade() -> None:
    for table in _BACKOFFICE_TABLES:
        op.execute(f"DROP POLICY IF EXISTS service_all_{table} ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
    op.execute("""
    DO $$
    BEGIN
        IF EXISTS (SELECT FROM pg_tables WHERE schemaname = 'public' AND tablename = 'alembic_version') THEN
            DROP POLICY IF EXISTS service_all_alembic_version ON alembic_version;
            ALTER TABLE alembic_version DISABLE ROW LEVEL SECURITY;
        END IF;
    END $$;
    """)
