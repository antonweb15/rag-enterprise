# 🏗️ План реализации Enterprise RAG (Коробка + Облако)

> Документ подготовлен на основе Markdown-ядра проекта (`core/*.md`) и обзорного `SPECIFICATION.md`.
> Это **исполнительный план реализации программного продукта**: порядок этапов, тестовое покрытие
> на каждом этапе и пошаговые инструкции для ручных действий человека (аккаунты, доступы, ключи).
>
> **Источник истины** — файлы `core/*.md`. При расхождениях следовать ядру. Любое изменение
> архитектуры/БД/контрактов сначала фиксируется в `core/*.md`, затем реализуется в коде
> (см. `core/INDEX.md` → Core Governance).

---

## 0. Принципы реализации («чтобы ничего не сломать»)

- **Инкрементально и аддитивно.** Каждый этап самодостаточен, оставляет систему в рабочем
  (зелёном CI) состоянии. Миграции БД — только additive-only (`core/eval_and_ops.md` §3.3):
  никаких `DROP COLUMN`/`DROP TABLE`/сужающих `ALTER TYPE`.
- **Один код — два режима поставки.** `saas_cloud` и `on_premise_box` различаются только
  runtime-конфигом (`GET /api/v1/admin/config`). Форки запрещены (`core/architecture.md` §1.1).
- **Контракты вперёд реализации.** Pydantic V2 модели и `Base*`-интерфейсы
  (`core/modules_spec.md`) фиксируются до провайдеров; код зависит от абстракций (DI).
- **Fail-closed безопасность.** Нет тенанта/уровня доступа → пустая выборка. Контракт-тесты
  безопасности (`core/eval_and_ops.md` §1.3) — CI-блокирующие, пишутся вместе с Этапом схемы БД.
- **Тесты — часть каждого этапа (TDD-ориентир).** Этап считается закрытым только при зелёных
  unit/integration/контракт-тестах и непросевших метриках Evaluation Gate.
- **Best practices по умолчанию:** строгая типизация (`mypy --strict`), `ruff check/format`,
  `testcontainers-postgres` для интеграций, секреты только через `.env`/GitHub Secrets,
  никаких заглушек, маскирующих падение CI.

### Матрица отличий Коробка vs Облако (ключевое при реализации)

| Аспект | `saas_cloud` | `on_premise_box` |
|---|---|---|
| Тенанты | multi-tenant (RLS по `tenant_id`) | один тенант |
| Хранилище файлов | S3 / R2 / MinIO | local FS + внешние коннекторы |
| Лицензия | статус из панели вендора | активация `.key`/код + `hardware_id` |
| Langfuse | вендорский self-hosted (проект/тенант) | локальный в compose коробки |
| Bind админки | `0.0.0.0` за reverse-proxy | `127.0.0.1` + SSH-туннель |
| CLI-дублёр | опционально | обязателен (air-gapped) |

---

## 1. Дорожная карта этапов (обзор)

| Этап | Название | Результат (Definition of Done) | Режим |
|---|---|---|---|
| **A** | Фундамент: монорепо, тулинг, CI | `uv`+`pnpm` workspace, `ruff`/`mypy --strict`/`pytest` зелёные, CI-пайплайн | оба |
| **B** | Схема БД, миграции, RLS | Alembic additive-миграции, pgvector/HNSW/sparsevec, RLS + контракт-тесты безопасности | оба |
| **C** | Контракты ядра (Pydantic V2 + `Base*`) | все модели и интерфейсы из `modules_spec.md`, DI-контейнер | оба |
| **D** | Ingestion pipeline | `BaseObjectStorage`, Docling-парсер, Parent-Child сплиттер, BGE-M3, Taskiq-pg воркер | оба |
| **E** | Retrieval + Rerank + Confidence Gate | `PgVectorStore` (dense/sparse), RRF в app-layer, FlashRank, PolicyEvaluator-фильтр | оба |
| **F** | Query Understanding + LLM Generation | Intent Router, Query Condensation, LLM Runner с fallback, цитаты, anti-injection | оба |
| **G** | Каналы доставки (Web-виджет → Telegram → REST/SSE → CLI) | `BaseChannel`, Shadow DOM виджет, брендинг | оба |
| **H** | Observability: Langfuse | trace-контракт, маскировка ПД, fire-and-forget, feedback 👍/👎 | оба |
| **I** | Admin Panel (SPA + `/api/v1/admin/*`) | `fastapi-users`, RBAC, 2FA, файл-менеджер (LISTEN/NOTIFY), §5A управление доступом | оба |
| **J** | Evaluation Gate (RAGAS) | золотой датасет, метрики SLO в CI | оба |
| **K** | Лицензирование (клиент + сервер лицензий) | клиент: Ed25519-валидатор/`hardware_id`/heartbeat/grace; сервер: issuance/revocation/CRL/HSM | клиент — box+cloud; сервер — только вендор |
| **L** | Vendor Backoffice | `/api/v1/backoffice/*`, клиенты/лицензии/коды, реестр коробок, impersonation | cloud/вендор |
| **M** | Защищённая поставка коробки | Cython/PyArmor, приватный Registry, водяные знаки, supply-chain скан (Trivy/Grype+SBOM), релизный CI по тегам | box |
| **N** | Жизненный цикл SaaS-тенанта | provisioning / offboarding / backup-restore / юридическое удаление данных при расторжении | cloud |
| **O** | Нагрузочное тестирование и верификация SLO | k6/locust: P95 retrieval < 400 мс, TTFT < 1.5 с, пропускная способность инжеста | оба |

