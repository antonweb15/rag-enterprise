"""access_level backfill + cascade re-classification (documents -> parent/child)

Canonical DDL - core/storage_schema.md §2.3. The transactional cascade closes the window of
inconsistency of the denormalized access_level in chunks.

Revision ID: 0002_access_cascade
Revises: 0001_core_schema
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_access_cascade"
down_revision: str | None = "0001_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # (1) One-time backfill of existing data.
    op.execute("""
    UPDATE parent_chunks p
       SET access_level = d.access_level
      FROM documents d
     WHERE p.document_id = d.id
       AND p.access_level IS DISTINCT FROM d.access_level;

    UPDATE child_chunks c
       SET access_level = d.access_level
      FROM parent_chunks p
      JOIN documents d ON p.document_id = d.id
     WHERE c.parent_id = p.id
       AND c.access_level IS DISTINCT FROM d.access_level;
    """)

    # (2) Cascade re-classification trigger (single transaction).
    op.execute("""
    CREATE OR REPLACE FUNCTION cascade_access_level() RETURNS TRIGGER AS $$
    BEGIN
        UPDATE parent_chunks SET access_level = NEW.access_level WHERE document_id = NEW.id;
        UPDATE child_chunks  SET access_level = NEW.access_level
         WHERE parent_id IN (SELECT id FROM parent_chunks WHERE document_id = NEW.id);
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;

    DROP TRIGGER IF EXISTS trg_documents_access_cascade ON documents;
    CREATE TRIGGER trg_documents_access_cascade
        AFTER UPDATE OF access_level ON documents
        FOR EACH ROW WHEN (OLD.access_level IS DISTINCT FROM NEW.access_level)
        EXECUTE FUNCTION cascade_access_level();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_documents_access_cascade ON documents;")
    op.execute("DROP FUNCTION IF EXISTS cascade_access_level();")
