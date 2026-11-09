"""RLS: enable + dual-contour isolation policies (tenant + access_level)

Canonical DDL - core/storage_schema.md §4.1. The secondary defense-in-depth contour:
- service_role (BYPASSRLS) - full access (workers/migrations/backoffice);
- authenticated/anon - isolation by app.current_tenant_id, and for documents/*_chunks also
  by app.user_access_level (fail-closed: a missing GUC -> empty result set / level 0);
- storage_credentials and audit_log - ONLY service_role (no client policies, fail-closed).

Policies are recreated idempotently (DROP POLICY IF EXISTS before CREATE), §5 item 5.

Revision ID: 0006_rls_policies
Revises: 0005_hybrid_search_rrf
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_rls_policies"
down_revision: str | None = "0005_hybrid_search_rrf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# All tables under RLS (core §4.1 + enterprise ACL §4.1 item 3).
_RLS_TABLES = (
    "tenants",
    "licenses",
    "users",
    "documents",
    "parent_chunks",
    "child_chunks",
    "conversations",
    "messages",
    "storage_credentials",
    "audit_log",
    "groups",
    "user_groups",
    "collections",
    "document_collections",
    "document_acl",
)

# GUC predicates (storage_schema.md §4.1).
_TENANT_PRED = "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid"
_LEVEL_PRED = (
    "access_level <= COALESCE(NULLIF(current_setting('app.user_access_level', true), ''), '0')::int"
)

# service_role policies (FOR ALL USING(true)) - for all RLS tables.
_SERVICE_POLICIES = {t: f"service_all_{t}" for t in _RLS_TABLES}

# Client isolation policies (tenant / tenant+level / special). None => service_role only.
_CLIENT_POLICIES: dict[str, tuple[str, str, str] | None] = {
    "tenants": (
        "tenant_isolation_tenants",
        "SELECT",
        "id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid",
    ),
    "licenses": ("tenant_isolation_licenses", "SELECT", _TENANT_PRED),
    "users": ("tenant_isolation_users", "ALL", _TENANT_PRED),
    "documents": (
        "tenant_isolation_documents",
        "ALL",
        f"{_TENANT_PRED}\n        AND {_LEVEL_PRED}",
    ),
    "parent_chunks": (
        "tenant_isolation_parent_chunks",
        "ALL",
        f"{_TENANT_PRED}\n        AND {_LEVEL_PRED}",
    ),
    "child_chunks": (
        "tenant_isolation_child_chunks",
        "ALL",
        f"{_TENANT_PRED}\n        AND {_LEVEL_PRED}",
    ),
    "conversations": ("tenant_isolation_conversations", "ALL", _TENANT_PRED),
    "messages": (
        "tenant_isolation_messages",
        "ALL",
        "conversation_id IN (SELECT id FROM conversations WHERE "
        "tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid)",
    ),
    # Fail-closed: service_role only.
    "storage_credentials": None,
    "audit_log": None,
    "groups": ("tenant_isolation_groups", "ALL", _TENANT_PRED),
    "user_groups": ("tenant_isolation_user_groups", "ALL", _TENANT_PRED),
    "collections": ("tenant_isolation_collections", "ALL", _TENANT_PRED),
    "document_collections": ("tenant_isolation_document_collections", "ALL", _TENANT_PRED),
    "document_acl": ("tenant_isolation_document_acl", "ALL", _TENANT_PRED),
}


def upgrade() -> None:
    for table in _RLS_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")

        service_policy = _SERVICE_POLICIES[table]
        op.execute(f"DROP POLICY IF EXISTS {service_policy} ON {table};")
        op.execute(
            f"CREATE POLICY {service_policy} ON {table} FOR ALL TO service_role USING (true);"
        )

        client = _CLIENT_POLICIES[table]
        if client is None:
            continue
        name, command, predicate = client
        op.execute(f"DROP POLICY IF EXISTS {name} ON {table};")
        op.execute(
            f"CREATE POLICY {name} ON {table}\n"
            f"    FOR {command} TO authenticated, anon\n"
            f"    USING ({predicate});"
        )


def downgrade() -> None:
    for table in _RLS_TABLES:
        client = _CLIENT_POLICIES[table]
        if client is not None:
            op.execute(f"DROP POLICY IF EXISTS {client[0]} ON {table};")
        op.execute(f"DROP POLICY IF EXISTS {_SERVICE_POLICIES[table]} ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")