Порядок строгий: каждый этап опирается на предыдущие. Безопасность (RLS/ACL/контракт-тесты)
внедряется **вместе с Этапом B**, а не в конце.

---

## 2. Детализация этапов (реализация + тесты)

### Этап A — Фундамент: монорепо, тулинг, CI

**Реализация (best practices):**
- Структура монорепо (`core/eval_and_ops.md` §3.6): `apps/admin`, `apps/widget` (pnpm workspaces,
  Vite→`dist/`), `services/api` (FastAPI-приложения), `packages/rag_core` (чистое ядро, `uv` workspace).
- **Каноническое имя пакета ядра — `packages/rag_core`** (единое по всем `core/*.md` и коду;
  формы `packages/core`/`core/` считаются устаревшими и приводятся к `rag_core`).
- `pyproject.toml` уже содержит `ruff`/`mypy --strict`/`pytest`; по мере кода расширять
  `files`/`src` на `services/`, `packages/`. Добавить `[build-system]`, чтобы `packages/rag_core`
  был pip-installable (`pip install -e .`), а не только через `pythonpath`/`mypy_path`.
- `.env.example` со всеми ключами (см. §4; имена = ВЕРХНИЙ регистр полей `Settings`),
  `.gitignore` для `.env`, `dist/`, `data/`.
- CI `.github/workflows/ci.yml`: `ruff check .` + **`ruff format --check .`** (форматтер
  проверяется, не только линт) + `mypy` (strict) → Unit/Integration → Evaluation Gate
  (`core/eval_and_ops.md` §4). ⛔ Запрещены заглушки `ruff check . || echo ok`.

**Тестовое покрытие:**
- Smoke-тест импорта пакетов, тест что CI падает при ошибке линта/типов (самопроверка pipeline).
- `pytest` каркас (`testcontainers-postgres` в dev-зависимостях — для Этапа B).

### Этап B — Схема БД, миграции, RLS, контракт-тесты безопасности

**Реализация (`core/storage_schema.md`; `SPECIFICATION.md` — устаревший обзорный blueprint, не источник истины):**
- Alembic (additive-only), entrypoint `alembic upgrade head`; в Box — pre-upgrade `pg_dump` hook.
- Таблицы: `tenants`, `licenses`, `documents` (c `access_level`), `parent_chunks`
  (денормализованный `access_level`), `child_chunks` (`vector(1024)` HNSW `m=16, ef_construction=64,
  vector_cosine_ops` + `sparsevec` индекс), `conversations`, `messages`, `storage_credentials`.
- Enterprise-слой за флагами лицензии (`enterprise_acl`/`collections`): `groups`, `user_groups`,
  `collections`, `document_collections`, `document_acl`, `audit_log`.
- SQL-функция `hybrid_search_rrf` как **внутренняя деталь** `PgVectorStore`.
- RLS: `SET LOCAL app.current_tenant_id` + `app.user_access_level`; каскад ре-классификации
  (`cascade_access_level` + backfill).
- `sparsevec`: зафиксировать оператор индекса (`sparsevec_cosine_ops`) и fallback на почти
  нулевых нормах (минимальная норма / отсечка), т.к. cosine при нулевой норме неустойчив.
- Backfill канала `'fastapi' → 'rest_sse'` в данных (`channels.md` §2, `storage_schema.md` §2.1)
  выполняется additive-миграцией.

**Тестовое покрытие (CI-блокирующее, `core/eval_and_ops.md` §1.3):**
1. Фильтр уровня во всех плечах (`search_dense/sparse/hybrid`).
2. RLS не «протекает» по пулу соединений (отсутствие GUC → `0`, fail-closed).
3. Каскад ре-классификации + initial backfill.
4. Ре-проверка цитат/гидрации (нет parent-утечки в обход child-фильтра).
5. ACL грант/отзыв — немедленный эффект + запись в `audit_log`.
6. Запрет query-пути под ролью с `BYPASSRLS` (`service_role`).
7. Виджет fail-closed по уровню (anonymous = `access_level 0`).
- Интеграционные тесты на `testcontainers-postgres` (pgvector image).

### Этап C — Контракты ядра (Pydantic V2 + `Base*`)

**Реализация (`core/modules_spec.md`):**
- Модели (ПОЛНЫЙ список `modules_spec.md`, а не выборка): `LicensePayload`, `TenantContext`,
  `UserContext` (`group_ids`/`external_subject`), `RawDocument`, `ParentChunk`, `ChildChunk`,
  `RetrievalCandidate`, `RAGResponse`, `AccessDecision`, `Group`, `Collection`, `DocumentAclEntry`,
  `ChannelEvent`/`ChannelCapabilities`, `BrandingConfig`/`WidgetBranding`/`TelegramBranding`,
  `TraceContext`, `HeartbeatPayload`, `BoxDeployment`, `ObservabilityConfig`, `ImpersonationGrant`,
  `LicenseIssueRequest`/`LicenseRevokeRequest`, `ActivationCode`, `Customer`,
  а также `Settings` (pydantic-settings). (Backoffice-модели `BackofficeUser`/`Order` в
  `modules_spec.md` не определены и в коде не реализуются.)
