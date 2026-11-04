# Enterprise RAG Specification & Architecture Blueprint (v2.2)

Документ определяет архитектурный базис, контракты и инженерные стандарты для построения расширяемой Enterprise RAG системы с поддержкой работы на Free-tier / Local / Cloud инфраструктуре.

Спецификация служит основой для создания ядра Markdown-спецификаций (`core/soul.md`, `core/architecture.md`, `core/storage_schema.md`, `core/modules_spec.md`, `core/licensing_and_distribution.md`, `core/eval_and_ops.md`).

> **Статус документа.** Это обзорный исторический blueprint. Канонический и приоритетный источник истины — файлы `core/*.md`. При расхождениях следовать `core/*.md`. В частности, **основной интерфейс конечного пользователя — встраиваемый Web-виджет** (`core/channels.md`), Telegram — вторичный канал.

---

## 🎯 1. Архитектурные принципы, NFR / SLO и Безопасность

### 1.1. Концепция: Гибридная модель (SaaS Cloud + Protected On-Premise Box)
- **SaaS Cloud (Multi-Tenant)**: Хостинг на экономичном VPS / Docker Compose (без Kubernetes на старте), жесткая изоляция через `tenant_id` и PostgreSQL RLS, хранение в S3 (Cloudflare R2 / MinIO / Selectel), подключение через встраиваемый Web-виджет (`<script>`) и Telegram-бот.
- **On-Premise Box (Enterprise)**: Поставка ядра в виде защищенных бинарных модулей (Cython / PyArmor) через приватный Docker Registry, криптографическая офлайн-валидация лицензий (Ed25519) + онлайн Heartbeat, возможность развертывания клиентом в Docker Compose / K8s со своими S3 и локальными LLM.
- **Строгая модульность и типизация**: Pydantic V2, Clean Architecture, Dependency Injection.

### 1.2. Целевые показатели качества и надежности (SLO / NFR)
- **Retrieval Метрики (на золотом датасете)**:
  - **Hit Rate@5**: $\ge 0.85$
  - **MRR@5**: $\ge 0.75$
  - **nDCG@5**: $\ge 0.80$
- **P95 Retrieval Latency**: $< 400$ мс (векторный поиск + sparse/лексический поиск + RRF слияние + FlashRank реранкинг при `ef_search = 40`).
- **P95 Time To First Token (TTFT)**: $< 1.5$ с при стриминге ответов пользователю в Telegram/SSE.
- **Отказоустойчивость**: Circuit Breaker, Exponential Backoff и Fallback-провайдеры при Rate Limit / сбоях внешних бесплатных API.

### 1.3. Безопасность, Rate Limiting и Data Governance
- **Защита от Indirect Prompt Injection**: Все чанки контекста изолируются в системных промптах строгими тегами `<untrusted_doc_context>` с инструкцией игнорировать любые системные команды внутри документов.
- **RBAC & Data Isolation**: Разделение прав доступа на уровне базы данных через `access_level` документов/пользователей и PostgreSQL Row-Level Security (RLS).
- **Rate Limiting & Throttling**: Защита публичных каналов (Web-виджет, Telegram) от спама через middleware (раздельные политики: публичный `widget_rate_limit_per_minute`, приватная админка `admin_rate_limit_per_minute`; graceful throttle response).
- **Data Residency (Supabase Cloud)**: Хранение чувствительных данных в заданном регионе ЕС/локальном контуре с шифрованием конфиденциальных полей в `metadata`.

---

## 🏗️ 2. Технологический стек и Инфраструктура

- **Основной интерфейс**: встраиваемый **Web-виджет** (`<script>`, Shadow DOM, white-label) поверх FastAPI (REST / SSE stream) — см. `core/channels.md`. Вторичные каналы: Telegram Bot на `aiogram 3.x` (Long polling / Webhook, inline-кнопки 👍/👎, typing-статус, стриминг с троттлингом и cancellation handling), REST/SSE для интеграций и CLI (air-gapped Box).
- **Абстракция каналов (`BaseChannel`)**: единый канало-нейтральный контракт (`ChannelEvent`) — «один вход, несколько выходов»: Web-виджет, Telegram, REST/SSE, CLI.
- **Хранилище**: **Supabase (Managed PostgreSQL 15+ с pgvector 0.7+)**:
  - Векторный поиск: HNSW индекс с косинусным расстоянием (`vector_cosine_ops`, 1024 dim для нормализованного BGE-M3, параметры построения: `m=16, ef_construction=64`, параметр запроса: `ef_search=40`).
  - Гибридное лексическое плечо: Dense + Sparse (`BAAI/bge-m3` с хранением sparse-векторов в типе `sparsevec` в pgvector 0.7+ с HNSW/IVFFlat индексом) либо `bm25s`.
  - Управление миграциями: `supabase migrations` / `alembic`.
