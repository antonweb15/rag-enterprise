# Evaluation, Observability & DevOps Specification (`core/eval_and_ops.md`)

## 1. Quality evaluation procedure (RAGAS & Golden Dataset)

### 1.1. Golden verification dataset (`tests/data/golden_dataset.json`)
The format of test examples (BEIR / QRELS compatible):
```json
[
  {
    "id": "eval_001",
    "question": "What is the maximum size of a parent chunk in the system?",
    "ground_truth_answer": "The parent chunk size is between 800 and 1200 tokens.",
    "ground_truth_contexts": [
      "Parent Chunks: 800-1200 tokens (logical sections with heading and structure metadata)."
    ],
    "ground_truth_chunk_ids": [
      "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    ],
    "access_level": 0
  }
]
```

### 1.2. Target RAGAS & IR SLO metrics
- **Retrieval Metrics (based on `ground_truth_chunk_ids`)**:
  - `Hit Rate@5` $\ge 0.85$ (percentage of cases where the reference chunk made it into the Top-5).
  - `MRR@5` (Mean Reciprocal Rank) $\ge 0.75$.
  - `nDCG@5` $\ge 0.80$.
- **Generation & Alignment Metrics (RAGAS)**:
  - `Faithfulness` $\ge 0.90$ (no unsupported hallucinations).
  - `Answer Relevance` $\ge 0.85$ (accuracy of the answer to the posed question).
  - `Context Precision` $\ge 0.85$ (ranking relevant contexts above noisy ones).
  - `Context Recall` $\ge 0.85$ (completeness of retrieving all information needed for the answer).

### 1.3. Access security contract tests (Stage E, P1 — mandatory, written together with Stage A)

A non-regression set; CI-blocking. Any failure — merge forbidden.

1. **Level filter in search**: a user with `access_level = 1` does **not** receive `access_level = 2/3` chunks in any arm — `search_dense`, `search_sparse`, `hybrid_search_rrf` (dense/sparse/hybrid).
2. **RLS does not "leak" across the pool**: when a SQLAlchemy-pool connection is reused, `app.user_access_level` (and `app.current_tenant_id`) set via `SET LOCAL` in transaction A are **not visible** in transaction B; a missing GUC is treated as `0` (Public, fail-closed).
3. **Re-classification cascade**: changing `documents.access_level` in a single transaction synchronizes `parent_chunks.access_level` and `child_chunks.access_level`; the initial backfill raises the level of old parent rows from `documents` (a previously Confidential item is not "revealed" as Public).
4. **Citation/hydration re-check**: the answer does **not** contain citations and parent sections for documents inaccessible to the subject, even on a direct access to `parent_chunks` (protection of the "parent without filter" hole).
5. **ACL grant/revoke — immediate effect + audit**: granting/revoking `document_acl` takes effect on the next request (without cache lag) and writes `audit_log` (`acl.grant`/`acl.revoke`).
6. **Forbidding the query path under `service_role`**: user retrieval executed under a role with `BYPASSRLS` must be blocked by the test (otherwise the level/tenant filter is bypassed).
7. **Widget fail-closed by level**: an anonymous widget session (`auth_mode = anonymous`) has `access_level = 0`; the `oidc` mode grants a level only from a verified IdP claim recomputed on the backend (not from the request body).

---

## 2. Observability and Tracing (Langfuse Observability)

### 2.1. Three signals — do not mix

| Signal | Tool | What it carries | Who sees it |
|---|---|---|---|
| **Tracing** (span tree) | **Langfuse** | request → intent → retrieval → rerank → generation, latencies, tokens, cost | tenant operator / super-admin |
| **Structured logging** | standard logger → Loki/DB | errors, events, audit | tenant operator |
| **Telemetry / Heartbeat** | `/licenses/heartbeat` + `box_deployments` | anonymized counters: requests, uptime, p95 | vendor only |

Tracing, logging and telemetry are **not interchangeable**. Raw traces live only in Langfuse; in Postgres — only aggregates/counters.

### 2.2. Provider and multi-tenancy