- Абстракции: `BaseLicenseValidator`, `BaseLicenseServer`, `BaseTenantResolver`,
  `BasePolicyEvaluator` (единый PDP), `BaseQueryRewriter`, `BaseIntentRouter`, `BaseRetriever`
  (+ эталонный `Retriever` с RRF-слиянием в application-layer), `BaseConfidenceGate`,
  `BaseChannel`, `BaseEmbedder`, `BaseReranker`, `BaseLLM`, `BaseDocumentSource`,
  `BaseObjectStorage`, `BaseVectorStore`, `BaseTraceSink`.
  DI-контейнер связывает абстракции с провайдерами.

**Тестовое покрытие:**
- Валидация Pydantic-схем (граничные значения, запрет лишних полей), round-trip сериализация.
- Контракт-тесты абстракций (абстрактные методы обязательны; fake-реализации для DI).

### Этап D — Ingestion Pipeline & Embedder

**Реализация (`core/architecture.md` §3, `core/storage_schema.md`):**
- `BaseObjectStorage` (per-tenant префикс `{tenant_id}/`): S3/R2/MinIO + local FS.
- File Watcher с debounce 2–3 c, проверка стабильности `st_size`, SHA-256 дедупликация.
- Единая задача `process_document_task(tenant_id, storage_ref, content_hash, access_level)`.
- Docling-парсер (`.pdf/.docx/.md/.txt/.xlsx/.pptx/.html/.epub`), token-aware Parent-Child
  сплиттер (parent 800–1200, child 150–250, overlap 20–40), BGE-M3 Dense+Sparse (batch 32).
- Transactional upsert, `status` переходы, `NOTIFY ingest_events`.
- Taskiq + Postgres queue (`taskiq-pg`), retry с exponential backoff.
- **Re-embedding при смене модели**: `embedding_model_id`/`embedding_version` уже в схеме;
  описать задачу bump версии + фоновый пере-эмбеддинг корпуса батчами с окном деплоя
  (старые и новые векторы сосуществуют, переключение атомарно) без простоя поиска.

**Тестовое покрытие:**
- Идемпотентность инжеста (повторный файл с тем же хэшем пропускается).
- Re-embedding: bump `embedding_version` переиндексирует только затронутые чанки, старые
  версии не смешиваются в одной колонке размерности.
- Корректность границ чанков (token-aware, не рвёт слова), размеры в пределах.
- Transactional rollback при сбое embedder; retry-логика.
- Тест батчинга (строго по 32), near-dedup чанков.

### Этап E — Retrieval, Reranker & Confidence Gate

**Реализация (`core/architecture.md` §2 п.7–9):**
- `PgVectorStore.search_dense` / `.search_sparse` (Top-30, `ef_search=40`); слияние RRF
  **в application-layer** (`Retriever.fuse_rrf`, α=0.6, β=0.4, k=60).
- Первичный фильтр `access_level <= user_access_level` + `allowed_document_ids` из
  `AccessDecision` **до** `LIMIT`/RRF.
- FlashRank reranker, Confidence Gate (τ=0.35), Small-to-Big гидрация с ре-проверкой
  доступа через `PolicyEvaluator.can_access_document`.

**Тестовое покрытие:**
- Юнит RRF (корректность формулы, отсутствие в одном списке → вклад 0).
- Confidence Gate (score < τ → «нет информации»).
- Ре-проверка доступа в гидрации (повтор контракт-тестов §1.3 п.4).
- Retrieval SLO на золотом датасете: Hit Rate@5 ≥ 0.85, MRR@5 ≥ 0.75, nDCG@5 ≥ 0.80.

### Этап F — Query Understanding & LLM Generation

**Реализация (`core/soul.md`, `core/architecture.md` §2 п.4–5,10):**
- Intent Router (`rag_search`/`chit_chat`/`meta_command`), Query Condensation (K=5),
  Session Memory Manager.
- LLM Runner (Groq `llama-3.3-70b-versatile` → fallback Gemini Free → Ollama `qwen2.5:7b-instruct`)
  с tenacity retry. **Канонические ID моделей** (единые для `Settings`/конфигов/промптов):
  `llama-3.3-70b-versatile` (Groq), `qwen2.5:7b-instruct` (Ollama).
- Anti-Prompt-Injection упаковка в `<untrusted_doc_context>`, цитаты `[1]`, token budgeting,
  dedup parent-секций, `temperature`/`max_tokens` из `Settings`.
- Завести порт `BaseTraceSink` (no-op реализация) уже здесь, чтобы llm/retrieval сразу писали
  спаны, а Этап H лишь подключал реальный Langfuse-sink (трейсинг не внедряется ретроактивно).

**Тестовое покрытие:**
- Роутинг intent (golden примеры), condensation зависимых вопросов.
- Fallback-цепочка при 429/5xx (mock провайдеров), retry backoff.
- Prompt-injection тест: инструкция в документе игнорируется.
- Корректность маппинга цитат на источники.

