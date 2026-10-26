# Architecture & Data Flow Specification (`core/architecture.md`)

## 1. Обзор архитектуры

Система построена по слоистой, слабосвязанной архитектуре (Clean Architecture / Hexagonal) с поддержкой асинхронной обработки и Dependency Injection.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           1. Channel Ingress Layer                          │
│   - Telegram Bot (aiogram 3.x) with Throttling & Inline Actions (👍/👎)     │
│   - Web Widget (Embed JS / SSE Stream / Shadow DOM)                         │
│   - Pluggable Channel Abstraction (FastAPI REST / SSE, CLI)                 │
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
│   - Supabase HNSW Search (Top-30 candidates, SET hnsw.ef_search = 40)       │
│   - Multi-tenant RRF (α=0.6, β=0.4, k=60) + WHERE tenant_id & access_level │
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

## 2. Жизненный цикл обработки запроса (Query Flow)

1. **Входное событие**: Пользователь отправляет сообщение в Telegram.
2. **Аутентификация & RBAC**:
   - `channel_user_id` (Telegram ID) сверяется с таблицей `users` (или кэшем сессии).
   - Извлекается авторизованный `user_access_level` (по умолчанию `0: Public`).
3. **Rate Limiting**: `ThrottlingMiddleware` проверяет лимит (не более 5 сообщений в минуту на `user_id`).
4. **Intent Routing**:
   - `chit_chat` $\to$ быстрый ответ легковесной LLM без обращения к БД.
   - `meta_command` $\to$ справка о состоянии системы и доступных источниках.
   - `rag_search` $\to$ передача в конвейер RAG.
5. **Конденсация вопроса (Multi-turn)**:
   - Извлекаются последние $K=5$ сообщений диалога из таблицы `messages`.
   - Если вопрос зависит от контекста, легковесный LLM генерирует самостоятельный `standalone_query`.
6. **Векторизация и Sparse-кодирование**:
   - `bge-m3` в один проход генерирует плотный вектор (1024 float) и разреженный вектор (sparse dictionary).
7. **Гибридный поиск в Supabase (PostgreSQL)**:
   - Выполняется хранимая функция `hybrid_search_rrf(query_dense, query_sparse, user_access_level, top_candidates=30)`.
   - Первичный фильтр `WHERE d.access_level <= user_access_level` исключает недоступные документы.
   - Находятся Top-30 кандидатов по косинусному расстоянию (HNSW) и Top-30 по sparsevec, объединяясь по формуле RRF:
     $$RRF(d) = \frac{0.6}{60 + r_{dense}(d)} + \frac{0.4}{60 + r_{sparse}(d)}$$
8. **Реранкинг (FlashRank)**:
   - Полученные Top-30 чанков переранжируются Cross-Encoder моделью.
   - **Confidence Gate**: Если $\max(score) < 0.35$, поиск прерывается, пользователю возвращается сообщение об отсутствии информации.
9. **Small-to-Big Hydration**:
   - По `parent_id` отобранных лучших child-чанков (Top 3–5) загружается полный текст соответствующих `parent_chunks`.
   - Устраняются дубликаты родительских секций.
10. **Сборка промпта и Стриминг**:
   - Формируется защищённый контекст внутри `<untrusted_doc_context>`.
   - Ответ стримится в Telegram с интервалом обновления сообщения 0.5–1.0 сек.
   - В конце формируются кнопки для сбора обратной связи (👍 / 👎).
11. **Трейсинг**: Все спаны, токены и время отправляются в Langfuse.

---

## 3. Архитектура фонового инжеста данных (Ingestion ETL Flow)

```
[ Локальная папка ./data/ / S3 (Cloudflare R2 / MinIO / AWS) / Confluence ]
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
│  4. Transactional DB Upsert (Supabase):                │
│     - INSERT documents (tenant_id) -> parent -> child  │
│     - UPDATE status = 'indexed'                        │
└────────────────────────────────────────────────────────┘
```

---

## 4. Контур отказоустойчивости (Resilience Matrix)

| Компонент | Возможный сбой | Механизм защиты |
|---|---|---|
| **Groq Cloud API** | Rate Limit (HTTP 429) / 5xx | Tenacity Retry (3 attempts, backoff 1s..5s) $\to$ Fallback на Gemini Free или локальную Ollama |
| **Embedder / CPU** | OOM / Высокая нагрузка | Батчинг строго по 32 чанка, ограничение concurrency воркера |
| **File Watcher** | Частичная запись файла | Debounce 2.5 сек, проверка `st_size` стабильности перед чтением |
| **Telegram API** | Flood control / Rate limit | Очередь редактирования сообщений с троттлингом (min 0.8s между editMessageText) |
| **Supabase DB** | Network timeout / Pool exhaustion | Connection Pooling (Supavisor / SQLAlchemy Pool), timeout 10s |
