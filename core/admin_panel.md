# Admin Panel Specification (`core/admin_panel.md`)

## 1. Цель и границы подсистемы

Админ-панель — **приватный операторский контур** системы: через неё оператор/владелец тенанта управляет рабочим каталогом документов (загрузка, удаление, переиндексация), активирует лицензию, наблюдает за очередью инжеста, квотами и аудит-логом.

**Явное разграничение интерфейсов:**

| Контур | Аудитория | Доступ | Назначение |
|---|---|---|---|
| **Admin SPA** (`/api/v1/admin/*`) | Операторы, владельцы тенанта, вендор | Приватный, только после аутентификации | Управление документами, лицензиями, мониторинг |
| **Web Widget** (`/api/v1/widget/*`) | Конечные пользователи сайта клиента | Публичный, анонимная сессия | Чат-ответы по базе знаний |
| **Telegram Bot** (`BaseChannel`) | Конечные пользователи | Публичный канал | Один из каналов доставки ответов (не основной интерфейс) |

**Вне границ**: Telegram-бот **не является** административным интерфейсом. Авторизация по Telegram `user_id` / username не даёт ролевой модели, 2FA, SSO и полноценного аудита — использовать его для управления файлами и лицензиями запрещено.

---

## 2. Принципы (зафиксированные решения)

1. **Один код, два режима**: `deployment_mode: saas_cloud | on_premise_box`. Различия — только через runtime-конфиг `GET /api/v1/admin/config`, **никаких форков** фронтенда или бэкенда.
2. **Одна БД для всего**: админка — потребитель той же PostgreSQL через `service_role`. Второй «административной» бизнес-БД не существует.
3. **Три модульных порта**: `SQLAlchemy 2.0` (реляционка: Postgres default / SQLite dev), `BaseVectorStore` (pgvector default / Qdrant / Chroma), `BaseObjectStorage` (S3 / MinIO / R2 / local, per-tenant). **RRF — в application-layer (код), не контракт SQL.**
4. **Аутентификация**: `fastapi-users` (SQLAlchemy) — email/password + TOTP 2FA + OAuth2/SSO. Транспорт — `CookieTransport` (HTTP-only + Secure + SameSite=Lax) + CSRF-токен.
5. **Разделение ключевых контуров**: сессии — **HS256 с per-install секретом**; лицензии — **Ed25519 вендорским офлайн-ключом**. Ключи строго независимы.
6. **Фронтенд**: Vite + React + TypeScript + Tailwind v4 + shadcn/ui + Lucide + `recharts` (без Tremor). Вся лицензионная и квотная логика — **только на бэкенде**.
7. **Реалтайм**: Postgres `LISTEN/NOTIFY` (fallback — polling `documents.status`). **In-memory pub/sub запрещён.**
8. **Box-безопасность**: админка слушает `127.0.0.1`, доступ через SSH-туннель / внутренний reverse-proxy; CLI (`typer`) дублирует активацию лицензии и ingest для air-gapped контуров.
9. **Миграции**: `alembic upgrade head` на старте контейнера + pre-upgrade backup hook + **additive-only** (деструктивные миграции запрещены).
10. **RLS-контракт**: `event.listens_for(Session, "after_begin")` → `SET LOCAL app.current_tenant_id`. Без него RLS при пуле соединений не работает и «протекает» между запросами.

---

## 3. Архитектура подсистемы