### Этап G — Каналы доставки

**Реализация (`core/channels.md`):** порядок — Web-виджет (основной) → Telegram → REST/SSE → CLI.
- `BaseChannel` (один вход `ChannelEvent`, выход по `ChannelCapabilities`).
- Web-виджет: Shadow DOM, Origin allowlist (без wildcard), короткоживущий JWT handshake,
  `data-api-url`, режимы Cloud/Box, `auth_mode` (anonymous/oidc).
- Telegram white-label (`aiogram 3.x`), стриминг-троттлинг 0.8–1.0 c, inline 👍/👎.
- Типизированный брендинг с `config_version`, лимитами и санитизацией.
- Внедрение публичной rate-limit политики `widget_rate_limit_per_minute` (по IP и `session_id`,
  server-side; отдельный процесс `widget-api`) — `core/eval_and_ops.md` §3.5.

**Тестовое покрытие:**
- Нормализация нативных событий → `ChannelEvent` для каждого канала.
- Виджет fail-closed (anonymous = level 0), Origin allowlist отвергает чужой домен.
- Рендер элементов только при соответствующих `supports_*` (бизнес-логика не ветвится).
- Throttling редактирования сообщений Telegram.

### Этап H — Observability: Langfuse

**Реализация (`core/eval_and_ops.md` §2):**
- Trace-контракт (спаны: intent_routing, query_condensation, hybrid_retrieval, reranking,
  llm_call) с обязательными атрибутами `TraceContext`.
- Маскировка ПД (`mask`), **сырой текст чанков не пишется**; sampling+retention per environment.
- Fire-and-forget запись (отказ Langfuse не блокирует RAG), feedback по `trace_id`,
  `messages.langfuse_trace_id`.
- Cloud: один проект на тенанта; Box: локальный Langfuse в compose.

**Тестовое покрытие:**
- Контракт trace (наличие всех спанов/атрибутов), отсутствие сырого текста в трейсах.
- Fire-and-forget: отказ Langfuse → ответ RAG не падает.
- Обновление score при 👍/👎.

### Этап I — Admin Panel (SPA + `/api/v1/admin/*`)

**Реализация (`core/admin_panel.md`):**
- Один билд SPA на оба `deployment_mode`; `GET /api/v1/admin/config` определяет режим.
- `fastapi-users` (HTTP-only cookie + CSRF + TOTP 2FA), RBAC (`admin`/`operator`/`viewer`),
  разделение сессионного (`HS256`) и лицензионного (`Ed25519`) ключей.
- §5A управление доступом: ре-классификация документов, CRUD пользователей/допусков,
  группы/коллекции/ACL с аудитом каждой мутации.
- Файл-менеджер с live-статусами через `LISTEN/NOTIFY` (SSE), визуализация чанков,
  лицензионный экран, блок доступа к Langfuse (URL/логин), CLI-дублёр (`rag-admin`, `typer`) для air-gapped.
- Приватная rate-limit политика `admin_rate_limit_per_minute` (мягкий лимит по
  аутентифицированному пользователю; отдельно от публичного `widget-api`) — `eval_and_ops.md` §3.5.

**Тестовое покрытие:**
- Аутентификация/CSRF/2FA, RBAC-матрица (viewer не мутирует).
- Аудит каждой мутации ACL/допусков (повтор §1.3 п.5).
- SSE статусы инжеста через LISTEN/NOTIFY (не in-memory), fallback polling.
- E2E (Playwright) ключевых сценариев админки.

### Этап J — Evaluation Gate (RAGAS)

**Реализация (`core/eval_and_ops.md` §1):**
- `tests/data/golden_dataset.json` (BEIR/QRELS-совместимый, с `access_level`).
- `python -m tests.eval_ragas`: Faithfulness ≥ 0.90, Answer Relevance ≥ 0.85,
  Context Precision ≥ 0.85, Context Recall ≥ 0.85 + IR-метрики.
- Интеграция как CI-гейт (блокирует merge при деградации).

**Тестовое покрытие:**
- Сам прогон RAGAS — это и есть регрессионный гейт; фиксировать random seed для воспроизводимости.
- Smoke на малой выборке перед полным прогоном.

### Этап K — Лицензирование (клиент + сервер лицензий)

**Реализация (`core/licensing_and_distribution.md`, `core/license_server.md`):**
- **Клиентская часть (box + cloud, входит в поставку):** `BaseLicenseValidator`
  (offline Ed25519), `hardware_id` fingerprint (Box), grace/Restricted Mode, heartbeat-клиент.
  `hardware_id` — **составной fingerprint** с порогом совпадения (не голый `cpu_id+mac`,
  ненадёжный в Docker/VM) + сценарий переактивации (`license_server.md` §5).
- **Серверная часть (ТОЛЬКО контур вендора, в артефакт Box НЕ входит):**
  `BaseLicenseServer` — issuance/revocation/heartbeat/CRL, signing-сервис/HSM/KMS с offline-ключом,
  профили `online_subscription`/`airgap_enterprise`, ротация ключей по key id.

