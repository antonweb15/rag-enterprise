"""Fixtures for RLS security integration contract tests (storage_schema.md §4, §1.3).

They start a real Postgres+pgvector via testcontainers, apply the Alembic chain up to head and
provide two engines:
- `admin_engine` - the container superuser (analog of service_role/BYPASSRLS): seeds data;
- `app_engine` - the `app_user` role (WITHOUT BYPASSRLS, a member of authenticated):
  user-retrieval, where RLS is checked.

A working Docker is required. In its absence all integration tests are marked skipped
(rather than failing), so a local run without Docker stays green; in CI Docker is present.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any, cast

import pytest
from sqlalchemy import Engine, create_engine, text

from tests.integration._docker import docker_available, in_ci

APP_USER_PASSWORD = "app_user_test_pw"  # noqa: S105 - test password for the container only


def _psycopg_url(container: object) -> str:
    """Builds a SQLAlchemy URL with the psycopg (v3) driver for the admin connection."""
    raw: str = cast(str, cast(Any, container).get_connection_url())
    return raw.replace("postgresql+psycopg2://", "postgresql+psycopg://")


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[object]:
    if not docker_available():
        # In CI Docker is mandatory -> fail loudly (never silently drop security coverage).
        if in_ci():
            pytest.fail("Docker is required in CI for the RLS security contract tests.")
        pytest.skip("Docker unavailable")
    from testcontainers.postgres import PostgresContainer  # noqa: PLC0415

    # Image with pgvector preinstalled (vector/sparsevec types + HNSW).
    with PostgresContainer("pgvector/pgvector:pg16") as container:
        yield container


@pytest.fixture(scope="session")
def admin_engine(_pg_container: object) -> Iterator[Engine]:
    """Admin engine (BYPASSRLS): applies migrations, sets the app_user password/grants."""
    from alembic import command  # noqa: PLC0415
    from alembic.config import Config  # noqa: PLC0415

    dsn = _psycopg_url(_pg_container)
    os.environ["POSTGRES_DSN"] = dsn

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cfg = Config(os.path.join(project_root, "alembic.ini"))
    command.upgrade(cfg, "head")

    engine = create_engine(dsn, future=True)
    with engine.begin() as conn:
        # app_user was created by the migration WITHOUT a password (an operational
        # secret). Set it for the test only.
        conn.execute(text(f"ALTER ROLE app_user WITH PASSWORD '{APP_USER_PASSWORD}'"))
        # In self-hosted (outside Supabase) authenticated needs table privileges - grant them
        # so that RLS (and not the absence of GRANT) determines row visibility.
        conn.execute(text("GRANT USAGE ON SCHEMA public TO authenticated"))
        conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES "
                "IN SCHEMA public TO authenticated"
            )
        )
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine(_pg_container: object, admin_engine: Engine) -> Iterator[Engine]:
    """app_user engine (NOBYPASSRLS, a member of authenticated) - the user-retrieval path."""
    base = _psycopg_url(_pg_container)
    # Swap the credentials in the URL to app_user. Format: postgresql+psycopg://user:pass@host:port/db
    scheme, rest = base.split("://", 1)
    _creds, hostpart = rest.split("@", 1)
    app_url = f"{scheme}://app_user:{APP_USER_PASSWORD}@{hostpart}"
    engine = create_engine(app_url, future=True)
    yield engine
    engine.dispose()
