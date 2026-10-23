# 📖 Enterprise RAG Markdown Core (`core/`)

Директория `core/` содержит каноническое Markdown-ядро проекта, определяющее системные правила, архитектуру, DDL схемы базы данных, программные контракты и регламенты тестирования / CI/CD.

---

## 🗂️ Навигация и структура файлов ядра

1. [**`soul.md`**](./soul.md) — Миссия ассистента, системные правила, стиль коммуникации, изоляция контекста от Indirect Prompt Injection (`<untrusted_doc_context>`), шаблоны промптов генерации, Query Condensation, Intent Router и LLM-as-a-Judge.
2. [**`architecture.md`**](./architecture.md) — Детальная схема слоёв системы, жизненный цикл запроса (Query Flow), конвейер фонового инжеста данных (Ingestion Flow с debounce) и матрица отказоустойчивости (Resilience Matrix).
3. [**`storage_schema.md`**](./storage_schema.md) — DDL миграция PostgreSQL / Supabase, схемы таблиц (`documents`, `parent_chunks`, `child_chunks`, `conversations`, `messages`), HNSW и `sparsevec` индексы, SQL-процедура гибридного поиска `hybrid_search_rrf` и RLS политики безопасности.
4. [**`modules_spec.md`**](./modules_spec.md) — Строгие Python-контракты и Pydantic V2 схемы (`RawDocument`, `ParentChunk`, `ChildChunk`, `RetrievalCandidate`, `RAGResponse`), базовые абстракции интерфейсов (`BaseChannel`, `BaseEmbedder`, `BaseReranker`, `BaseLLM`, `BaseDocumentSource`) и сигнатуры Taskiq-задач.
5. [**`eval_and_ops.md`**](./eval_and_ops.md) — Методология оценки качества RAGAS на верификационном золотом датасете, дерево спанов Langfuse, конфигурация multi-stage `Dockerfile`, `docker-compose.yml` и GitHub Actions CI/CD пайплайн.

---

## 🔒 Регламент ведения и изменения ядра (Core Governance)

- **Источник истины**: Markdown-ядро в каталоге `core/` является первичным контрактом системы.
- **Порядок внесения изменений**: Любые архитектурные изменения, добавление таблиц/полей в БД или смена интерфейсов сначала документируются в файлах ядра `core/*.md`, проходят ревью и только после этого реализуются в кодовой базе.
- **Связь с проектом**: Корневой обзорный документ проекта находится в [`../README.md`](../README.md).
