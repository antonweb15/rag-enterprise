# Enterprise RAG Specification & Architecture Blueprint (v2.2)

This document defines the architectural basis, contracts and engineering standards for building an extensible Enterprise RAG system supporting operation on Free-tier / Local / Cloud infrastructure.

The specification serves as the basis for creating the Markdown specification core (`core/soul.md`, `core/architecture.md`, `core/storage_schema.md`, `core/modules_spec.md`, `core/licensing_and_distribution.md`, `core/eval_and_ops.md`).

> **Document status.** This is an overview historical blueprint. The canonical and priority source of truth is the `core/*.md` files. In case of discrepancies, follow `core/*.md`. In particular, **the primary end-user interface is the embeddable Web widget** (`core/channels.md`), Telegram is a secondary channel.

---

## 🎯 1. Architectural principles, NFR / SLO and Security

### 1.1. Concept: the hybrid model (SaaS Cloud + Protected On-Premise Box)
- **SaaS Cloud (Multi-Tenant)**: hosting on a cost-effective VPS / Docker Compose (without Kubernetes at the start), strict isolation via `tenant_id` and PostgreSQL RLS, storage in S3 (Cloudflare R2 / MinIO / Selectel), connection via the embeddable Web widget (`<script>`) and a Telegram bot.
- **On-Premise Box (Enterprise)**: delivery of the core as protected binary modules (Cython / PyArmor) via a private Docker Registry, cryptographic offline license validation (Ed25519) + online Heartbeat, the ability for the client to deploy on Docker Compose / K8s with their own S3 and local LLMs.
- **Strict modularity and typing**: Pydantic V2, Clean Architecture, Dependency Injection.

### 1.2. Target quality and reliability metrics (SLO / NFR)
- **Retrieval Metrics (on the golden dataset)**:
  - **Hit Rate@5**: $\ge 0.85$
  - **MRR@5**: $\ge 0.75$
  - **nDCG@5**: $\ge 0.80$
- **P95 Retrieval Latency**: $< 400$ ms (vector search + sparse/lexical search + RRF fusion + FlashRank reranking at `ef_search = 40`).
- **P95 Time To First Token (TTFT)**: $< 1.5$ s when streaming answers to the user in Telegram/SSE.
- **Fault tolerance**: Circuit Breaker, Exponential Backoff and fallback providers on Rate Limit / failures of external free APIs.

### 1.3. Security, Rate Limiting and Data Governance
- **Protection against Indirect Prompt Injection**: all context chunks are isolated in system prompts by strict `<untrusted_doc_context>` tags with the instruction to ignore any system commands inside documents.
- **RBAC & Data Isolation**: access-rights separation at the database level via the `access_level` of documents/users and PostgreSQL Row-Level Security (RLS).
- **Rate Limiting & Throttling**: protection of public channels (Web widget, Telegram) from spam via middleware (separate policies: the public `widget_rate_limit_per_minute`, the private admin `admin_rate_limit_per_minute`; a graceful throttle response).
- **Data Residency (Supabase Cloud)**: storing sensitive data in a specified EU region / local contour with encryption of confidential fields in `metadata`.

---

## 🏗️ 2. Technology stack and Infrastructure

- **Primary interface**: the embeddable **Web widget** (`<script>`, Shadow DOM, white-label) on top of FastAPI (REST / SSE stream) — see `core/channels.md`. Secondary channels: a Telegram Bot on `aiogram 3.x` (Long polling / Webhook, inline 👍/👎 buttons, a typing status, streaming with throttling and cancellation handling), REST/SSE for integrations and a CLI (air-gapped Box).
- **Channel abstraction (`BaseChannel`)**: a single channel-neutral contract (`ChannelEvent`) — "one input, several outputs": Web widget, Telegram, REST/SSE, CLI.
- **Storage**: **Supabase (Managed PostgreSQL 15+ with pgvector 0.7+)**:
  - Vector search: an HNSW index with cosine distance (`vector_cosine_ops`, 1024 dim for the normalized BGE-M3, build parameters: `m=16, ef_construction=64`, query parameter: `ef_search=40`).
  - Hybrid lexical arm: Dense + Sparse (`BAAI/bge-m3` with sparse vectors stored in the `sparsevec` type in pgvector 0.7+ with an HNSW/IVFFlat index) or `bm25s`.
  - Migration management: `supabase migrations` / `alembic`.
- **Embeddings and Reranking**:
  - **Dense + Sparse Embedder**: `BAAI/bge-m3` (via `fastembed` / `sentence-transformers` locally on CPU/GPU, with batching support: `batch_size = 32`).
  - **Reranker**: `FlashRank` (a local lightweight cross-encoder) or `bge-reranker-small`.
