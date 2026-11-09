# 🏢 Enterprise RAG (Retrieval-Augmented Generation)

> A modular, fault-tolerant enterprise RAG system built on a local / free-tier stack with Web widget / Telegram / REST-SSE interfaces and storage on PostgreSQL 15+ / pgvector (Supabase or self-hosted).

---

### ⚡ Key features

- **Small-to-Big Chunking**: precise vector search over child chunks (150-250 tokens) with the full parent context (800-1200 tokens) passed to the LLM.
- **Hybrid Search + RRF**: combining semantic (Dense HNSW) and lexical (Sparse) `BAAI/bge-m3` search via the Reciprocal Rank Fusion algorithm ($\alpha=0.6, \beta=0.4, k=60$).
- **Reranker & Confidence Gate**: noise filtering via FlashRank and automatic cut-off of irrelevant questions when $\max(score) < \tau$ ($\tau=0.35$).
- **Multi-turn Memory & Condensation**: a fully contextual dialog in Telegram, rephrasing dependent questions into standalone search queries.
- **Zero Hallucination & Security**: context isolation in `<untrusted_doc_context>` tags, protection against Indirect Prompt Injection and precise source footnotes `[1]`, `[2]`.
- **100% Free / Local Stack Ready**: `FastEmbed` BGE-M3 + Groq (`llama-3.3-70b-versatile`) / Gemini Free / Ollama (`qwen2.5:7b-instruct`) + Supabase Cloud.

---

### 🛠️ Technology stack

| Layer | Technologies |
|---|---|
| **Interface** | Web widget (Shadow DOM, white-label, primary), Telegram Bot (`aiogram 3.x`), Admin SPA (`/api/v1/admin/*`), the extensible `BaseChannel` abstraction (`web`/`telegram`/`rest_sse`/`cli`) |
| **Database** | PostgreSQL 15+ + `pgvector 0.7+` (Supabase or self-hosted), HNSW, RLS, `sparsevec` |
| **Embeddings & Reranking** | `BAAI/bge-m3` (Dense 1024-dim + Sparse), `FlashRank` |
| **LLM & Orchestration** | Groq Cloud (`llama-3.3-70b-versatile`), Google Gemini Free, Ollama (`qwen2.5:7b-instruct`) |
| **Background tasks & ETL** | `taskiq` + Postgres queue (`taskiq-pg`), `Docling` parser |
| **Observability & Eval** | Langfuse Tracing, RAGAS (validation on a golden dataset) |

---

### 🚀 Quick start (Docker) — ⚠️ Phase 3+ (planned)

> **Important:** Stage A is implemented (foundation: monorepo, tooling, CI) — the monorepo skeleton (`packages/rag_core`, `services/api`, `apps/admin`, `apps/widget`) and `.env.example` were added. The application code, `Dockerfile` and `docker-compose.yml` will appear in later stages — the scenario below will work once the runtime is implemented. For now this is the target launch interface, not a ready-to-run command.

```bash
# 1. Clone the repository
git clone git@github.com:antonweb15/rag-enterprise.git
cd rag-enterprise

# 2. Configure environment variables (coming in Phase 3+)
cp .env.example .env

# 3. Start the services (coming in Phase 3+)
docker compose up -d
```

---

### 🌐 Language policy

All content in this repository is English-only (code, comments, docs, configs and commit messages). See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full policy; it is enforced by a CI-blocking test.

---

### 📚 Specification and the Markdown core (`core/`)

The full architectural specification and design contracts are fixed in the [`core/`](./core/INDEX.md) directory:
- [**`core/INDEX.md`**](./core/INDEX.md) — the core manifest and the rules for architectural changes.
- [**`core/soul.md`**](./core/soul.md) — the assistant's mission, system prompts, security rules and answer style.
- [**`core/architecture.md`**](./core/architecture.md) — data flows (Query Flow, Ingestion Flow, the fault-tolerance matrix).
- [**`core/storage_schema.md`**](./core/storage_schema.md) — Supabase DDL migrations, the RRF SQL function, RLS policies.
- [**`core/modules_spec.md`**](./core/modules_spec.md) — Python/Pydantic V2 contracts, provider interfaces and Taskiq tasks.
- [**`core/eval_and_ops.md`**](./core/eval_and_ops.md) — the RAGAS testing procedure, Langfuse tracing, Docker and GitHub Actions CI/CD.
