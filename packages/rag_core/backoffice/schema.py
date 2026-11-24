"""SQLAlchemy Core tables for the vendor backoffice contour (`core/storage_schema.md` §2.2).

The cross-tenant vendor tables are intentionally NOT modeled with the tenant ORM (see
``rag_core/db/models.py``): they live on the vendor infrastructure, OUTSIDE tenant-RLS. They are
defined here as lightweight Core tables in a dedicated :class:`MetaData` so the backoffice
service can run portable ``insert``/``select``/``update`` statements (the generic column types
adapt across PostgreSQL and the in-memory SQLite used by the tests), while the canonical DDL for
production remains the Alembic migration ``0004_backoffice``.

Foreign keys are deliberately omitted here (they are enforced by the production migration): the
Core layer only needs the column shapes for CRUD, and omitting cross-``MetaData`` FKs keeps the
test schema self-contained.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Uuid,
    func,
)

backoffice_metadata = MetaData()


customers_table = Table(
    "customers",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("legal_name", Text, nullable=False),
    Column("display_name", Text),
    Column("inn", Text),
    Column("contact_email", Text, nullable=False),
    Column("contact_phone", Text),
    Column("contract_number", Text),
    Column("status", Text, nullable=False, default="trial"),
    Column("metadata", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


backoffice_users_table = Table(
    "backoffice_users",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("email", Text, nullable=False, unique=True),
    Column("hashed_password", Text, nullable=False),
    Column("role", Text, nullable=False, default="support"),
    Column("is_active", Boolean, nullable=False, default=True),
    Column("totp_secret_enc", LargeBinary),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


orders_table = Table(
    "orders",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("customer_id", Uuid(as_uuid=True), nullable=False),
    Column("plan", Text, nullable=False),
    Column("amount_minor", BigInteger),
    Column("currency", Text, nullable=False, default="RUB"),
    Column("status", Text, nullable=False, default="pending"),
    Column("external_provider", Text),
    Column("external_ref", Text),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


activation_codes_table = Table(
    "activation_codes",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("code", Text, nullable=False, unique=True),
    Column("customer_id", Uuid(as_uuid=True), nullable=False),
    Column("order_id", Uuid(as_uuid=True)),
    Column("plan", Text, nullable=False),
    Column("status", Text, nullable=False, default="issued"),
    Column("redeemed_by_tenant_id", Uuid(as_uuid=True)),
    Column("expires_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


box_deployments_table = Table(
    "box_deployments",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("customer_id", Uuid(as_uuid=True), nullable=False),
    Column("tenant_id", Uuid(as_uuid=True)),
    Column("image_version", Text),
    Column("delivered_at", DateTime(timezone=True)),
    Column("hardware_id", Text),
    Column("last_heartbeat_at", DateTime(timezone=True)),
    Column("grace_period_expires_at", DateTime(timezone=True)),
    Column("restricted_mode", Boolean, nullable=False, default=False),
    Column("telemetry_opt_out", Boolean, nullable=False, default=True),
    Column("observability_forward_enabled", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


backoffice_audit_log_table = Table(
    "backoffice_audit_log",
    backoffice_metadata,
    Column("id", Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4),
    Column("actor_backoffice_user_id", Uuid(as_uuid=True)),
    Column("actor_email", Text),
    Column("action", Text, nullable=False),
    Column("impersonated_tenant_id", Uuid(as_uuid=True)),
    Column("object_type", Text),
    Column("object_id", Text),
    Column("ip_address", Text),
    Column("user_agent", Text),
    Column("payload", JSON, nullable=False, default=dict),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


def create_backoffice_schema(engine: object) -> None:
    """Creates all backoffice tables on ``engine`` (used by tests and local bootstrap)."""
    backoffice_metadata.create_all(engine)  # type: ignore[arg-type]