- **LLM providers and generation parameters**:
  - Locally: **Ollama** (`qwen2.5:7b-instruct`, `llama3.2`).
  - Cloud Free-Tier: **Groq Cloud API** (`llama-3.3-70b-versatile`), **Google Gemini Free API**.
  - Default parameters: `temperature = 0.1` (determinism and factuality), `max_tokens = 1024`, JSON mode for judging/structured output. The values are configured per-install via `Settings`/`.env` (`generation_temperature`, `generation_max_tokens`), not hard-coded in method signatures; for factuality `temperature <= 0.3` is recommended.
- **Ingestion & Queues**:
  - A native background queue: `taskiq` + Postgres (`taskiq-pg`) / `pgmq` (durable queues in Supabase without needing Redis at the start).
  - Parsing: `Docling` / `pypdf` + `python-docx` preserving headings, tables and structure (support for `.pdf`, `.docx`, `.md`, `.txt`, `.xlsx`, `.pptx`, `.html`, `.epub`).
  - File Watcher: a debounce interval of 2-3 sec to prevent reading partially written files.
- **CI/CD & Deployment**:
  - **Docker**: a multi-stage `Dockerfile` (separation of `app-bot` and `ingestion-worker`), a `docker-compose.yml` for a local replica of the environment.
  - **GitHub Actions**:
    - A code-check workflow: Linting (`ruff`), Type Check (`mypy`/`pyright`), Integration Tests (`pytest` + `testcontainers-postgres`), an Evaluation Gate (running the regression test on the golden dataset).
    - Secret management: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` are passed strictly through GitHub Actions Secrets.
- **Observability & Evaluation**:
  - Tracing: **Langfuse** (Self-hosted docker / Cloud Free Tier).
  - Evaluation: **RAGAS** (Faithfulness, Answer Relevance, Context Precision, Context Recall) + collecting telemetry of user reactions directly from Telegram.

---

## 📦 3. Data-flow architecture and system modules

```
[ User: Telegram / API / Web ]
                 │
                 ▼
┌────────────────────────────────────────────────────────┐
│               Query Understanding Layer                │
│  - Session & Conversation Memory Manager (K=5 turns)   │
│  - Query Condensation / Rewriter (Multi-turn → Stand.) │
│  - Intent Router (Chit-Chat / Document RAG / Meta)     │
└────────────────────────┬───────────────────────────────┘
                         │
                         ▼
┌────────────────────────────────────────────────────────┐
│               Hybrid Retrieval & Fusion                │
│  - Dense Embedding (bge-m3 dense, 1024-dim, cosine)    │
│  - Sparse/Lexical Vector (bge-m3 sparsevec)            │
│  - Supabase pgvector HNSW + RRF (Reciprocal Rank)      │
│  - Access Control (RLS & access_level <= user_level)   │
└────────────────────────┬───────────────────────────────┘
                         │ (Top-30 Chunks)
                         ▼
┌────────────────────────────────────────────────────────┐
│              Reranking & Confidence Gate               │
│  - FlashRank / Cross-Encoder Reranker                  │
│  - Score Threshold Check (Score < τ=0.35 → No Match)   │
│  - Parent Chunks Hydration (Small-to-Big Resolution)   │
└────────────────────────┬───────────────────────────────┘
                         │ (Top-3..5 Parent Contexts)
                         ▼
┌────────────────────────────────────────────────────────┐
│           Context Budgeting & Generation               │
│  - Token Budget Packing & Deduplication                │
│  - Anti-Prompt-Injection Wrapping                      │
│  - LLM Runner (Groq / Ollama) with Fallback & Retry    │
│  - Structured Citation Mapping [1], [2]                │
└────────────────────────┬───────────────────────────────┘
                         │ (Streaming Token Stream)
                         ▼