**Тестовое покрытие:**
- Валидация/отклонение подписи Ed25519 (включая подделку/просрочку/CRL).
- Grace Period → Restricted Mode переходы.
- Привязка `hardware_id`, переактивация.
- Heartbeat счётчики обезличены (нет ПД).

### Этап L — Vendor Backoffice

**Реализация (`core/vendor_backoffice.md`):**
- `/api/v1/backoffice/*` (ОТДЕЛЬНО от тенантной админки), роли `BackofficeRole`.
- Разделение `customer` (юрлицо) ↔ `tenant` (инсталляция), каталог клиентов,
  выдача/отзыв/продление лицензий, активационные коды, реестр `box_deployments`,
  кросс-тенантная наблюдаемость, break-glass impersonation с обязательным аудитом.

**Тестовое покрытие:**
- RBAC backoffice-ролей, изоляция от тенантной админки.
- Аудит impersonation (обязателен), выдача/отзыв лицензий отражается в статусе клиента.

### Этап M — Защищённая поставка коробки

**Реализация (`core/eval_and_ops.md` §4, `core/licensing_and_distribution.md`):**
- Релизный CI по тегам `v*`: компиляция `rag_core/licensing/`, `rag_core/retrieval/` в `.so`
  (Cython/PyArmor), сборка закрытого образа, публикация в приватный GHCR.
- **Supply-chain best practice:** скан образа (Trivy/Grype) на CVE + генерация SBOM
  (CycloneDX/SPDX), `.dockerignore` и `HEALTHCHECK` в образе; релиз блокируется при критических CVE.
- Водяные знаки/канареечные метки для атрибуции утечки + юридический контур
  (обфускация — лишь повышение стоимости реверса, не защита IP).

**Тестовое покрытие:**
- Сборка образа и запуск миграций + smoke RAG внутри образа.
- Проверка, что приватные модули скомпилированы (нет исходников в образе).
- Скан Trivy/Grype проходит без критических CVE; SBOM приложен к релизу.

### Этап N — Жизненный цикл SaaS-тенанта

**Реализация (закрывает пробел релиза 6; `core/vendor_backoffice.md`, `core/storage_schema.md`):**
- **Provisioning**: создание `tenant` + `customer`, выдача лицензии, заведение Langfuse-проекта,
  бутстрап админ-пользователя (всё через backoffice, Атомарно).
- **Offboarding/расторжение**: приостановка → экспорт данных клиенту → юридическое удаление
  (hard-delete с каскадом `ON DELETE CASCADE`, удаление S3-префикса `{tenant_id}/`, Langfuse-проекта),
  retention-политика по 152-ФЗ/GDPR.
- **Backup/restore**: регулярные бэкапы (Supabase PITR / `pg_dump`), проверяемое восстановление.

**Тестовое покрытие:**
- Provisioning создаёт изолированный тенант (RLS-проверка непересечения с соседом).
- Offboarding: после удаления нет остатков в БД/S3/Langfuse; аудит операции.
- Restore из бэкапа восстанавливает целостность тенанта.

### Этап O — Нагрузочное тестирование и верификация SLO

**Реализация (`SPECIFICATION.md` §1.2 — целевые NFR; источник истины — `core/eval_and_ops.md`):**
- Нагрузочные сценарии (k6/locust): верификация **P95 retrieval < 400 мс** (при `ef_search=40`),
  **P95 TTFT < 1.5 с**, пропускная способность инжеста (docs/sec) на репрезентативном корпусе.
- Профилирование HNSW/RRF на росте корпуса (проверка денормализации `tenant_id` в чанки).

**Тестовое покрытие:**
- Нагрузочный прогон как nightly/релизный гейт (не в каждом PR); фиксация baseline SLO.
- Алерт при регрессе латентности ниже порога.

---

## 3. 🙋 Что нужно сделать РУКАМИ (инструкции для человека)

> Это действия, которые **нельзя автоматизировать кодом**: регистрация аккаунтов, создание
> проектов во внешних сервисах, генерация ключей, выдача доступов. Выполняются до/во время
> соответствующих этапов. Все полученные значения вписываются в `.env` (локально) и в
> **GitHub Actions Secrets** (для CI) — см. таблицу в §4. ⛔ Никогда не коммить секреты в git.

### 3.1. PostgreSQL + pgvector — хранилище (Supabase ИЛИ self-hosted) — нужно для Этапа B

**Вариант A. Supabase Cloud (рекомендуется для `saas_cloud`):**
1. Зарегистрируйтесь на <https://supabase.com> → **New organization** → **New project**.
2. Выберите **регион** (для data residency — EU, напр. Frankfurt), задайте надёжный
   **Database Password** (сохраните в менеджере паролей).
3. Дождитесь провижининга. Откройте **Project Settings → Database**:
   - Скопируйте **Connection string (URI)** → это `POSTGRES_DSN` (поле `Settings.postgres_dsn`;
     имя env = `POSTGRES_DSN`, НЕ `DATABASE_URL`). Используйте **Connection pooling / Supavisor**,
     порт 6543 для приложения; порт 5432 — для миграций Alembic.
   - ⚠️ Этот DSN — **административный** (роль `postgres`/`service_role`, обходит RLS): только
     для миграций и воркеров. Для user-retrieval используется **отдельный** `POSTGRES_APP_DSN`
     под ролью `app_user` (шаг 6).