- **Provider**: **self-hosted Langfuse** on the vendor's infrastructure — the only production option (Cloud Free Tier — dev only).
- **Model B — one project per tenant** (isolation for browser read-only access). The organization is single, the vendor's.
- **The super-admin** has the `Admin`/`Owner` role at the organization → sees all projects. **The cloud client** is a user with the `None` role at the organization + `Viewer` on their project → sees **only their project**, **read-only**, does not see the organization's project list.
- **Signup disabled**: `AUTH_DISABLE_SIGNUP=true`; projects/users/keys are created by the vendor (Langfuse Org API / SCIM; initially manual in the UI is acceptable).
- **Client access** — from the browser, via the deep link `https://<langfuse-host>/project/<project_id>`; a duplicate read interface in the admin panel is **not built** (see [`admin_panel.md`](./admin_panel.md) §11).

### 2.3. Trace contract

Every incoming request forms a `Trace` in Langfuse with a span tree:
1. `Span: intent_routing` (input, intent, score, latency).
2. `Span: query_condensation` (chat_history, raw_query, standalone_query).
3. `Span: hybrid_retrieval` (dense_time, sparse_time, `chunk_ids` + `scores` — **without the raw chunk text**).
4. `Span: reranking` (flashrank_scores, confidence_gate_passed).
5. `Generation: llm_call` (prompt_tokens, completion_tokens, total_cost, model_id).

**Mandatory attributes of every trace** (`TraceContext`, see [`modules_spec.md`](./modules_spec.md)):
`tenant_id`, `environment` (`saas_cloud`/`on_premise_box`), `deployment_id` (for boxes), `channel`, `session_id`, `user_id`, `access_level`, `license_id`.

- **Masking**: fields with PII are logged with `mask`; the raw document text is **not written** into traces.
- **Sampling + retention** — per environment (`trace_sample_rate`, `trace_retention_days`).
- **Online Feedback**: 👍/👎 updates the trace by `trace_id` (score `+1`/`-1`); `messages.langfuse_trace_id` links the DB row with the trace.

### 2.4. Box (on-premise)

- The box compose adds **its own Langfuse** (local, behind `127.0.0.1`/the internal network); the client is the **owner** of the instance (full access).
- The box RAG backend writes traces there automatically (the `LANGFUSE_*` env is generated on the first start).
- Only an **anonymized heartbeat** goes to the vendor; forwarding traces/metrics to the vendor Langfuse is strictly opt-in when `telemetry_opt_out: false` and the license feature flag is set (see [`licensing_and_distribution.md`](./licensing_and_distribution.md)).

### 2.5. Fault tolerance

Trace writing is **fire-and-forget / buffered**: a Langfuse failure does not block the RAG answer.

---

## 3. Containerization and Docker (Multi-stage)

### 3.1. `Dockerfile`
```dockerfile
# Stage 1: Build & Dependencies
FROM python:3.11-slim as builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Stage 2: Final Runtime
FROM python:3.11-slim as runner

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONUNBUFFERED=1

COPY packages/rag_core/ packages/rag_core/
COPY services/api/ services/api/

# Default CMD — a generic API application (the concrete --app is overridden in docker-compose
# for each contour: admin-api / widget-api / telegram-bot / ingestion-worker).
# The Telegram bot is just one of the channels, not the default.
CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 3.2. `docker-compose.yml`
```yaml
services:
  admin-api:
    build:
      context: .
      dockerfile: Dockerfile
    # Serves the Admin SPA static files + the /api/v1/admin/* routers.
    # In On-Premise Box it listens strictly on 127.0.0.1 (access only via an SSH tunnel / reverse proxy).
    command: uvicorn services.api.admin_app:app --host 127.0.0.1 --port 8001
    env_file: .env
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  widget-api:
    build:
      context: .
      dockerfile: Dockerfile
    # The public, anonymous, high-load contour — moved to a separate --app,
    # ready for horizontal scaling (docker compose up --scale widget-api=N).
    command: uvicorn services.api.widget_app:app --host 0.0.0.0 --port 8002
    env_file: .env
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  telegram-bot:
    build:
      context: .
      dockerfile: Dockerfile
    command: python -m services.api.channels.telegram_bot
    env_file: .env
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  ingestion-worker:
    build:
      context: .
      dockerfile: Dockerfile
    command: taskiq worker services.api.tasks.ingestion_tasks:broker
    env_file: .env
    volumes:
      - ./data:/app/data
    restart: unless-stopped
    depends_on:
      - postgres-pgvector

  postgres-pgvector:
    image: pgvector/pgvector:pg15
    environment:
      POSTGRES_DB: postgres
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgrespassword
    ports:
      - "5432:5432"
    volumes:
      - pgdata:/var/lib/postgresql/data

  # Observability: self-hosted Langfuse (see §2). The full Langfuse stack (web + worker +
  # postgres + clickhouse + redis + s3/minio) is deployed per the official compose
  # (langfuse/langfuse); below is just the service binding to the shared network. In Box it listens on 127.0.0.1.
  langfuse:
    image: langfuse/langfuse:latest
    environment:
      AUTH_DISABLE_SIGNUP: "true"       # self-signup disabled — access is granted by the vendor
      NEXTAUTH_URL: http://localhost:3000
    env_file: .env                      # DATABASE_URL, CLICKHOUSE_URL, REDIS_URL, SALT, ENCRYPTION_KEY, LANGFUSE_*
    ports:
      - "3000:3000"                     # in Box — only 127.0.0.1:3000
    restart: unless-stopped

