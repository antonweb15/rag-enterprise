"""backfill channel 'fastapi' -> 'rest_sse' + remove wildcard default for allowed_origins

Canonical DDL - core/storage_schema.md §2.1 (channels.md §2). Legacy 'fastapi' is not parsed by
the str-enum ChannelType; the unsafe wildcard '["*"]' in allowed_origins is removed.

Revision ID: 0007_channel_backfill
Revises: 0006_rls_policies
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_channel_backfill"
down_revision: str | None = "0006_rls_policies"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("UPDATE users SET channel = 'rest_sse' WHERE channel = 'fastapi';")
    op.execute("UPDATE conversations SET channel = 'rest_sse' WHERE channel = 'fastapi';")
    # Remove the unsafe wildcard default (the widget stops being served until an origin
    # is added explicitly) - a deliberate security fix.
    op.execute(
        "UPDATE tenants SET allowed_origins = '[]'::jsonb WHERE allowed_origins = '[\"*\"]'::jsonb;"
    )


def downgrade() -> None:
    # The data backfill is irreversible by contract (additive-only, storage_schema.md §5);
    # the channel/allowed_origins values are not rolled back.
    pass
