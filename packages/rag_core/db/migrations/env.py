"""Alembic runtime environment (core/storage_schema.md §5).

The DSN is taken from the `POSTGRES_DSN` environment variable (admin/migrations, a role with
`BYPASSRLS`), not from `alembic.ini` - so secrets do not end up in git. For self-hosted/Supabase
it is one and the same migration chain (a single tool - Alembic, not `supabase migrations`).
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from rag_core.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata of the core ORM models (for autogenerate diagnostics; the DDL itself in the
# revisions is raw SQL from storage_schema.md, the source of truth being `core/*.md`).
target_metadata = Base.metadata

# POSTGRES_DSN takes priority over an empty sqlalchemy.url from alembic.ini.
_dsn = os.environ.get("POSTGRES_DSN")
if _dsn:
    config.set_main_option("sqlalchemy.url", _dsn)


def run_migrations_offline() -> None:
    """Generates SQL without connecting to the DB (`alembic upgrade --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Applies migrations through a live connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