4. **Project Settings → API**:
   - `Project URL` → `SUPABASE_URL`.
   - `service_role` secret key → `SUPABASE_SERVICE_ROLE_KEY` (⚠️ только для бэкенда/миграций,
     обходит RLS — никогда не отдавать на фронт и не использовать для user-retrieval, см. §1.3 п.6).
   > ⚠️ `anon`-ключ **не используется** (отдельного поля в `Settings` нет): доступ к БД
   > идёт через `POSTGRES_APP_DSN` + роль `app_user` без `BYPASSRLS`, а не через PostgREST/anon.
5. **Database → Extensions**: включите `vector` (pgvector ≥ 0.7). Проверьте версию
   `SELECT extversion FROM pg_extension WHERE extname='vector';` (нужно ≥ 0.7 для `sparsevec`).
6. Создайте **выделенную роль приложения `app_user` БЕЗ `BYPASSRLS`** для user-retrieval (не `service_role`),
   задайте ей пароль и **обязательно сделайте членом `authenticated`**: `GRANT authenticated TO app_user;`.
   Соберите **`POSTGRES_APP_DSN`** (поле `Settings.postgres_app_dsn`) под ролью `app_user`.
   Иначе политики изоляции (висят `TO authenticated`) не применятся к `app_user`, и RLS вернёт
   **0 строк** (default-deny). Единый источник DDL ролей и политик — `core/storage_schema.md` §0, §4.1.

**Вариант B. Self-hosted Postgres (для `on_premise_box` и локальной разработки):**
1. Поднимается из `docker-compose.yml` (сервис `postgres-pgvector`, образ `pgvector/pgvector:pg15`).
2. Задайте в `.env`: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, соберите `POSTGRES_DSN`.
3. Для Box включите **pre-upgrade `pg_dump` backup hook** (см. `core/eval_and_ops.md` §3.3).
4. Аналогично варианту A: DDL (`storage_schema.md` §0) создаёт роль `app_user`; задайте ей пароль
   и соберите `POSTGRES_APP_DSN` для user-retrieval (`POSTGRES_DSN` — только админ/миграции).

### 3.2. Объектное хранилище файлов (S3 / Cloudflare R2 / MinIO) — нужно для Этапа D

**Cloud (S3-совместимое, напр. Cloudflare R2 / Selectel / AWS S3):**
1. Создайте аккаунт провайдера, создайте **bucket** (напр. `rag-tenant-files`), регион — как у БД.
2. Создайте **Access Key** и **Secret Key** с правами только на этот bucket (принцип наименьших прав).
3. Впишите (имена = поля `Settings`): `S3_ENDPOINT_URL`, `S3_REGION_NAME` (НЕ `S3_REGION`),
   `S3_DEFAULT_BUCKET` (НЕ `S3_BUCKET`), `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`.
4. **S3-безопасность (best practice):** шифрование at-rest (SSE), версионирование объектов,
   **per-tenant креды/префикс `{tenant_id}/`** (least privilege, ключ тенанта не видит чужие
   объекты), доступ к файлам — через **короткоживущие signed URLs**, lifecycle-политики
   (ротация/архивация), блокировка публичного доступа к bucket.

**Box (локальная папка):** смонтируйте каталог в `ingestion-worker` (`./data:/app/data` в compose),
укажите путь в конфиге `BaseObjectStorage=local`. S3 не обязателен.

### 3.3. Langfuse (наблюдаемость) — нужно для Этапа H

**Cloud (вендорский self-hosted Langfuse — единственный production-вариант):**
1. Разверните Langfuse по официальному compose (`langfuse/langfuse`: web+worker+postgres+
   clickhouse+redis+s3/minio). Поставьте за HTTPS (reverse-proxy).
2. В `.env` Langfuse задайте: `DATABASE_URL`, `CLICKHOUSE_URL`, `REDIS_URL`, `SALT`,
   `ENCRYPTION_KEY`, `NEXTAUTH_URL`, `NEXTAUTH_SECRET`, и **`AUTH_DISABLE_SIGNUP=true`**
   (самоРегистрация запрещена — доступ выдаёт только вендор).
