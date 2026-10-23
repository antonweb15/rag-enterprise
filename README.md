# 🏢 Enterprise RAG (Retrieval-Augmented Generation)

> Модульная, отказоустойчивая корпоративная RAG-система на базе локального / free-tier стека с интерфейсом в Telegram и хранилищем в Supabase (PostgreSQL + pgvector).

---

### ⚡ Ключевые особенности

- **Small-to-Big Chunking**: Точечный векторный поиск по дочерним чанкам (150–250 токенов) с передачей в LLM полного родительского контекста (800–1200 токенов).
- **Hybrid Search + RRF**: Объединение семантического (Dense HNSW) и лексического (Sparse) поиска `BAAI/bge-m3` по алгоритму Reciprocal Rank Fusion ($\alpha=0.6, \beta=0.4, k=60$).
- **Reranker & Confidence Gate**: Фильтрация шума через FlashRank и автоматическая отсечка нерелевантных вопросов при $\max(score) < \tau$ ($\tau=0.35$).
- **Multi-turn Memory & Condensation**: Полноценный контекстный диалог в Telegram с переформулированием зависимых вопросов в самостоятельные поисковые запросы.
- **Zero Hallucination & Security**: Изоляция контекста в тегах `<untrusted_doc_context>`, защита от Indirect Prompt Injection и точные сноски на источники `[1]`, `[2]`.
- **100% Free / Local Stack Ready**: `FastEmbed` BGE-M3 + Groq (`llama-3.3-70b`) / Gemini Free / Ollama + Supabase Cloud.

---

### 🛠️ Технологический стек

| Слой | Технологии |
|---|---|
| **Интерфейс** | Telegram Bot (`aiogram 3.x`), расширяемая абстракция `BaseChannel` |
| **База данных** | Supabase (PostgreSQL 15+, `pgvector 0.7+`, HNSW, RLS, `sparsevec`) |
| **Эмбеддинги & Реранкинг** | `BAAI/bge-m3` (Dense 1024-dim + Sparse), `FlashRank` |
| **LLM & Оркестрация** | Groq Cloud (`llama-3.3-70b`), Google Gemini Free, Ollama (`qwen2.5:7b`) |
| **Фоновые задачи & ETL** | `taskiq` + Postgres queue (`taskiq-pg`), `Docling` парсер |
| **Observability & Eval** | Langfuse Tracing, RAGAS (валидация на золотом датасете) |

---

### 🚀 Быстрый старт (Docker)

```bash
# 1. Клонирование репозитория
git clone git@github.com:antonweb15/rag-enterprise.git
cd rag-enterprise

# 2. Настройка переменных окружения
cp .env.example .env

# 3. Запуск сервисов
docker compose up -d
```

---

### 📚 Спецификация и Markdown-ядро (`core/`)

Полная архитектурная спецификация и проектные контракты зафиксированы в каталоге [`core/`](./core/INDEX.md):
- [**`core/INDEX.md`**](./core/INDEX.md) — Манифест ядра и регламент архитектурных изменений.
- [**`core/soul.md`**](./core/soul.md) — Миссия ассистента, системные промпты, правила безопасности и стиль ответов.
- [**`core/architecture.md`**](./core/architecture.md) — Потоки данных (Query Flow, Ingestion Flow, матрица отказоустойчивости).
- [**`core/storage_schema.md`**](./core/storage_schema.md) — DDL миграции Supabase, SQL-функция RRF, RLS политики.
- [**`core/modules_spec.md`**](./core/modules_spec.md) — Python/Pydantic V2 контракты, интерфейсы провайдеров и Taskiq задачи.
- [**`core/eval_and_ops.md`**](./core/eval_and_ops.md) — Регламент RAGAS-тестирования, Langfuse трейсинг, Docker и GitHub Actions CI/CD.