```
┌─────────────────────────────────────────────────────────────────────────────┐
│          Admin SPA (Vite + React + TS + Tailwind + shadcn/ui + recharts)     │
│   - File Manager (Drag & Drop папок, клиентский sha256, дедупликация)        │
│   - Ingestion Pipeline Tracker (Live SSE: pending → processing → indexed)    │
│   - RAG Structure Viewer (parent/child chunks, метаданные, нормы)            │
│   - License Screen (код / .key / PASETO, срок, лимиты, hardware_id)          │
│   - Dashboard (recharts), Audit Log, Retrieval Playground                    │
│   * Лицензии и квоты только отображаются: бандл читаем, проверок на нём нет  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTPS, HTTP-only Secure Cookie + CSRF
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      FastAPI Admin Router (/api/v1/admin/*)                  │
│   - fastapi-users: email/password, TOTP 2FA, OAuth2/SSO, CookieTransport     │
│   - RBAC Dependency: admin | operator | viewer                              │
│   - Rate-limit policy: private (отдельно от публичного widget)              │
│   - Отдача SPA статикой (Box: один контейнер) / Nginx-CDN (SaaS)            │
└───────────┬───────────────────────────┬───────────────────────┬─────────────┘
            │                           │                       │
            ▼                           ▼                       ▼
┌───────────────────────┐   ┌───────────────────────┐  ┌────────────────────┐
│ PostgreSQL (service_  │   │ Taskiq + Postgres     │  │ BaseObjectStorage  │
│ role, RLS, pgvector)  │   │ process_document_task │  │ (S3/MinIO/R2/local)│
│ + LISTEN/NOTIFY       │   │                       │  │ per-tenant префикс │
└───────────────────────┘   └───────────────────────┘  └────────────────────┘
                                        │
                                        ▼
                             ┌────────────────────┐
                             │ BaseVectorStore    │
                             │ (pgvector default) │
                             └────────────────────┘
```

---

## 4. Аутентификация и авторизация

### 4.1. Механизм
- **Библиотека**: `fastapi-users` поверх `SQLAlchemy 2.0` (та же БД, та же схема, тот же код-путь для SaaS и Box — без внешнего GoTrue/Supabase Auth, который невозможен в air-gapped).
- **Методы входа**: email + password (Argon2id), **TOTP 2FA** (Google Authenticator / 1Password), OAuth2 / OIDC SSO (корпоративный провайдер) — опционально по `features`.
- **Транспорт сессии**: `CookieTransport` — cookie с флагами `HttpOnly`, `Secure`, `SameSite=Lax`, ограниченным `Path=/api/v1` и TTL 60 минут (refresh — по sliding-обновлению).
- **CSRF**: double-submit cookie / `X-CSRF-Token` заголовок на всех небезопасных методах (`POST`, `PUT`, `PATCH`, `DELETE`).
- **Запрет**: ⛔ **JWT не хранить в `localStorage` / `sessionStorage`** и не передавать в `Authorization: Bearer` из SPA. Один XSS в админке = полный захват контура с правом удаления данных и подмены лицензии.

### 4.2. Роли (RBAC)

| Роль | Документы | Лицензия | Дашборд / Аудит | Пользователи |
|---|---|---|---|---|
| `admin` | CRUD + reindex | Активация / просмотр | Полный доступ | Управление |
| `operator` | Upload / reindex (без delete) | Только просмотр статуса | Просмотр | — |
| `viewer` | Только список и чанки | Только просмотр статуса | Просмотр | — |

- Доступ к БД: процесс админки ходит под `service_role` (BYPASSRLS), ключ существует **только на сервере**. Scoped-доступ по `tenant_id` обеспечивается `SET LOCAL app.current_tenant_id` в `after_begin` (см. `storage_schema.md`).
- В режиме `saas_cloud` дополнительно существует роль вендора (кросс-тенантный оператор) с явным аудитом каждого входа в чужой тенант.

### 4.3. Ключи (критично)

| Ключ | Алгоритм | Где живёт | Назначение |
|---|---|---|---|
| `JWT_SECRET` | **HS256**, ≥ 32 байт | Per-install: генерируется при первом старте, хранится в секрет-сторе инсталляции, ротируется при деплое | Подпись сессионных cookie-токенов |
| `LICENSE_SIGNING_KEY` | **Ed25519** | Только у вендора, offline; приватная часть **никогда** не попадает в артефакт Box | Подпись лицензионных токенов |

> ⚠️ **Запрещено переиспользовать `LICENSE_SIGNING_KEY` для подписи сессий.** Компрометация любого клиентского инстанса в этом случае даёт злоумышленнику возможность **выписывать лицензии от имени вендора**. Переиспользуется только *библиотека* (`pynacl` / `cryptography`) и код-паттерн, но не ключ.
>
> Асимметричная подпись сессий (EdDSA) и кастомная `JWTStrategy` **не вводятся** до появления отдельного auth-сервиса (sign/verify в разных процессах). Нативный HS256 из `fastapi-users` достаточен.

---

## 5. Контракты эндпоинтов (`/api/v1/admin/*`)