volumes:
  pgdata:
```

---

## 3.3. DB migrations (Alembic, additive-only)

- **Container start**: the entrypoint runs `alembic upgrade head` before starting the application.
- **Pre-upgrade backup hook**: before the migration, in Box a `pg_dump` of the current DB is made (protection against a failed update on the client's Postgres).
- **Additive-only**: destructive operations (`DROP COLUMN`, `DROP TABLE`, a narrowing `ALTER TYPE`) are forbidden; removing fields — only through deprecation in future major versions.

## 3.4. Real-time ingest statuses (Postgres LISTEN/NOTIFY)

- `GET /api/v1/admin/ingest/stream` (SSE) broadcasts the statuses `pending → processing → indexed | failed`.
- The event source is **Postgres `LISTEN/NOTIFY`** (the worker does `NOTIFY` on a `documents.status` change), the fallback is polling the `documents.status` table.
- ⛔ **In-memory pub/sub is forbidden**: with `--workers > 1` the SSE in one worker will not see events from the taskiq worker without a shared broker.

## 3.5. Rate-limiting (separate policies)

| Contour | Policy |
|---|---|
| `widget-api` (public) | A hard limit by IP and `session_id`, protection against DDoS/scraping; a separate process — a DDoS on the widget does not take down the admin panel |
| `admin-api` (private) | A soft limit by the authenticated user; access only from the trusted contour |

---

## 3.6. Monorepo and tooling

The structure `apps/admin` + `apps/widget` + `services/api` + `packages/rag_core` is served by two independent toolchains:
- **Frontend**: `pnpm workspaces` (building Vite → `dist/`, which `admin-api` serves as `StaticFiles`).
- **Backend**: a `uv` (or Poetry) workspace for the Python packages.
- **CI**: two pipelines (front / back) with independent dependency caches.

---

## 4. CI/CD Pipeline (GitHub Actions & Protected Box Distribution)

The workflow file `.github/workflows/ci.yml` (implemented in the repository; CI is pinned to Python 3.11):
1. **Lint & Format & Static Check**: `ruff check .`, `ruff format --check .` (formatting is checked, not only the lint) and `mypy` (strict check without swallowing errors; as code appears, the target is `mypy services/ packages/`). ⛔ "Stubs" like `ruff check . || echo ok` are forbidden — the step must fail on errors.
2. **Unit & Integration Tests**: `pytest tests/ -v` (checking tenant isolation, Ed25519 token validation, RRF and the S3 connector).
3. **Evaluation Gate**: running `python -m tests.eval_ragas` — checking that the quality metrics on `golden_dataset.json` have not degraded below the acceptable SLO threshold.
4. **On-Premise Core Protection & Image Release** (on `v*` tags):
   - Compiling the core modules (`rag_core/licensing/`, `rag_core/retrieval/`) into `.so` via Cython/PyArmor.
   - Building the closed Docker image `rag-core:enterprise-vX.Y`.
   - Publishing to the private GitHub Container Registry (`ghcr.io/your-org/rag-core`).
   > **An honest assessment of the protection boundaries.** Compilation into `.so`/obfuscation is only a *cost increase* for reverse-engineering, NOT reliable IP protection: the image still runs on the client and contains enough artifacts for analysis. Real protection is provided by **(a) watermarks/canary marks for leak attribution** (see `core/licensing_and_distribution.md` §4a) and **(b) the legal contour** (the license agreement, signed Ed25519 tokens, CRL, heartbeat clone detection), not binary obfuscation.
5. **Secrets**: management of confidential variables (`SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `LICENSE_SIGNING_KEY`, `JWT_SECRET`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN`) strictly through **GitHub Actions Secrets**.
