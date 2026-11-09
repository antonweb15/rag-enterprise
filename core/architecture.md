# Architecture & Data Flow Specification (`core/architecture.md`)

## 1. Architecture overview

The system is built on a layered, loosely coupled architecture (Clean Architecture / Hexagonal) with support for asynchronous processing and Dependency Injection.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           1. Channel Ingress Layer                          │
│   PRIMARY: Web Widget (Embed JS / SSE / Shadow DOM) + Admin SPA (private)   │
│   - Admin SPA: Vite+React, /api/v1/admin/*, fastapi-users cookie sessions   │
│   - Telegram Bot (aiogram 3.x) — ONE OF the BaseChannel channels, not primary│
│   - Pluggable Channel Abstraction (FastAPI REST / SSE, CLI/typer)           │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (raw query + tenant_id + user_id + session_id)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       2. Query Understanding Layer                          │
│   - License & Tenant Guard (Ed25519 token check + rate limit)               │
│   - Session Memory Manager (K=5 context turns per tenant)                   │
│   - Intent Router (RAG Search / Chit-Chat / Meta)                           │
│   - Query Condenser & Standalone Query Generator                            │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (standalone query)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        3. Hybrid Retrieval & Fusion                         │
│   - Dense Embedding: BAAI/bge-m3 (1024 dim, Cosine)                         │
│   - Sparse Vector: BAAI/bge-m3 sparsevec                                    │
│   - BaseVectorStore.search_dense / .search_sparse (Top-30, ef_search = 40)   │
│   - RRF in the application layer (α=0.6, β=0.4, k=60) + tenant_id & access_level│
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (Top-30 child candidates)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                       4. Reranking & Confidence Gate                        │
│   - Cross-Encoder Reranker: FlashRank / bge-reranker-small                  │
│   - Confidence Gate Check (Max Score < τ=0.35 → Return "No info found")     │
│   - Small-to-Big Hydration: Mapping child_chunks -> parent_chunks (Top 3..5)│
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (Hydrated parent context chunks)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    5. Context Budgeting & LLM Generation                    │
│   - Context Window Budgeting & Deduplication (Parent Chunks)                │
│   - Anti-Prompt-Injection Sandbox Packing (<untrusted_doc_context>)         │
│   - LLM Runner (Groq llama-3.3-70b / Ollama qwen2.5:7b) + Retry & Fallback  │
│   - Token Streaming with Message Edit Throttling (0.5 - 1.0s) & Citations   │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    6. Observability & Async Evaluation                      │
│   - Langfuse Tracing (Tree of Spans, Latencies, Token Costs)                │
│   - Async LLM-as-a-Judge (Faithfulness, Relevance, Precision)               │
│   - User Feedback Telemetry (Inline 👍/👎)                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 1.1. Deployment Modes

A single codebase and a single SPA build for both modes; the differences come only through the runtime config `GET /api/v1/admin/config`. Forks are forbidden.

| Feature | `saas_cloud` | `on_premise_box` |
|---|---|---|
| Tenant management (CRUD, cross-tenant operator view) | ✅ | ❌ (single tenant) |
| License issuance/renewal from the vendor panel | ✅ | ❌ |
| License activation screen (code / `.key` / PASETO) | ⚠️ status only | ✅ |
| Machine Fingerprint (`hardware_id`) | ❌ | ✅ |
| Heartbeat status + Grace Period | ✅ | ✅ (in `online_subscription`) |
| Local file manager (local FS via `BaseObjectStorage`) | ❌ (S3/R2) | ✅ |
| External storage connectors (S3/MinIO/R2/Confluence) | ✅ | ✅ |
| CLI fallback (`typer`) for air-gapped | ⚠️ optional | ✅ required |
| Admin bind address | `0.0.0.0` behind a reverse proxy | `127.0.0.1` + SSH tunnel |
| SPA serving | Nginx / CDN | FastAPI `StaticFiles` (single container) |

Details of the private operator contour are in [`admin_panel.md`](./admin_panel.md).
The single source for the user delivery channels (Web Widget, Telegram, REST/SSE, CLI), the widget handshake, branding and the Cloud/Box modes is [`channels.md`](./channels.md).

> ⚠️ The row "License issuance/renewal from the vendor panel" refers to the **separate** vendor super-admin contour, not the tenant admin panel: it is described in [`vendor_backoffice.md`](./vendor_backoffice.md) (the `BackofficeRole` roles, customers, codes, box registry, break-glass impersonation), while the server half of licensing (issuance/revocation/heartbeat/CRL) is in [`license_server.md`](./license_server.md).

---

## 1.2. Observability Plane (a separate plane)

Observability is a **separate plane**, it does not replace the data plane or licensing:

- **Cloud**: a single self-hosted vendor Langfuse; **one project per tenant** (model B); the cloud client reads **only its own project** (role `Viewer`) from the browser; the super-admin sees all projects at the organization level.
- **Box**: its own local Langfuse in the box compose (the client is the owner); only an anonymized heartbeat (`/licenses/heartbeat`) goes to the vendor.
- **Trace writing** is fire-and-forget: a Langfuse failure does not block the RAG answer (see [`eval_and_ops.md`](./eval_and_ops.md) §2).

---

## 2. Request processing lifecycle (Query Flow)

1. **Input event** (channel-neutral): the channel adapter (Web Widget / Telegram / REST-SSE / CLI) normalizes the native message into a `ChannelEvent` (`event_type`, `user_id`, `tenant_id`, `channel`, `channel_user_id`, `text`, `payload`). The retrieval core accepts **only** a `ChannelEvent` and knows nothing about the specific channel (see [`channels.md`](./channels.md), [`modules_spec.md`](./modules_spec.md)).
2. **Authentication, subject resolution & effective-access computation**:
   - `channel_user_id` (the channel's external ID) is matched against the `users` table (or the session cache); for an authenticated widget — the subject from a verified OIDC/SAML (`external_subject`, see [`channels.md`](./channels.md)).
   - **Subject resolution**: a `UserContext` is assembled (`access_level` + `group_ids`); the values are taken **from the backend** (`users`/`user_groups`/IdP claim), **never** from the client payload.
   - **PolicyEvaluator.effective_access(user) → `AccessDecision`**: the single decision point (PDP) computes `effective_access_level = max(user, groups)` and, when `enterprise_acl` is enabled, `allowed_document_ids` (from `document_acl`). When the flag is off — plain `access_level`. fail-closed: no tenant/level → empty result set.
3. **Rate Limiting**: `ThrottlingMiddleware` checks the limit (no more than 5 messages per minute per `user_id`).
4. **Intent Routing**:
   - `chit_chat` $\to$ a quick answer from a lightweight LLM without hitting the DB.
   - `meta_command` $\to$ info about the system state and available sources.
   - `rag_search` $\to$ handed off to the RAG pipeline.
5. **Question condensation (Multi-turn)**:
   - The last $K=5$ dialog messages are retrieved from the `messages` table.
   - If the question depends on context, a lightweight LLM generates a standalone `standalone_query`.
6. **Vectorization and Sparse encoding**:
   - `bge-m3` in a single pass generates the dense vector (1024 float) and the sparse vector (sparse dictionary).
7. **Hybrid search (`BaseVectorStore` + `Retriever`)**:
   - The **separate arms of the contract** are called: `BaseVectorStore.search_dense(...)` and `BaseVectorStore.search_sparse(...)`.
   - Fusion is performed **in the application layer** (`Retriever.fuse_rrf`), not in SQL. The SQL function `hybrid_search_rrf` is an internal optimization detail of `PgVectorStore`, used only when `capabilities.supports_hybrid_rrf = True`.
   - The primary filter `WHERE d.access_level <= user_access_level` excludes inaccessible documents; with `enterprise_acl` the predicate `allowed_document_ids` from `AccessDecision` is additionally applied — **before** `LIMIT`/RRF (not as a post-filter), so as not to break top-k and ranking.
   - The Top-30 candidates by cosine distance (HNSW) and the Top-30 by sparsevec are found and combined by the RRF formula:
     $$RRF(d) = \frac{0.6}{60 + r_{dense}(d)} + \frac{0.4}{60 + r_{sparse}(d)}$$
8. **Reranking (FlashRank)**:
   - The resulting Top-30 chunks are reranked by a Cross-Encoder model.
   - **Confidence Gate**: if $\max(score) < 0.35$, the search is aborted and the user receives a "no information found" message.
9. **Small-to-Big Hydration + access re-check**:
   - By the `parent_id` of the selected best child chunks (Top 3-5), the full text of the corresponding `parent_chunks` is loaded.
   - **Effective-access re-check (defense-in-depth)**: the hydrator and the citation resolver must re-check each `parent_chunk`/citation through `PolicyEvaluator.can_access_document(...)` (using the denormalized `parent_chunks.access_level` + ACL). This closes direct access to parent content bypassing the child filter; inaccessible citations/sections are dropped.
   - Duplicate parent sections are removed.
10. **Prompt assembly and Output** (channel-neutral):
   - A protected context is formed inside `<untrusted_doc_context>`.
   - Output goes through `BaseChannel.send_response()` / `stream_response()`; the delivery method is chosen **at the channel boundary** by `ChannelCapabilities` (e.g. streaming throttling 0.5-1.0 sec for Telegram, SSE for Web/REST).
   - UI elements (👍/👎 buttons, markdown, citations) are rendered by the adapter only when the corresponding `supports_*` are set; the search business logic does **not** branch on flags.
11. **Tracing**: all spans, tokens and timings are sent to Langfuse.

---

## 3. Background data ingestion architecture (Ingestion ETL Flow)

```
[ BaseObjectStorage (per-tenant: S3 / MinIO / R2 / local) / Admin SPA Upload ]
                 │
                 ▼
┌────────────────────────────────────────────────────────┐
│            File Watcher with Debounce (2-3s)           │
│  - Detects new/modified files per tenant               │
│  - Computes SHA-256 Checksum                           │
│  - Checks `documents` table (Skip if already indexed)  │
└────────────────────────┬───────────────────────────────┘
                         │ (Job Payload: tenant_id, file_path, hash)
                         ▼
┌────────────────────────────────────────────────────────┐
│              Durable Queue (Taskiq + Postgres)         │
│  - Atomic Task enqueue (1 file = 1 task)               │
│  - Retry with Exponential Backoff                      │
└────────────────────────┬───────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────┐
│               Ingestion Worker Process                 │
│  1. Parsing: Docling / Structured Layout Parser        │
│  2. Token-aware Parent-Child Splitter:                 │
│     - Parent: 800 - 1200 tokens (logical sections)     │
│     - Child: 150 - 250 tokens (granular search units)  │
│  3. Batch Embedding Generation (BGE-M3, batch_size=32):│
│     - Dense (1024 dim) + Sparse vector extraction      │
│  4. Transactional Upsert (SQLAlchemy + VectorStore):   │
│     - INSERT documents (tenant_id) -> parent -> child  │
│     - UPDATE status = 'indexed'                        │
│  5. NOTIFY ingest_events -> admin SSE (LISTEN)         │
└────────────────────────────────────────────────────────┘
```

File sources are unified through the **`BaseObjectStorage`** port (per-tenant prefix `{tenant_id}/`): the same task `process_document_task(tenant_id, storage_ref, content_hash, access_level)` serves drag-and-drop upload from the Admin SPA, the local-folder File Watcher, and external connectors.

---

## 4. Fault-tolerance contour (Resilience Matrix)

| Component | Possible failure | Protection mechanism |
|---|---|---|
| **Groq Cloud API** | Rate Limit (HTTP 429) / 5xx | Tenacity Retry (3 attempts, backoff 1s..5s) $\to$ Fallback to Gemini Free or local Ollama |
| **Embedder / CPU** | OOM / high load | Batching strictly by 32 chunks, limiting worker concurrency |
| **File Watcher** | Partial file write | Debounce 2.5 sec, checking `st_size` stability before reading |
| **Telegram API** | Flood control / Rate limit | Message-edit queue with throttling (min 0.8s between editMessageText) |
| **PostgreSQL / pgvector** | Network timeout / Pool exhaustion | Connection Pooling (SQLAlchemy Pool / Supavisor when hosted on Supabase), timeout 10s |
| **Admin SSE (`/admin/ingest/stream`)** | Worker events not visible with `--workers N` | Postgres `LISTEN/NOTIFY` as a shared bus + fallback polling of `documents.status`; in-memory pub/sub is forbidden |
| **Public Web widget** | DDoS / traffic spike takes down the private admin | Separate `--app` / worker pools and rate-limit policies, ready for `docker compose scale widget=N` |
| **Langfuse** | Unavailable / timeout | Fire-and-forget trace writing with a buffer; the RAG answer is not blocked (see §1.2, [`eval_and_ops.md`](./eval_and_ops.md) §2.5) |
