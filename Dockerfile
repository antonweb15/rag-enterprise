# syntax=docker/dockerfile:1.7
# ---------------------------------------------------------------------------
# Protected On-Premise Box image (IMPLEMENTATION_PLAN.md Stage M,
# core/licensing_and_distribution.md §2, core/eval_and_ops.md §4).
#
# Multi-stage build:
#   1. builder  - install deps, cythonize the protected core (licensing, retrieval/RRF, chunking
#                 and context budgeting) into .so and STRIP the .py sources, then REPLACE the
#                 readable pip-installed copy with the compiled tree (no readable critical IP).
#   2. runtime  - a slim, non-root image with ONLY the compiled core + open config layer.
#
# Honest security boundary (core/eval_and_ops.md §4): compilation only raises the
# reverse-engineering cost. Real protection = watermark/canary attribution
# (rag_core.watermark, §4a) + the legal contour (signed Ed25519 licenses, CRL, heartbeat).
# ---------------------------------------------------------------------------

# Base image pinned to an immutable digest (⚪ supply-chain reproducibility, Stage M): a mutable
# tag like `python:3.11-slim` would let `apt`/`pip` resolve different layers between the scan and
# the push. Bump the digest deliberately (renovate/dependabot) when upgrading the base.
# --- Stage 1: builder -------------------------------------------------------
FROM python:3.11.9-slim-bookworm@sha256:7a3ed1226224bcc1fe5443262363d42f48cf832a540c1836ba8ccbeaadf8637c AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Cython needs a C toolchain to compile the protected core into .so.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install the core package (runtime deps) + the build toolchain. This also drops a *readable*
# copy of rag_core into site-packages - we overwrite it with the compiled tree below.
COPY pyproject.toml README.md ./
COPY packages ./packages
RUN python -m pip install --upgrade pip \
    && pip install "Cython>=3.0" "setuptools>=68" wheel \
    && pip install --prefix=/install .

# Cythonize the protected core IN PLACE and strip the .py/.c sources, then REPLACE the readable
# copy that `pip install` left in site-packages with the compiled tree. We must `rm -rf` the
# installed package first: `cp -r SOURCE DEST` would otherwise NEST the tree under DEST/rag_core/
# (leaving the readable .py IP importable at the top level). Replacing wholesale guarantees the
# only rag_core in the image is the stripped one (no readable .py for licensing/retrieval/...).
COPY scripts ./scripts
RUN SITE_PACKAGES="$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))' | sed 's#^/usr/local#/install#')" \
    && python scripts/compile_core.py --build \
    && rm -rf "${SITE_PACKAGES}/rag_core" \
    && cp -r packages/rag_core "${SITE_PACKAGES}/rag_core" \
    && python scripts/compile_core.py --assert-stripped "${SITE_PACKAGES}/rag_core"

# --- Stage 2: runtime -------------------------------------------------------
# Same immutable digest as the builder (reproducibility + single scanned artifact, Stage M).
FROM python:3.11.9-slim-bookworm@sha256:7a3ed1226224bcc1fe5443262363d42f48cf832a540c1836ba8ccbeaadf8637c AS runtime

# The per-build leak-attribution stamp (rag_core.watermark §4a) is injected at build time via
# `--build-arg RAG_BUILD_STAMP=...`; it stays empty when not supplied.
ARG RAG_BUILD_STAMP=""
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RAG_BUILD_STAMP="${RAG_BUILD_STAMP}" \
    APP_HOME=/app

# A stable OCI label set for the private GHCR image (rag-core:enterprise-vX.Y).
LABEL org.opencontainers.image.title="rag-core" \
      org.opencontainers.image.description="Protected On-Premise Box (compiled core)" \
      org.opencontainers.image.source="https://github.com/your-org/rag-core"

# Run as an unprivileged user (never root in the client's contour).
RUN groupadd --system rag && useradd --system --gid rag --home-dir ${APP_HOME} rag

WORKDIR ${APP_HOME}

# Copy the installed site-packages (compiled core + deps) and the open transport layer.
COPY --from=builder /install /usr/local
COPY services ./services
COPY alembic.ini ./alembic.ini
# The box image must be able to run `alembic upgrade head` (RUN_MIGRATIONS=1). The migrations dir
# lives OUTSIDE the pip-installed rag_core package (`migrations/` has no `__init__.py`, so
# setuptools omits it from the wheel) - COPY it explicitly and point the entrypoint at /app/migrations.
COPY packages/rag_core/db/migrations ./migrations
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
COPY apps/admin/dist ./apps/admin/dist

# Bake the per-build leak-attribution stamp next to the compiled core as build_stamp.json
# (rag_core.watermark §4a): the release pipeline passes a per-release/per-client stamp via the
# RAG_BUILD_STAMP build-arg, so a leaked copy can be attributed from BOTH the env var and the file.
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && if [ -n "${RAG_BUILD_STAMP}" ]; then \
         printf '%s' "${RAG_BUILD_STAMP}" \
           > "$(python -c 'import sysconfig; print(sysconfig.get_path("purelib"))')/rag_core/build_stamp.json"; \
       fi

# Serve the public widget contour by default (admin-api binds to 127.0.0.1, see compose).
USER rag
EXPOSE 8002

# HEALTHCHECK (supply-chain / ops best practice, IMPLEMENTATION_PLAN.md Stage M): the orchestrator
# restarts the container if the app stops importing/serving. Uses only the stdlib (no curl).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=3).status==200 else 1)" || exit 1

# The entrypoint runs additive-only DB migrations when RUN_MIGRATIONS=1 (box update wave,
# IMPLEMENTATION_PLAN.md §5) and then execs the command. It points alembic at the migrations dir
# COPYed above (/app/migrations), so it works even though only the compiled core (not the repo
# `packages/` tree) ships.
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["uvicorn", "services.api.widget_app:app", "--host", "0.0.0.0", "--port", "8002"]
