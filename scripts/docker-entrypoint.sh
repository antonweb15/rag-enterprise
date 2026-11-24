#!/bin/sh
# Protected Box entrypoint (IMPLEMENTATION_PLAN.md Stage M / §5).
#
# Optionally applies additive-only DB migrations before starting the app, then execs the given
# command (default: the widget API). Migrations are gated behind RUN_MIGRATIONS=1 so a plain
# restart never touches the schema; the box update wave sets it explicitly.
#
# The migrations dir is COPYed explicitly into the image at /app/migrations (the pip-installed
# `rag_core` package does NOT ship it: `migrations/` has no `__init__.py`, so setuptools omits it
# from the wheel). See the Dockerfile runtime stage.
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
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