| Метод | Путь | Запрос | Ответ | Назначение |
|---|---|---|---|---|
| `GET` | `/config` | — | `AdminConfig` | Runtime-конфиг: `deployment_mode`, `features`, `branding` |
| `GET` | `/documents` | `?status=&q=&page=&per_page=` | `Page[DocumentListItem]` | Список документов с пагинацией и фильтром по статусу |
| `POST` | `/documents/upload` | `multipart/form-data` (файлы/папки, `access_level`, `content_hash`) | `List[DocumentListItem]` | Загрузка в `BaseObjectStorage` + постановка `process_document_task` |
| `DELETE` | `/documents/{id}` | — | `204` | Каскадное удаление: объект в сторадже + `documents` + parent/child чанки + векторы |
| `POST` | `/documents/{id}/reindex` | — | `DocumentListItem` | Принудительная переиндексация (сброс статуса в `pending`) |
| `GET` | `/documents/{id}/chunks` | `?include_vectors=false&chunk_id=` | `List[ChunkView]` | Просмотр parent/child структуры; векторы — только явным флагом |
| `GET` | `/license/status` | — | `LicenseStatus` | Срок, план, лимиты, статус онлайн/офлайн-валидации, Restricted Mode |
| `POST` | `/license/activate` | `LicenseActivationRequest` | `LicenseStatus` | Активация по коду, `.key`-файлу или PASETO-токену |
| `GET` | `/license/machine-fingerprint` | — | `MachineFingerprint` | `hardware_id` инсталляции для выпуска привязанной лицензии |
| `GET` | `/stats` | `?period=` | `DashboardStats` | Документы, чанки, запросы, латентность, объём хранилища, квоты |
| `GET` | `/ingest/queue` | `?state=` | `List[IngestQueueItem]` | Текущее состояние очереди Taskiq (pending/processing/failed, retry) |
| `GET` | `/ingest/stream` | — (SSE) | `text/event-stream` | Live-статусы инжеста через Postgres `LISTEN/NOTIFY` |
| `GET` | `/audit-log` | `?actor=&action=&page=` | `Page[AuditLogEntry]` | Журнал действий: кто, когда, что, IP, User-Agent |
| `POST` | `/playground/retrieve` | `{query, access_level}` | `List[RetrievalCandidate]` | Диагностика retrieval: dense/sparse ранги, RRF, rerank |

**Общие правила:**
- Все эндпоинты требуют валидную сессионную cookie + роль; небезопасные методы — ещё и CSRF-токен.
- Все мутации пишут запись в `audit_log` в той же транзакции.
- Ошибки — в едином формате `{ "detail": {...}, "code": "..." }`; внутренние трейсы наружу не отдаются.

---

## 6. Файловый менеджер и конвейер инжеста

1. Drag & Drop файлов **и папок** (`webkitdirectory`) в SPA; для каждого файла клиент считает `sha256` и предварительно спрашивает бэкенд о дубликате — повторная заливка не выполняется.
2. Файл сохраняется через `BaseObjectStorage.put(tenant_id, ...)` с per-tenant префиксом (`{tenant_id}/...`), после чего создаётся запись в `documents` со статусом `pending`.
3. Ставится задача `process_document_task(tenant_id, storage_ref, content_hash, access_level)`.
4. Статусы документа: `pending → processing → indexed | failed` (на UI дополнительно детализируются подэтапы воркера: парсинг Docling → Parent/Child сплиттер → BGE-M3 эмбеддинги → запись в `BaseVectorStore`).
5. Удаление — строго каскадное и транзакционное: объект в сторадже → `child_chunks` → `parent_chunks` → `documents` (БД-каскады `ON DELETE CASCADE` + явное удаление векторов, если vector store внешний).
6. Квоты (`max_documents`, объём хранилища) проверяются **до** записи, на бэкенде, через `enforce_quota`.

---

## 7. Визуализация RAG-структуры

- `GET /documents/{id}/chunks` по умолчанию возвращает только `content`, метаданные, `token_count`, `chunk_index` и **нормы** векторов (`dense_norm`), а также признак наличия sparse-представления.
- Эмбеддинги отдаются **только** при `?include_vectors=true` **и только для одного чанка** (`?chunk_id=...`). Причина: 1024 float × N чанков = мегабайты JSON на каждый запрос.
- UI показывает дерево `document → parent_chunks → child_chunks` с подсветкой того, какие чанки попали в последнюю выдачу Playground.