[ The user receives the answer + inline 👍/👎 buttons ]
```

### 3.1. Database schema (Supabase / Postgres DDL)
1. **`documents`**:
   - `id` (UUID, PK), `source_type` (text: local_folder, s3, etc.), `source_uri` (text), `content_hash` (text, a unique sha256 for deduplication).
   - `status` (`pending`, `processing`, `indexed`, `failed`), `error_message` (text).
   - `language` (text, default `'ru'`), `ingestion_job_id` (UUID for tracking the ingest session).
   - `access_level` (int, default 0 for RBAC), `metadata` (jsonb), `created_at`, `updated_at`.
2. **`parent_chunks`**:
   - `id` (UUID, PK), `document_id` (FK -> `documents.id` ON DELETE CASCADE).
   - `content` (text), `token_count` (int), `chunk_index` (int), `metadata` (jsonb).
3. **`child_chunks`**:
   - `id` (UUID, PK), `parent_id` (FK -> `parent_chunks.id` ON DELETE CASCADE).
   - `content` (text), `token_count` (int), `chunk_index` (int).
   - `embedding` (`vector(1024)` with an HNSW index: `m=16, ef_construction=64, vector_cosine_ops`).
   - `sparse_vector` (`sparsevec` for lexical matching with an HNSW index).
   - `embedding_model_id` (text: `'BAAI/bge-m3'`), `embedding_version` (int: `1`).
4. **`conversations` & `messages`**:
   - `conversation_id` (UUID), `user_id` (text), `role` (user/assistant), `content` (text), `feedback` (int: -1/+1), `langfuse_trace_id` (text), `created_at`.

### 3.2. Query Understanding & Multi-turn
- **Dialog memory**: the Telegram bot stores the user's last $K=5$ messages within an active session.
- **Query Condensation**: a lightweight model/prompt reformulates the user's contextual question (e.g. *"And how much does it cost?"*) into a standalone search query (e.g. *"Cost of the Enterprise RAG plan"*).
- **Confidence Gate**: if after the FlashRank stage the maximum score of the found chunks is below the given threshold $\tau = 0.35$, the system returns a polite message about the absence of relevant data in the base without wasting LLM tokens.

### 3.3. Hybrid Search & Reciprocal Rank Fusion (RRF)
- The search is performed separately over two arms (Dense HNSW + Sparse Lexical), retrieving Top-$K$ ($K=30$) candidates with the runtime parameter `SET hnsw.ef_search = 40;`.
- **RRF formula**:
  $$RRF(d) = \frac{\alpha}{k + r_{dense}(d)} + \frac{\beta}{k + r_{sparse}(d)}$$
  where:
  - $k = 60$ (the standard smoothing constant).
  - $\alpha = 0.6$ (the weight of the semantic vector search).
  - $\beta = 0.4$ (the weight of the lexical sparse search).
  - If a document is absent from one of the lists, its rank in the missing list is taken as $\infty$ (the corresponding term's contribution is 0).

### 3.4. Ingestion & Chunking (Small-to-Big)
- **Token-aware splitter**: splitting is done by the real tokens of the tokenizer in use (`tiktoken` / HuggingFace tokenizer), avoiding accidental word breaks.
- **Semantic Chunking (optional module)**: splitting at the boundaries of jumps in sentence cosine similarity for unstructured continuous texts.
- **Parent Chunks**: 800-1200 tokens (logical sections with heading and structure metadata).
- **Child Chunks**: 150-250 tokens (granular meaning units with a 20-40 token overlap).
- **Near-Deduplication**: duplicate control at the document level (`sha256`) and an optional cosine near-duplicate check for chunks.
- **Batching**: embedding generation for BGE-M3 is done in batches of 32 chunks for maximum CPU/GPU speed.
- **Idempotent ingest**: computing `sha256(file_content)` prevents re-processing of unchanged files.
- **File Watcher**: protection against race conditions via a debounce timer (2-3 seconds).

### 3.5. LLM fault tolerance and security
- **Resilience**: wrapping all external network calls (`tenacity`): 3 attempts, exponential backoff, switching to a backup provider (e.g. from Groq to Gemini Free or Ollama) when the rate limit is exhausted.
- **Prompt Injection Defense**: context isolation in the system instructions:
  ```xml
  <doc_context>
    <chunk id="1" source="doc.pdf">...</chunk>
  </doc_context>
  ```
- **Citations**: the LLM returns an answer with references to the identifiers of the provided chunks `[1]`, which are mapped to the source documents in the Telegram answer.
- **Streaming & Cancellation**: token streaming into Telegram with throttling of the message-edit rate (once every 0.5-1.0 sec), with correct handling of generation stop on user interruption.

---

## 🛠️ 4. Implementation roadmap

1. **Phase 1: Specification, Tooling and CI/CD** (updating the specification, `.gitignore`, a GitHub Actions CI with linting and tests, setting up the repository).
2. **Phase 2: Markdown Core (`core/*.md`)** (`soul.md`, `architecture.md`, `storage_schema.md`, `modules_spec.md`, `eval_and_ops.md`).
3. **Phase 3: Storage, DDL and base contracts** (Supabase migrations, Pydantic V2 models, the DI container).
4. **Phase 4: Ingestion Pipeline & Embedder** (Docling/PDF parser, the Parent-Child splitter, BGE-M3 Dense+Sparse generation, the Taskiq-pg worker).
5. **Phase 5: Retrieval, Reranker & Confidence Gate** (HNSW + the RRF SQL procedure, FlashRank, the Query Rewriter / Condensation).
6. **Phase 6: Telegram Bot & LLM Generation** (Aiogram 3, answer streaming, 👍/👎 feedback, Langfuse tracing).
7. **Phase 7: Evaluation and regression testing** (a RAGAS test on the golden dataset, integration into CI).