- **Эмбеддинги и Реранкинг**:
  - **Dense + Sparse Embedder**: `BAAI/bge-m3` (через `fastembed` / `sentence-transformers` локально на CPU/GPU, с поддержкой batching: `batch_size = 32`).
  - **Reranker**: `FlashRank` (локальный легковесный cross-encoder) или `bge-reranker-small`.
- **LLM Провайдеры и параметры генерации**:
  - Локально: **Ollama** (`qwen2.5:7b-instruct`, `llama3.2`).
  - Cloud Free-Tier: **Groq Cloud API** (`llama-3.3-70b-versatile`), **Google Gemini Free API**.
  - Дефолтные параметры: `temperature = 0.1` (детерминированность и фактологичность), `max_tokens = 1024`, режим JSON для судейства/структурированного вывода. Значения настраиваются per-install через `Settings`/`.env` (`generation_temperature`, `generation_max_tokens`), а не хардкодятся в сигнатурах методов; для фактологичности рекомендуется `temperature <= 0.3`.
- **Ingestion & Очереди**:
  - Нативная фоновая очередь: `taskiq` + Postgres (`taskiq-pg`) / `pgmq` (durable очереди в Supabase без необходимости Redis на старте).
  - Парсинг: `Docling` / `pypdf` + `python-docx` с сохранением заголовков, таблиц и структуры (поддержка `.pdf`, `.docx`, `.md`, `.txt`, `.xlsx`, `.pptx`, `.html`, `.epub`).
  - File Watcher: Debounce-интервал 2–3 сек для предотвращения чтения частично записанных файлов.
- **CI/CD & Деплой**:
  - **Docker**: Multi-stage `Dockerfile` (разделение `app-bot` и `ingestion-worker`), `docker-compose.yml` для локальной реплики окружения.
  - **GitHub Actions**:
    - Workflow проверки кода: Linting (`ruff`), Type Check (`mypy`/`pyright`), Integration Tests (`pytest` + `testcontainers-postgres`), Evaluation Gate (запуск регрессионного теста на золотом датасете).
    - Управление секретами: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `GROQ_API_KEY`, `TELEGRAM_BOT_TOKEN` передаются строго через GitHub Actions Secrets.
- **Observability & Evaluation**:
  - Трейсинг: **Langfuse** (Self-hosted docker / Cloud Free Tier).
  - Оценка: **RAGAS** (Faithfulness, Answer Relevance, Context Precision, Context Recall) + сбор телеметрии пользовательских реакций прямо из Telegram.

---

## 📦 3. Архитектура потоков данных и модули системы

```
[ Пользователь: Telegram / API / Web ]
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
│  - LLM Runner (Groq / Ollama) с Fallback & Retry       │
│  - Structured Citation Mapping [1], [2]                │
└────────────────────────┬───────────────────────────────┘
                         │ (Streaming Token Stream)
                         ▼
[ Пользователь получает ответ + Inline-кнопки 👍/👎 ]
```

### 3.1. Схема базы данных (Supabase / Postgres DDL)
1. **`documents`**:
   - `id` (UUID, PK), `source_type` (text: local_folder, s3, etc.), `source_uri` (text), `content_hash` (text, уникальный sha256 для дедупликации).
   - `status` (`pending`, `processing`, `indexed`, `failed`), `error_message` (text).
   - `language` (text, default `'ru'`), `ingestion_job_id` (UUID для трекинга сессии инжеста).
   - `access_level` (int, default 0 для RBAC), `metadata` (jsonb), `created_at`, `updated_at`.
2. **`parent_chunks`**:
   - `id` (UUID, PK), `document_id` (FK -> `documents.id` ON DELETE CASCADE).
   - `content` (text), `token_count` (int), `chunk_index` (int), `metadata` (jsonb).
3. **`child_chunks`**:
   - `id` (UUID, PK), `parent_id` (FK -> `parent_chunks.id` ON DELETE CASCADE).
   - `content` (text), `token_count` (int), `chunk_index` (int).
   - `embedding` (`vector(1024)` с HNSW индексом: `m=16, ef_construction=64, vector_cosine_ops`).
   - `sparse_vector` (`sparsevec` для lexical matching с HNSW индексом).
   - `embedding_model_id` (text: `'BAAI/bge-m3'`), `embedding_version` (int: `1`).
4. **`conversations` & `messages`**:
   - `conversation_id` (UUID), `user_id` (text), `role` (user/assistant), `content` (text), `feedback` (int: -1/+1), `langfuse_trace_id` (text), `created_at`.

