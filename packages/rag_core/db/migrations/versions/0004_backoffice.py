"""vendor backoffice (cross-tenant): customers, backoffice_users, orders, codes, deployments, audit

Canonical DDL - core/storage_schema.md §2.2 (items 13-19). The cross-tenant vendor contour is
OUTSIDE tenant-RLS: access only under the backoffice `service_role`, no client policies
(fail-closed). It is not deployed in the on_premise_box artifact.

Revision ID: 0004_backoffice
Revises: 0003_enterprise_acl
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_backoffice"
down_revision: str | None = "0003_enterprise_acl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 13. customers (legal entities; 1 customer : N tenants)
    op.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        legal_name TEXT NOT NULL,
        display_name TEXT,
        inn TEXT,
        contact_email TEXT NOT NULL,
        contact_phone TEXT,
        contract_number TEXT,
        status TEXT NOT NULL DEFAULT 'trial',
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # 14. backoffice_users (vendor staff)
    op.execute("""
    CREATE TABLE IF NOT EXISTS backoffice_users (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        email TEXT NOT NULL UNIQUE,
        hashed_password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'support',
        is_active BOOLEAN NOT NULL DEFAULT true,
        totp_secret_enc BYTEA,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """)

    # 15. orders
    op.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        plan TEXT NOT NULL,
        amount_minor BIGINT,
        currency TEXT NOT NULL DEFAULT 'RUB',
        status TEXT NOT NULL DEFAULT 'pending',
        external_provider TEXT,
        external_ref TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
    """)

    # 16. activation_codes
    op.execute("""
    CREATE TABLE IF NOT EXISTS activation_codes (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        code TEXT NOT NULL UNIQUE,
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
        plan TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'issued',
        redeemed_by_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_activation_codes_customer ON activation_codes(customer_id);
    CREATE INDEX IF NOT EXISTS idx_activation_codes_status ON activation_codes(status);
    """)

    # 17. box_deployments (registry of boxes)
    op.execute("""
    CREATE TABLE IF NOT EXISTS box_deployments (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
        tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
        image_version TEXT,
        delivered_at TIMESTAMPTZ,
        hardware_id TEXT,
        last_heartbeat_at TIMESTAMPTZ,
        grace_period_expires_at TIMESTAMPTZ,
        restricted_mode BOOLEAN NOT NULL DEFAULT false,
        telemetry_opt_out BOOLEAN NOT NULL DEFAULT true,
        observability_forward_enabled BOOLEAN NOT NULL DEFAULT false,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_box_deployments_customer ON box_deployments(customer_id);
    CREATE INDEX IF NOT EXISTS idx_box_deployments_hardware ON box_deployments(hardware_id);
    """)

    # 18. backoffice_audit_log (global cross-tenant vendor audit)
    op.execute("""
    CREATE TABLE IF NOT EXISTS backoffice_audit_log (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        actor_backoffice_user_id UUID REFERENCES backoffice_users(id) ON DELETE SET NULL,
        actor_email TEXT,
        action TEXT NOT NULL,
        impersonated_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
        object_type TEXT,
        object_id TEXT,
        ip_address INET,
        user_agent TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_backoffice_audit_created ON backoffice_audit_log(created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_backoffice_audit_impersonated ON backoffice_audit_log(impersonated_tenant_id);
    """)

    # 19. Extend licenses with vendor fields (additive, nullable).
    op.execute("""
    ALTER TABLE licenses ADD COLUMN IF NOT EXISTS issued_by UUID REFERENCES backoffice_users(id);
    ALTER TABLE licenses ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id);
    ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
    ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revocation_reason TEXT;
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE licenses DROP COLUMN IF EXISTS revocation_reason;")
    op.execute("ALTER TABLE licenses DROP COLUMN IF EXISTS revoked_at;")
    op.execute("ALTER TABLE licenses DROP COLUMN IF EXISTS customer_id;")
    op.execute("ALTER TABLE licenses DROP COLUMN IF EXISTS issued_by;")
    for table in (
        "backoffice_audit_log",
        "box_deployments",
        "activation_codes",
        "orders",
        "backoffice_users",
        "customers",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
