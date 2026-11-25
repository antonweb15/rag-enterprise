#!/bin/sh
# Protected Box entrypoint (IMPLEMENTATION_PLAN.md Stage M / §5).
#
# Optionally applies additive-only DB migrations before starting the app, then execs the given
# command (default: the widget API). Migrations are gated behind RUN_MIGRATIONS=1 so a plain
# restart never touches the schema; the box update wave sets it explicitly. Before migrating it
# takes a pg_dump of the current DB (storage_schema.md §5 item 3 / eval_and_ops.md §3.3) and
# aborts if the dump cannot be created - a failed update on the client's Postgres must never
# leave it unrecoverable.
#
# The migrations dir is COPYed explicitly into the image at /app/migrations (the pip-installed
# `rag_core` package does NOT ship it: `migrations/` has no `__init__.py`, so setuptools omits it
# from the wheel). See the Dockerfile runtime stage.
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    # --- Pre-upgrade backup hook (mandatory for Box) -------------------------
    # A pg_dump of the CURRENT database is taken BEFORE applying migrations. If the dump
    # cannot be created the upgrade MUST NOT start. The snapshot is written to a mounted
    # volume (BACKUP_DIR, default /app/backups) so the operator can roll back a bad update.
    BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
    mkdir -p "${BACKUP_DIR}"

    if [ -z "${POSTGRES_DSN:-}" ]; then
        echo "[entrypoint] POSTGRES_DSN is not set - refusing to migrate without a pre-upgrade backup" >&2
        exit 1
    fi
    if ! command -v pg_dump >/dev/null 2>&1; then
        echo "[entrypoint] pg_dump is not installed - refusing to migrate without a pre-upgrade backup" >&2
        exit 1
    fi

    # The admin DSN is a SQLAlchemy URL (postgresql+psycopg://...); pg_dump/libpq does not
    # understand the `+psycopg` driver suffix, so normalize it to a libpq URI before dumping.
    DUMP_DSN="$(printf '%s' "${POSTGRES_DSN}" | sed 's#^postgresql+psycopg://#postgresql://#')"
    STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
    DUMP_FILE="${BACKUP_DIR}/pre-migration-${STAMP}.dump"

    echo "[entrypoint] pre-upgrade pg_dump -> ${DUMP_FILE}"
    pg_dump --dbname "${DUMP_DSN}" --format=custom --file "${DUMP_FILE}"

    echo "[entrypoint] RUN_MIGRATIONS=1 -> alembic upgrade head"
    MIG_DIR="/app/migrations"
    # Point alembic at the copied migrations dir (alembic.ini's repo-relative path is absent at
    # runtime). The DSN itself is injected by env.py from POSTGRES_DSN - never from the ini.
    RUNTIME_INI="$(mktemp)"
    sed "s#^script_location = .*#script_location = ${MIG_DIR}#; s#^prepend_sys_path = .*#prepend_sys_path = .#" \
        /app/alembic.ini > "${RUNTIME_INI}"
    alembic -c "${RUNTIME_INI}" upgrade head
    rm -f "${RUNTIME_INI}"
fi

exec "$@"
