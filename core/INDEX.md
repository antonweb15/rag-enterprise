# 📖 Enterprise RAG Markdown Core (`core/`)

Директория `core/` содержит каноническое Markdown-ядро проекта, определяющее системные правила, архитектуру, DDL схемы базы данных, программные контракты и регламенты тестирования / CI/CD.

---

## 🗂️ Навигация и структура файлов ядра

1. [**`soul.md`**](./soul.md) — Миссия ассистента, системные правила, стиль коммуникации, изоляция контекста от Indirect Prompt Injection (`<untrusted_doc_context>`), шаблоны промптов генерации, Query Condensation, Intent Router и LLM-as-a-Judge.
2. [**`architecture.md`**](./architecture.md) — Детальная схема слоёв системы, гибридная модель (SaaS Cloud + Protected On-Premise Box), жизненный цикл запроса (Query Flow), конвейер фонового инжеста данных (Ingestion Flow с debounce), S3 интеграция и матрица отказоустойчивости (Resilience Matrix).
3. [**`licensing_and_distribution.md`**](./licensing_and_distribution.md) — Криптографическая защита лицензий (Ed25519), сборка защищенного ядра (Cython/PyArmor), поставка коробок через приватный Docker Registry, Heartbeat телеметрия и встраиваемый Web-виджет.
4. [**`storage_schema.md`**](./storage_schema.md) — DDL миграция PostgreSQL 15+ / pgvector (Supabase — один из вариантов хостинга), схемы таблиц (`tenants`, `licenses`, `documents`, `parent_chunks`, `child_chunks`, `conversations`, `messages`, `storage_credentials`), HNSW и `sparsevec` индексы, SQL-функция `hybrid_search_rrf` как внутренняя деталь `PgVectorStore`, контракт RLS + SQLAlchemy (`SET LOCAL app.current_tenant_id`) и additive-only миграции.
5. [**`modules_spec.md`**](./modules_spec.md) — Строгие Python-контракты и Pydantic V2 схемы (`LicensePayload`, `TenantContext`, `RawDocument`, `ParentChunk`, `ChildChunk`, `RetrievalCandidate`, `RAGResponse`), базовые абстракции интерфейсов (`BaseLicenseValidator`, `BaseChannel`, `BaseEmbedder`, `BaseReranker`, `BaseLLM`, `BaseDocumentSource`) и сигнатуры Taskiq-задач.
6. [**`eval_and_ops.md`**](./eval_and_ops.md) — Методология оценки качества RAGAS на верификационном золотом датасете, дерево спанов Langfuse, конфигурация multi-stage `Dockerfile`, `docker-compose.yml` (admin / widget / worker), монорепо-тулинг (pnpm + uv), Postgres `LISTEN/NOTIFY`, раздельные rate-limit политики и GitHub Actions CI/CD пайплайн.
7. [**`admin_panel.md`**](./admin_panel.md) — Спецификация приватного операторского контура (Admin SPA + FastAPI `/api/v1/admin/*`): один билд на оба `deployment_mode`, аутентификация `fastapi-users` (HTTP-only cookie + CSRF + TOTP 2FA), RBAC (`admin`/`operator`/`viewer`), разделение сессионного (`HS256`) и лицензионного (`Ed25519`) ключей, файловый менеджер с live-статусами через `LISTEN/NOTIFY`, визуализация RAG-чанков, лицензионный экран и CLI-дублёр для air-gapped.

---

## 🔒 Регламент ведения и изменения ядра (Core Governance)

- **Источник истины**: Markdown-ядро в каталоге `core/` является первичным контрактом системы.
- **Порядок внесения изменений**: Любые архитектурные изменения, добавление таблиц/полей в БД или смена интерфейсов сначала документируются в файлах ядра `core/*.md`, проходят ревью и только после этого реализуются в кодовой базе.
- **Связь с проектом**: Корневой обзорный документ проекта находится в [`../README.md`](../README.md).