---

## 8. Лицензионный экран

- Поля ввода: код активации (`RAG-ENT-XXXX-YYYY-ZZZZ`), загрузка `.key` файла, вставка PASETO/JWT-токена.
- Отображается: план, `issued_at` / `expires_at`, лимиты (`max_documents`, `max_users`), текущее потребление, `hardware_id`, режим (`airgap_enterprise` / `online_subscription`), время последнего heartbeat и остаток Grace Period.
- Баннер **Restricted Mode** при истечении лицензии или исчерпании Grace Period — с текстом, пришедшим от бэкенда.
- ⛔ Фронтенд **не хранит и не валидирует** лицензии: React-бандл всегда читаем, любая проверка `if (license.valid)` на клиенте декоративна. Источник истины — ответ бэкенда.

---

## 9. Реалтайм-статусы

- **Основной механизм**: Postgres `LISTEN/NOTIFY` — воркер Taskiq при смене статуса документа шлёт `NOTIFY ingest_events, '{"document_id": ..., "status": ...}'`, SSE-эндпоинт админки слушает канал и ретранслирует события.
- **Fallback**: polling `documents.status` (интервал 2–3 с) при старте соединения и в окружениях без доступа к `LISTEN`.
- ⛔ **In-memory pub/sub запрещён**: при `uvicorn --workers N` события из воркера Taskiq не видны процессу, обслуживающему SSE-соединение.
- На старте SSE-потока клиенту отдаётся снапшот текущих статусов, далее — только дельты (`event: document_status`).

---

## 10. Развёртывание и изоляция

- **Box**: сервис админки биндится на `127.0.0.1` (публикация порта наружу по умолчанию отключена). Доступ — SSH-туннель или внутренний reverse-proxy клиента с его же аутентификацией.
- **Air-gapped CLI-дублёр** (`typer`) — чтобы в закрытом контуре вообще не требовался браузер:
  - `rag-admin license activate --file /etc/rag/license.key`
  - `rag-admin license status`
  - `rag-admin ingest --path ./data --access-level 0`
  - `rag-admin documents list|delete|reindex`
- **Widget vs Admin**: один код, но **разные `--app` / воркер-пулы и разные rate-limit политики**. Публичный высоконагруженный виджет не должен ронять приватную админку; структура готова к `docker compose scale widget=N` без рефакторинга.
- **SaaS**: SPA отдаётся Nginx/CDN, API — отдельным сервисом; **Box**: SPA отдаётся FastAPI как `StaticFiles` из одного контейнера.

---

## 11. Структура монорепозитория

```
rag-enterprise/
  apps/
    admin/      # Vite + React + TS + Tailwind v4 + shadcn/ui + Lucide + recharts
    widget/     # встраиваемый web-виджет (Shadow DOM)
  services/
    api/        # FastAPI: admin / chat / widget роутеры, --app на каждый контур
  packages/
    core/       # Pydantic-модели + интерфейсы (BaseVectorStore, BaseObjectStorage, ...)
  core/         # Markdown-ядро спецификаций (источник истины)
```

- **Тулинг**: `pnpm workspaces` (frontend) + `uv` / `poetry` workspace (backend).
- **CI**: два независимых пайплайна (frontend: typecheck + build; backend: ruff + mypy + pytest + eval gate). Трудозатраты на настройку монорепо-тулинга закладываются отдельной задачей — в рамки «Фазы 1» они не помещаются.

---

## 12. Чек-лист безопасности админки

- [x] HTTP-only + Secure + SameSite=Lax cookie, без `localStorage`.
- [x] CSRF-токен на всех мутациях.
- [x] TOTP 2FA обязателен для роли `admin`.
- [x] Аудит каждой мутации с IP и User-Agent.
- [x] Разделённые ключевые контуры (session HS256 per-install vs license Ed25519 vendor).
- [x] Никакой лицензионной/квотной логики на клиенте.
- [x] Box: bind `127.0.0.1` + CLI-дублёр.
- [x] Раздельные rate-limit политики `admin` (private) и `widget` (public).
