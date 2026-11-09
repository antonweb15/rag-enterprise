"""schema/table privileges for `authenticated` + role-attribute normalization

Closes the self-hosted / on_premise_box gap: Supabase ships `pg_default_acl` grants for
`public`, but a plain Postgres (self-hosted / the pgvector container) has none, so the
`app_user` retrieval path would fail with `permission denied for table ...` BEFORE RLS is
ever reached - the fail-closed contract (storage_schema.md §4) would not even be exercisable.
This migration makes the Box/self-hosted path self-contained by granting USAGE on the schema
and table privileges to `authenticated` (app_user inherits them via role membership), and by
normalizing the (NO)BYPASSRLS attribute on the pre-existing roles so an arbitrary pre-seeded
cluster cannot silently keep the wrong attribute.

Canonical DDL - core/storage_schema.md §0/§4. Additive-only, idempotent, safe to re-run.

Revision ID: 0008_schema_grants
Revises: 0007_channel_backfill
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_schema_grants"
down_revision: str | None = "0007_channel_backfill"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Normalize role attributes on pre-existing roles. CREATE ROLE in 0001 is IF NOT
    #    EXISTS-only and does NOT enforce (NO)BYPASSRLS on a role that already existed with
    #    the wrong attribute. These ALTER ROLE statements are idempotent and guarantee the
    #    three-role security model on self-hosted / on_premise_box clusters (where the
    #    migration runs as superuser). On managed Supabase, service_role/authenticated/anon are
    #    reserved roles that only a superuser may modify AND they already carry the correct
    #    attributes, so insufficient_privilege is caught and ignored (nothing to fix there).
    op.execute("""
    DO $$
    DECLARE
        r RECORD;
    BEGIN
        FOR r IN
            SELECT * FROM (VALUES
                ('service_role', true),
                ('authenticated', false),
                ('anon', false),
                ('app_user', false)
            ) AS t(rolname, want_bypassrls)
        LOOP
            BEGIN
                IF r.want_bypassrls THEN
                    EXECUTE format('ALTER ROLE %I BYPASSRLS', r.rolname);
                ELSE
                    EXECUTE format('ALTER ROLE %I NOBYPASSRLS', r.rolname);
                END IF;
            EXCEPTION WHEN insufficient_privilege THEN
                RAISE NOTICE 'skip ALTER ROLE % (reserved/managed role)', r.rolname;
            END;
        END LOOP;
    END
    $$;
    """)

    # 2. Schema/table privileges for `authenticated` (app_user inherits them via
    #    GRANT authenticated TO app_user from 0001). On self-hosted Postgres there are no
    #    Supabase default ACLs, so without these grants RLS is never reached. Idempotent.
    op.execute("GRANT USAGE ON SCHEMA public TO authenticated;")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO authenticated;"
    )
    op.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;")

    # 3. Default privileges for FUTURE tables/sequences created in `public` (so later
    #    additive migrations do not need to repeat the grants).
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT USAGE, SELECT ON SEQUENCES TO authenticated;"
    )


def downgrade() -> None:
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE USAGE, SELECT ON SEQUENCES FROM authenticated;"
    )
    op.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM authenticated;"
    )
    op.execute("REVOKE USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public FROM authenticated;")
    op.execute(
        "REVOKE SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public FROM authenticated;"
    )
    op.execute("REVOKE USAGE ON SCHEMA public FROM authenticated;")
    # Role attributes are intentionally not rolled back (shared cluster-level state).