### 3.2. Query Understanding & Multi-turn
- **Диалоговая память**: Telegram-бот сохраняет последние $K=5$ реплик пользователя в рамках активной сессии.
- **Query Condensation**: Легковесная модель/промпт переформулирует контекстный вопрос пользователя (например, *"А сколько это стоит?"*) в самостоятельный поисковый запрос (например, *"Стоимость тарифа Enterprise RAG"*).
- **Confidence Gate**: Если после этапа FlashRank максимальный score найденных чанков ниже заданного порога $\tau = 0.35$, система возвращает вежливое сообщение об отсутствии релевантных данных в базе без бесполезной траты токенов LLM.

### 3.3. Hybrid Search & Reciprocal Rank Fusion (RRF)
- Поиск выполняется раздельно по двум плечам (Dense HNSW + Sparse Lexical), извлекая Top-$K$ ($K=30$) кандидатов при runtime-параметре `SET hnsw.ef_search = 40;`.
- **Формула RRF**:
  $$RRF(d) = \frac{\alpha}{k + r_{dense}(d)} + \frac{\beta}{k + r_{sparse}(d)}$$
  где:
  - $k = 60$ (стандартная сглаживающая константа).
  - $\alpha = 0.6$ (вес семантического векторного поиска).
  - $\beta = 0.4$ (вес лексического sparse поиска).
  - Если документ отсутствует в одном из списков, его ранг в отсутствующем списке принимается за $\infty$ (вклад соответствующего слагаемого равен 0).

### 3.4. Ingestion & Chunking (Small-to-Big)
- **Token-aware сплиттер**: Нарезка ведется по реальным токенам используемого токенизатора (`tiktoken` / HuggingFace tokenizer), исключая случайный разрыв слов.
- **Semantic Chunking (опциональный модуль)**: Нарезка по границам скачков косинусного сходства предложений для неструктурированных сплошных текстов.
- **Parent Chunks**: 800–1200 токенов (логические секции с метаданными заголовков и структуры).
- **Child Chunks**: 150–250 токенов (точечные смысловые единицы с перекрытием 20–40 токенов).
- **Near-Deduplication**: Контроль дубликатов на уровне документов (`sha256`) и опциональный cosine near-duplicate check для чанков.
- **Batching**: Генерация эмбеддингов для BGE-M3 выполняется батчами по 32 чанка для максимальной скорости CPU/GPU.
- **Идемпотентный инжест**: Расчет `sha256(file_content)` предотвращает повторную обработку неизмененных файлов.
- **File Watcher**: Защита от race conditions через debounce-таймер (2–3 секунды).

### 3.5. Отказоустойчивость и безопасность LLM
- **Resilience**: Обертка всех внешних сетевых вызовов (`tenacity`): 3 попытки, экспоненциальная задержка, переключение на резервный провайдер (например, с Groq на Gemini Free или Ollama) при исчерпании rate limit.
- **Prompt Injection Defense**: Изоляция контекста в системных инструкциях:
  ```xml
  <doc_context>
    <chunk id="1" source="doc.pdf">...</chunk>
  </doc_context>
  ```
- **Цитаты**: LLM возвращает ответ со ссылками на идентификаторы переданных чанков `[1]`, которые маппятся на исходные документы в ответе Telegram.
- **Streaming & Cancellation**: Потоковая передача токенов в Telegram с троттлингом частоты редактирования сообщения (1 раз в 0.5–1.0 сек), с корректной обработкой остановки генерации при прерывании пользователем.

---

## 🛠️ 4. Roadmap реализации

1. **Фаза 1: Спецификация, Tooling и CI/CD** (Обновление спецификации, `.gitignore`, GitHub Actions CI с линтингом и тестами, настройка репозитория).
2. **Фаза 2: Markdown Core (`core/*.md`)** (`soul.md`, `architecture.md`, `storage_schema.md`, `modules_spec.md`, `eval_and_ops.md`).
3. **Фаза 3: Хранилище, DDL и Базовые контракты** (Supabase миграции, Pydantic V2 модели, DI контейнер).
4. **Фаза 4: Ingestion Pipeline & Embedder** (Docling/PDF парсер, Parent-Child сплиттер, BGE-M3 Dense+Sparse генерация, Taskiq-pg воркер).
5. **Фаза 5: Retrieval, Reranker & Confidence Gate** (HNSW + RRF SQL процедура, FlashRank, Query Rewriter / Condensation).
6. **Фаза 6: Telegram Bot & LLM Generation** (Aiogram 3, стриминг ответов, feedback 👍/👎, Langfuse трейсинг).
7. **Фаза 7: Оценка и Регрессионное тестирование** (RAGAS тест на золотом датасете, интеграция в CI).