3. Создайте **организацию вендора**; вы (суперадмин) — роль `Owner`/`Admin` на организации.
4. **Для каждого тенанта** (модель B — один проект на тенанта):
   - Создайте **проект** с именем = `tenant_id`.
   - Сгенерируйте **API keys проекта** (`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
     `LANGFUSE_HOST`) → передайте в конфиг RAG-бэкенда этого тенанта.
   - Заведите пользователя клиента с ролью **`None` на организации + `Viewer` на его проекте**
     (клиент видит только свой проект, только чтение).
   - Дайте клиенту deep-link `https://<langfuse-host>/project/<project_id>`.

**Box:** локальный Langfuse едет в compose коробки (за `127.0.0.1`); `LANGFUSE_*` генерируются
при первом старте, клиент — владелец инстанса. Вендору уходит только обезличенный heartbeat.

### 3.4. LLM-провайдеры (ключи API) — нужно для Этапа F

1. **Groq** (основной, free-tier): <https://console.groq.com> → **API Keys** → Create →
   `GROQ_API_KEY`. Модель `llama-3.3-70b-versatile`.
2. **Google Gemini** (fallback, free): <https://aistudio.google.com/app/apikey> →
   `GEMINI_API_KEY`.
3. **Ollama** (локальный fallback / Box): установите <https://ollama.com>, выполните
   `ollama pull qwen2.5:7b-instruct`; укажите `OLLAMA_BASE_URL` (напр. `http://localhost:11434`).

### 3.5. Telegram-бот (канал, Этап G) — опционально

1. В Telegram напишите **@BotFather** → `/newbot` → получите `TELEGRAM_BOT_TOKEN`.
2. Для white-label аватар/имя меняются **только через @BotFather** (`/setuserpic`, `/setname`).
3. Для webhook-режима подготовьте публичный HTTPS-URL; для long-polling — ничего не нужно.

### 3.6. Ключи подписи лицензий (Ed25519) — нужно для Этапов K/L

1. Сгенерируйте офлайн пару Ed25519 (на изолированной машине/HSM/KMS):
   `openssl genpkey -algorithm ed25519 -out license_signing_key.pem` и извлеките публичный ключ.
2. **Приватный ключ** (`LICENSE_SIGNING_KEY`) хранится **только** в signing-сервисе вендора /
   HSM / KMS — НИКОГДА не кладётся в образ коробки и не коммитится.
3. **Публичный ключ** вшивается в клиентский валидатор (для offline-проверки на коробке).
4. Заранее спланируйте **ротацию ключей** и публикацию **CRL** (см. `core/license_server.md`).
   Адрес CRL → `LICENSE_CRL_URL`, endpoint signing-сервиса → `LICENSE_SIGNING_SERVICE_URL`
   (оба — только на стороне вендора).
5. Сессионный ключ админки (`JWT_SECRET`, алгоритм `HS256`) — **отдельный** от лицензионного
   (`Ed25519`); не переиспользовать.

### 3.6a. SSO/OIDC конечного пользователя (authenticated-виджет) — опционально, гейт фичей `sso_oidc`

1. В своём IdP (Keycloak/Auth0/Azure AD) заведите OIDC-клиента для виджета.
2. Впишите: `OIDC_ENABLED=true`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
   `OIDC_JWKS_URL`, `OIDC_GROUPS_CLAIM` (имя claim со списком групп, по умолчанию `groups`).
3. Уровень доступа/группы берутся **только из проверенного id_token на бэкенде**, не из тела запроса.

### 3.7. GitHub / CI и приватный Registry — нужно для Этапов A и M

1. Создайте приватный репозиторий; в **Settings → Secrets and variables → Actions** добавьте
   все секреты из §4.
2. Для поставки коробок включите **GitHub Container Registry** (`ghcr.io/<org>/rag-core`),
   создайте токен с правами `write:packages`.
3. Клиентам коробок выдайте **read-доступ** к приватному образу (deploy token / PAT).

### 3.8. Для клиента коробки (`on_premise_box`) — инструкция «на той стороне»

1. Получить от вендора: `.key`-файл лицензии (или активационный код), доступ к приватному образу.
2. Заполнить `.env` коробки (БД, local storage, LLM/Ollama, при необходимости S3).
3. `docker compose up -d` → открыть экран активации админки → ввести лицензию
   (привязка к `hardware_id`).
4. Админка слушает `127.0.0.1` — доступ через **SSH-туннель**:
   `ssh -L 8001:127.0.0.1:8001 user@box-host`.
5. Для air-gapped-контуров (без браузерного доступа к SPA) все операции (активация лицензии,
   загрузка документов, управление доступом) выполняются через **CLI-дублёр `rag-admin`** (`typer`),
   обязательный для Box (см. матрицу §0).

---

## 4. 🔐 Реестр переменных окружения и секретов

> Полный `.env.example` создаётся на Этапе A. Секреты для CI — в GitHub Actions Secrets
> (`core/eval_and_ops.md` §4 п.5). Значения ниже заполняются по инструкциям §3.
>
> ⚠️ **Имена переменных = ВЕРХНИЙ регистр полей `Settings`** (`core/modules_spec.md`):
> pydantic-settings мапит env строго по имени поля. Любое расхождение = переменная не подхватится.

| Переменная (= поле `Settings`) | Назначение | Где взять (§) | Режим |
|---|---|---|---|
| `DEPLOYMENT_MODE` | `saas_cloud` / `on_premise_box` | задаётся вручную | оба |
| `POSTGRES_DSN` | админ-подключение Postgres (миграции/воркеры, обходит RLS) | §3.1 | оба |
| `POSTGRES_APP_DSN` | подключение user-retrieval (роль `app_user`, RLS применяется) | §3.1 | оба |
| `SUPABASE_URL` | URL проекта Supabase | §3.1 | cloud |
| `SUPABASE_SERVICE_ROLE_KEY` | сервисный ключ (миграции, обходит RLS) | §3.1 | cloud |
| `S3_ENDPOINT_URL` / `S3_REGION_NAME` / `S3_DEFAULT_BUCKET` | объектное хранилище | §3.2 | cloud |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | доступ к bucket | §3.2 | cloud |
| `LANGFUSE_HOST` | адрес Langfuse | §3.3 | оба |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` / `LANGFUSE_PROJECT_ID` | ключи/проект тенанта | §3.3 | оба |
| `GROQ_API_KEY` | основной LLM | §3.4 | оба |
| `GEMINI_API_KEY` | fallback LLM | §3.4 | оба |
| `OLLAMA_BASE_URL` | локальный LLM | §3.4 | box |
| `TELEGRAM_BOT_TOKEN` | Telegram-канал | §3.5 | опц. |
| `LICENSE_SIGNING_KEY` | приватный Ed25519 (только сервер/HSM) | §3.6 | вендор |
| `LICENSE_PUBLIC_KEY` | публичный Ed25519 (валидатор) | §3.6 | оба |
| `LICENSE_CRL_URL` | список отзыва (online-проверка) | §3.6 | вендор |
| `LICENSE_SIGNING_SERVICE_URL` | endpoint signing-сервиса | §3.6 | вендор |
| `OIDC_ENABLED` / `OIDC_ISSUER` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` / `OIDC_JWKS_URL` / `OIDC_GROUPS_CLAIM` | SSO конечного пользователя (гейт `sso_oidc`) | §3.6a | оба |
| `JWT_SECRET` | сессионный ключ админки (`HS256`) | сгенерировать | оба |
| `TELEMETRY_OPT_OUT` / `OBSERVABILITY_FORWARD_ENABLED` | телеметрия (см. §5) | по умолчанию off | оба |
| `GENERATION_TEMPERATURE` / `GENERATION_MAX_TOKENS` | параметры LLM | по умолчанию 0.1 / 1024 | оба |

---

## 5. 📦 Версионирование продукта (Коробка и Облако)

- **Единый код, единый SemVer** (`vMAJOR.MINOR.PATCH`) на оба варианта поставки; различие —
  в канале доставки и feature-флагах лицензии, не в форке кода.
- **Облако (`saas_cloud`)**: непрерывный деплой из `main` после зелёного CI; версия = тег
  последнего релиза; все тенанты на одной версии; миграции additive-only применяются
  автоматически (`alembic upgrade head` в entrypoint).
- **Коробка (`on_premise_box`)**: релиз по git-тегу `v*` → релизный CI (Этап M) собирает
  защищённый образ `rag-core:enterprise-vX.Y` в приватный GHCR; клиент обновляется волнами
  (приоритизация поставки — `vendor_backoffice.md`); перед миграцией — автоматический `pg_dump`.
- **Feature-флаги лицензии** включают enterprise-возможности без пересборки — один образ,
  разное поведение по лицензии. **Единый реестр = `enum LicenseFeature`** (`core/modules_spec.md`),
  он же — единственное допустимое множество для `licenses.features` (CHECK `<@` в
  `core/storage_schema.md`): `web_widget`, `telegram`, `rest_sse`, `cli`, `enterprise_acl`,
  `collections`, `sso_oidc`, `hybrid_search`, `s3_connector`, `flashrank_rerank`,
  `observability_forward`. Дефолт (`DEFAULT_LICENSE_FEATURES`) сведён между `LicensePayload`,
  `LicenseIssueRequest` и DDL: `["hybrid_search", "s3_connector", "flashrank_rerank"]`.
- **Телеметрия — два разных флага, не путать:** `telemetry_opt_out` (по умолчанию `true` —
  телеметрия выключена, 152-ФЗ/GDPR) и **канонический** `observability_forward_enabled`
  (по умолчанию `false` — opt-in форвард обезличенных метрик/трейсов из Box вендору).
  Устаревшее имя `telemetry_forward_enabled` использовать НЕЛЬЗЯ.
- **`observability_forward` (entitlement) ≠ `observability_forward_enabled` (runtime):** первый —
  право по лицензии (элемент `features`), второй — рантайм-переключатель клиента; форвард идёт
  только при одновременном выполнении обоих И `telemetry_opt_out = false`.
- **Совместимость**: только additive-миграции БД; удаление полей — через deprecation в
  следующем MAJOR. Клиент коробки может быть на версии ниже облака — контракты обратно совместимы.

---

## 6. Чек-лист готовности к продакшену (Definition of Done продукта)

- [ ] Все этапы A–O закрыты, CI (ruff check + `ruff format --check` + mypy + tests + Evaluation Gate) зелёный.
- [ ] Нагрузочные SLO (P95 retrieval < 400 мс, TTFT < 1.5 с) верифицированы (Этап O).
- [ ] Контракт-тесты безопасности (§1.3) проходят и блокируют merge при регрессе.
- [ ] Метрики RAGAS/IR не ниже SLO на золотом датасете.
- [ ] `.env.example` актуален; секреты только в хранилищах секретов.
- [ ] Облако: деплой, RLS-изоляция тенантов проверена; Langfuse-проекты заведены.
- [ ] Коробка: образ собран, лицензия активируется, `pg_dump` hook работает, CLI-дублёр доступен.
- [ ] Ручные инструкции (§3) выполнены и задокументированы для оператора/клиента.
