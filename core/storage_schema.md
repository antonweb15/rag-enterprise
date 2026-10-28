# Storage Schema & Database DDL Specification (`core/storage_schema.md`)

## 1. Обзор схемы базы данных

Хранилище базируется на **PostgreSQL 15+ / `pgvector` (0.7+)**. **Supabase — лишь один из вариантов хостинга** этой же Postgres (наравне с self-hosted Docker, managed-облаком или локальной СУБД заказчика в режиме `on_premise_box`); обязательной зависимостью Supabase **не является**. Для dev-окружения допустим SQLite через SQLAlchemy (без векторного плеча).

Схема поддерживает:
- Разделение на **Parent** (для формирования контекста LLM) и **Child** чанки (для точного векторного и sparse поиска).
- Поддержку **Dense (1024-dim)** и **Sparse (250002-dim)** векторов модели `BAAI/bge-m3`.
- **HNSW** индексы для высокоскоростного векторного и sparse поиска.
- **Двухконтурную модель RBAC**:
  - **Первичный контур (Primary Filter)**: фильтр `access_level` применяется в query-пути — контрактно на уровне приложения (`BaseVectorStore` / `Retriever`), а в pgvector-пути дополнительно внутри функции `hybrid_search_rrf` (её внутренняя деталь, см. §3) через предикат `WHERE d.access_level <= user_access_level`. Значение `user_access_level` определяется бэкендом на основе аутентифицированного пользователя (таблица `users`) и не может быть подделано клиентом.
  - **Вторичный контур (Defense-in-depth)**: PostgreSQL **Row-Level Security (RLS)** для изоляции данных при прямых табличных запросах.
- Хранение пользователей (`users`), сессий (`conversations`), сообщений (`messages`) и меток обратной связи (thumbs up/down).

---

## 2. PostgreSQL DDL Миграция

```sql
-- 0. Обеспечение совместимости со средой выполнения (создание роли service_role для локального Docker/Postgres)
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role BYPASSRLS;
    END IF;
END
$$;

-- 1. Активация расширений
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Таблица клиентов/тенантов (Мультитенантность для SaaS и Box)
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug TEXT NOT NULL UNIQUE,                        -- уникальный идентификатор тенанта (company-slug)
    name TEXT NOT NULL,                               -- наименование организации/клиента
    public_key_hash TEXT,                             -- хэш привязанного публичного ключа
    is_active BOOLEAN NOT NULL DEFAULT true,
    storage_type TEXT NOT NULL DEFAULT 's3',          -- 's3', 'minio', 'local'
    s3_bucket TEXT,
    s3_prefix TEXT,                                   -- базовый путь префикса в бакете (например: tenants/uuid/)
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb, -- строгий allowlist доменов для CORS Web-виджета (БЕЗ wildcard '*'; см. channels.md)
    config JSONB NOT NULL DEFAULT '{}'::jsonb,        -- напр. observability: {url, username, provisioned} (БЕЗ пароля); branding: {...}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);

-- 3. Таблица лицензий и подписок
CREATE TABLE IF NOT EXISTS licenses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    license_key TEXT NOT NULL UNIQUE,                 -- подписанный Ed25519 токен лицензии
    plan TEXT NOT NULL DEFAULT 'standard',            -- 'standard', 'pro', 'enterprise_box'
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                           -- срок окончания действия лицензии (NULL для бессрочных On-Premise)
    max_documents INTEGER NOT NULL DEFAULT 1000,
    max_users INTEGER NOT NULL DEFAULT 10,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    hardware_id TEXT,                                 -- привязка к оборудованию для Box версии
    features JSONB NOT NULL DEFAULT '["hybrid_search"]'::jsonb,
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_licenses_tenant_id ON licenses(tenant_id);
CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(license_key);

-- 4. Таблица пользователей и прав доступа
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    channel_user_id TEXT NOT NULL,                    -- Telegram user ID или внешний Auth ID
    channel TEXT NOT NULL DEFAULT 'telegram',         -- ChannelType: 'web', 'telegram', 'rest_sse', 'cli' (legacy 'fastapi' -> 'rest_sse')
    access_level INTEGER NOT NULL DEFAULT 0,          -- 0: Public, 1: Internal, 2: Confidential, 3: Strict
    is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_channel_user UNIQUE (tenant_id, channel_user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_channel ON users(tenant_id, channel_user_id);

-- 5. Таблица документов (метаданные исходных файлов)
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 's3',           -- 's3', 'local_folder', 'confluence'
    source_uri TEXT NOT NULL,                         -- путь к файлу или S3 URI
    content_hash TEXT NOT NULL,                       -- SHA-256 хеш содержимого
    status TEXT NOT NULL DEFAULT 'pending',           -- 'pending', 'processing', 'indexed', 'failed'
    error_message TEXT,
    language TEXT NOT NULL DEFAULT 'ru',
    ingestion_job_id UUID,                            -- ID задачи инжеста в очереди Taskiq
    access_level INTEGER NOT NULL DEFAULT 0,          -- 0: Public, 1: Internal, 2: Confidential, 3: Strict
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,      -- заголовки, автор, страницы, теги
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_content_hash UNIQUE (tenant_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_status ON documents(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_access_level ON documents(tenant_id, access_level);

-- 6. Таблица родительских чанков (Parent Chunks: 800 - 1200 токенов)
CREATE TABLE IF NOT EXISTS parent_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- Денормализованный уровень доступа (каскад из documents); defense-in-depth для прямых запросов к parent
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_id ON parent_chunks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_document_id ON parent_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_access ON parent_chunks(tenant_id, access_level);

-- 7. Таблица дочерних чанков (Child Chunks: 150 - 250 токенов)
CREATE TABLE IF NOT EXISTS child_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    parent_id UUID NOT NULL REFERENCES parent_chunks(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- Денормализованный уровень доступа для быстрого фильтра
    embedding vector(1024),                           -- Dense BGE-M3 (1024 dim)
    sparse_vector sparsevec(250002),                  -- Sparse BGE-M3 (словарь 250002 токена)
    embedding_model_id TEXT NOT NULL DEFAULT 'BAAI/bge-m3',
    embedding_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_child_chunks_tenant_access ON child_chunks(tenant_id, access_level);
CREATE INDEX IF NOT EXISTS idx_child_chunks_parent_id ON child_chunks(parent_id);

-- HNSW индекс для плотного векторного поиска (Cosine distance)
CREATE INDEX IF NOT EXISTS idx_child_chunks_embedding_hnsw 
ON child_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- HNSW индекс для sparse-векторов (Sparse Cosine / IP)
CREATE INDEX IF NOT EXISTS idx_child_chunks_sparse_hnsw 
ON child_chunks USING hnsw (sparse_vector sparsevec_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 8. Таблицы сессий и сообщений диалога
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                            -- ID пользователя в Telegram / Channel
    channel TEXT NOT NULL DEFAULT 'telegram',         -- ChannelType: 'web', 'telegram', 'rest_sse', 'cli' (legacy 'fastapi' -> 'rest_sse')
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant_user ON conversations(tenant_id, user_id);

-- 9. Таблица сообщений диалога
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                               -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    feedback INTEGER DEFAULT NULL,                    -- 1 (👍), -1 (👎), NULL
    feedback_comment TEXT,
    langfuse_trace_id TEXT,                           -- Связь с трассировкой в Langfuse
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);

-- 10. Учетные данные хранилищ тенанта (BaseObjectStorage: S3 / MinIO / R2 / local)
-- ВАЖНО: секреты хранятся ТОЛЬКО в зашифрованном виде (AES-GCM с ключом из KMS / env master key).
-- Плейнтекст ключей в БД запрещен; расшифровка выполняется исключительно на бэкенде.
CREATE TABLE IF NOT EXISTS storage_credentials (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 's3',              -- 's3' | 'minio' | 'r2' | 'local'
    endpoint_url TEXT,
    region TEXT DEFAULT 'auto',
    bucket TEXT,
    prefix TEXT,                                      -- базовый per-tenant префикс ({tenant_id}/)
    access_key_id_enc BYTEA,                          -- AES-GCM ciphertext
    secret_access_key_enc BYTEA,                      -- AES-GCM ciphertext
    encryption_key_id TEXT,                           -- идентификатор ключа в KMS / ротации
    nonce BYTEA,                                      -- AES-GCM nonce
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storage_credentials_tenant_provider UNIQUE (tenant_id, provider, bucket)
);

CREATE INDEX IF NOT EXISTS idx_storage_credentials_tenant_id ON storage_credentials(tenant_id);

-- 11. Журнал действий администраторов (Audit Log приватного контура админки)
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id UUID,                               -- пользователь админки (fastapi-users)
    actor_email TEXT,
    action TEXT NOT NULL,                             -- 'document.upload', 'document.delete', 'license.activate', 'auth.login'
    object_type TEXT,
    object_id TEXT,
    ip_address INET,
    user_agent TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created ON audit_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_user_id);

-- 12. Канал уведомлений для SSE админки (реалтайм-статусы инжеста)
-- Смена documents.status публикуется через NOTIFY; in-memory pub/sub запрещен.
CREATE OR REPLACE FUNCTION notify_document_status() RETURNS TRIGGER AS $$
BEGIN
    PERFORM pg_notify(
        'ingest_events',
        json_build_object(
            'document_id', NEW.id,
            'tenant_id', NEW.tenant_id,
            'status', NEW.status,
            'error_message', NEW.error_message
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_status_notify ON documents;
CREATE TRIGGER trg_documents_status_notify
    AFTER INSERT OR UPDATE OF status ON documents
    FOR EACH ROW EXECUTE FUNCTION notify_document_status();
```

### 2.1. Миграция каналов (`channel`) и allowlist доменов

> Контекст: `UserContext.channel` переведён на `ChannelType` (`web/telegram/rest_sse/cli`, см. [`modules_spec.md`](./modules_spec.md)).
> Legacy-значение `'fastapi'` рассинхронизировано и не парсится str-enum — требуется backfill.
> Дефолт `allowed_origins` исправлен с `'["*"]'` на `'[]'` (wildcard по умолчанию — security-баг).

```sql
-- Backfill каналов: legacy 'fastapi' -> 'rest_sse'
UPDATE users          SET channel = 'rest_sse' WHERE channel = 'fastapi';
UPDATE conversations  SET channel = 'rest_sse' WHERE channel = 'fastapi';

-- Снятие небезопасного wildcard-дефолта у существующих тенантов, не настроивших allowlist вручную.
-- ВНИМАНИЕ: выполнять осознанно — виджет перестанет обслуживаться, пока origin не добавлен явно.
UPDATE tenants SET allowed_origins = '[]'::jsonb WHERE allowed_origins = '["*"]'::jsonb;
```

### 2.2. Таблицы вендорского контура (Backoffice) — additive-only

> Контекст: это **кросс-тенантные** данные вендора (см. [`vendor_backoffice.md`](./vendor_backoffice.md), [`license_server.md`](./license_server.md)).
> Все таблицы backoffice **вне tenant-RLS** (нет колонки/политики `app.current_tenant_id`) и доступны **только** `service_role` backoffice-контура.
> В артефакте `on_premise_box` этот контур **не разворачивается**; таблицы существуют на инфраструктуре вендора (отдельная backoffice-БД/схема рекомендуется).
> Приватный Ed25519-ключ подписи лицензий здесь **не хранится** — только в signing-сервисе / HSM / KMS.

```sql
-- 13. Покупатели (юр. лица). ОТДЕЛЬНО от tenants: 1 customer : N tenants.
CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    legal_name TEXT NOT NULL,
    display_name TEXT,
    inn TEXT,                                          -- ИНН / tax id
    contact_email TEXT NOT NULL,
    contact_phone TEXT,
    contract_number TEXT,
    status TEXT NOT NULL DEFAULT 'trial',             -- 'trial', 'active', 'suspended', 'churned'
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 14. Пользователи backoffice (сотрудники вендора). НЕ путать с users (тенантные).
CREATE TABLE IF NOT EXISTS backoffice_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,                     -- Argon2id
    role TEXT NOT NULL DEFAULT 'support',             -- BackofficeRole: 'superadmin', 'support', 'finance', 'sales'
    is_active BOOLEAN NOT NULL DEFAULT true,
    totp_secret_enc BYTEA,                             -- 2FA обязателен для всех backoffice-ролей
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 15. Заказы (минимальный каркас биллинга; полноценный биллинг — внешний провайдер).
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    amount_minor BIGINT,                              -- сумма в минорных единицах (копейки/центы)
    currency TEXT NOT NULL DEFAULT 'RUB',
    status TEXT NOT NULL DEFAULT 'pending',          -- 'pending', 'paid', 'cancelled', 'refunded'
    external_provider TEXT,                           -- 'stripe' | 'paddle' | 'manual' | ...
    external_ref TEXT,                               -- id платежа во внешнем провайдере
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);

-- 16. Активационные коды 'RAG-ENT-XXXX-YYYY-ZZZZ' (выдаются вендором под заказ).
CREATE TABLE IF NOT EXISTS activation_codes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code TEXT NOT NULL UNIQUE,                        -- 'RAG-ENT-XXXX-YYYY-ZZZZ'
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued',           -- 'issued', 'redeemed', 'revoked', 'expired'
    redeemed_by_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activation_codes_customer ON activation_codes(customer_id);
CREATE INDEX IF NOT EXISTS idx_activation_codes_status ON activation_codes(status);

-- 17. Реестр проданных/развёрнутых коробок (on-premise инсталляции).
CREATE TABLE IF NOT EXISTS box_deployments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    image_version TEXT,                              -- версия поставленного закрытого образа
    delivered_at TIMESTAMPTZ,
    hardware_id TEXT,                               -- fingerprint инсталляции
    last_heartbeat_at TIMESTAMPTZ,
    grace_period_expires_at TIMESTAMPTZ,
    restricted_mode BOOLEAN NOT NULL DEFAULT false,
    telemetry_opt_out BOOLEAN NOT NULL DEFAULT true,          -- уважается вендором; форвард трейсов/метрик только при false
    observability_forward_enabled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_box_deployments_customer ON box_deployments(customer_id);
CREATE INDEX IF NOT EXISTS idx_box_deployments_hardware ON box_deployments(hardware_id);

-- 18. Глобальный аудит вендора (кросс-тенантный; отдельно от tenant-scoped audit_log).
--     Сюда же пишутся break-glass входы (impersonation) сотрудников вендора в чужой тенант.
CREATE TABLE IF NOT EXISTS backoffice_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor_backoffice_user_id UUID REFERENCES backoffice_users(id) ON DELETE SET NULL,
    actor_email TEXT,
    action TEXT NOT NULL,                            -- 'license.issue', 'license.revoke', 'tenant.impersonate', ...
    impersonated_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    object_type TEXT,
    object_id TEXT,
    ip_address INET,
    user_agent TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_backoffice_audit_created ON backoffice_audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_backoffice_audit_impersonated ON backoffice_audit_log(impersonated_tenant_id);

-- 19. Расширение licenses вендорскими полями выдачи/отзыва (additive, nullable).
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS issued_by UUID REFERENCES backoffice_users(id);
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id);
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revocation_reason TEXT;
```

> RLS: таблицы backoffice (`customers`, `backoffice_users`, `orders`, `activation_codes`, `box_deployments`, `backoffice_audit_log`) **не включаются** в tenant-isolation политики §4. Доступ к ним — исключительно под ролью backoffice `service_role`; клиентских политик (`authenticated`/`anon`) для них нет (fail-closed).

### 2.3. Контракт ре-классификации `access_level` (Этап A, P0)

> Контекст: `access_level` **денормализован** в `child_chunks` и `parent_chunks` для дешёвого primary-фильтра. Источник истины — `documents.access_level`.
> Любая смена уровня документа обязана **каскадно и транзакционно** перезаписать уровень во всех его чанках, иначе возникает окно рассогласования (Confidential-контент утечёт как Public или наоборот).

```sql
-- (1) Одноразовый backfill существующих данных при внедрении колонки parent_chunks.access_level.
--     Без него старые parent-строки останутся Public (DEFAULT 0) и ранее Confidential-контент «раскроется».
UPDATE parent_chunks p
   SET access_level = d.access_level
  FROM documents d
 WHERE p.document_id = d.id
   AND p.access_level IS DISTINCT FROM d.access_level;

-- Подстраховка для child_chunks (на случай ранее рассогласованных строк):
UPDATE child_chunks c
   SET access_level = d.access_level
  FROM parent_chunks p
  JOIN documents d ON p.document_id = d.id
 WHERE c.parent_id = p.id
   AND c.access_level IS DISTINCT FROM d.access_level;

-- (2) Триггер каскадной ре-классификации: смена documents.access_level в ОДНОЙ транзакции
--     перезаписывает parent_chunks.access_level и child_chunks.access_level.
CREATE OR REPLACE FUNCTION cascade_access_level() RETURNS TRIGGER AS $$
BEGIN
    UPDATE parent_chunks SET access_level = NEW.access_level WHERE document_id = NEW.id;
    UPDATE child_chunks  SET access_level = NEW.access_level
     WHERE parent_id IN (SELECT id FROM parent_chunks WHERE document_id = NEW.id);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_documents_access_cascade ON documents;
CREATE TRIGGER trg_documents_access_cascade
    AFTER UPDATE OF access_level ON documents
    FOR EACH ROW WHEN (OLD.access_level IS DISTINCT FROM NEW.access_level)
    EXECUTE FUNCTION cascade_access_level();
```

- **Транзакционность — обязательна.** Допустима Taskiq-задача каскада только при явной пометке «eventually consistent» и временном **fail-closed** (до завершения синхронизации чанки документа считаются недоступными), иначе появится окно рассогласования.
- При инжесте новые `parent_chunks`/`child_chunks` наследуют `access_level` от своего `documents` (проставляется воркером при записи).

### 2.4. Группы, коллекции и per-document ACL (Этап B, P1 — за feature-флагом `enterprise_acl`)

> Контекст: плоская шкала `access_level` (0–3) покрывает ~80% кейсов и остаётся **primary-фильтром по умолчанию**.
> Для enterprise-гибкости («отдел A видит только свои документы, даже если все Internal») добавляется опциональный слой: группы субъектов + коллекции (базы знаний) + точечный ACL «субъект → документ».
> Все таблицы — **additive-only**, под tenant-RLS (§4). Фичи включаются флагом лицензии `enterprise_acl`/`collections` (см. [`licensing_and_distribution.md`](./licensing_and_distribution.md)); при выключенном флаге работает только `access_level`.

```sql
-- 20. Группы субъектов (маппятся на оргструктуру / группы из IdP).
CREATE TABLE IF NOT EXISTS groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- базовый уровень допуска группы
    external_ref TEXT,                                -- id группы в IdP (OIDC/SAML/SCIM), NULL для локальных
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_groups_tenant_name UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_groups_tenant_id ON groups(tenant_id);

-- 21. Членство пользователей в группах (m2m).
CREATE TABLE IF NOT EXISTS user_groups (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_user_groups_tenant ON user_groups(tenant_id);

-- 22. Коллекции (базы знаний / папки, с иерархией).
CREATE TABLE IF NOT EXISTS collections (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,
    parent_id UUID REFERENCES collections(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_collections_tenant_name UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_collections_tenant_id ON collections(tenant_id);

-- 23. Привязка документов к коллекциям (m2m).
CREATE TABLE IF NOT EXISTS document_collections (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, collection_id)
);

CREATE INDEX IF NOT EXISTS idx_document_collections_collection ON document_collections(collection_id);

-- 24. Точечный ACL: «субъект (user|group) -> документ». permission — enum на расширение.
CREATE TABLE IF NOT EXISTS document_acl (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL,                     -- 'user' | 'group'
    principal_id UUID NOT NULL,                       -- users.id | groups.id (по principal_type)
    permission TEXT NOT NULL DEFAULT 'read',          -- 'read' (enum заложен на расширение: 'write'/'manage'/...)
    granted_by UUID,                                  -- users.id администратора, выдавшего грант (аудит)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_acl UNIQUE (tenant_id, document_id, principal_type, principal_id, permission)
);

CREATE INDEX IF NOT EXISTS idx_document_acl_document ON document_acl(document_id);
CREATE INDEX IF NOT EXISTS idx_document_acl_principal ON document_acl(tenant_id, principal_type, principal_id);
```

- **Семантика эффективного доступа** (вычисляется `PolicyEvaluator`, см. [`modules_spec.md`](./modules_spec.md)): документ доступен субъекту, если
  `access_level(doc) <= max(user.access_level, max(group.access_level))` **ИЛИ** есть явная строка `document_acl` для `user` / одной из его `group`.
  При выключенном `enterprise_acl` второе слагаемое (ACL/группы) игнорируется — остаётся чистый `access_level`.
- Контракт возврата из `PolicyEvaluator` — **множество/предикат разрешённых ресурсов** (`allowed_document_ids` / SQL-предикат), применяемый **до** top-k/RRF (не пост-фильтр), иначе ломаются ранжирование и RRF.

---

## 3. SQL Хранимая функция гибридного поиска (внутренняя деталь `PgVectorStore`)

> ⚠️ **Статус контракта**: функция `hybrid_search_rrf` — **внутренняя оптимизационная деталь реализации `PgVectorStore`**, а не контракт системы.
> Контрактом являются `BaseVectorStore.search_dense()` / `BaseVectorStore.search_sparse()` + слияние RRF в **application-layer** (`Retriever.fuse_rrf`, см. [`modules_spec.md`](./modules_spec.md) §2.3).
> Функция вызывается только при `capabilities.supports_hybrid_rrf = True` и обязана давать результат, идентичный коду-фьюжену. Это сохраняет оптимизированный путь для pgvector, не превращая «сменный вектор-стор» в фикцию.

```sql
CREATE OR REPLACE FUNCTION hybrid_search_rrf(
    query_dense vector(1024),
    query_sparse sparsevec(250002),
    target_tenant_id UUID,
    user_access_level INTEGER DEFAULT 0,
    top_candidates INTEGER DEFAULT 30,
    rrf_k INTEGER DEFAULT 60,
    alpha FLOAT DEFAULT 0.6,
    beta FLOAT DEFAULT 0.4
)
RETURNS TABLE (
    child_id UUID,
    parent_id UUID,
    document_id UUID,
    content TEXT,
    dense_rank BIGINT,
    sparse_rank BIGINT,
    rrf_score FLOAT
)
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    -- Настройка точности HNSW для запроса (P95 latency < 400ms при сохранении высокого Recall)
    SET LOCAL hnsw.ef_search = 40;

    RETURN QUERY
    WITH dense_search AS (
        SELECT 
            c.id AS child_id,
            ROW_NUMBER() OVER (ORDER BY c.embedding <=> query_dense) AS rank
        FROM child_chunks c
        WHERE c.tenant_id = target_tenant_id
          AND c.access_level <= user_access_level
        ORDER BY c.embedding <=> query_dense
        LIMIT top_candidates
    ),
    sparse_search AS (
        SELECT 
            c.id AS child_id,
            ROW_NUMBER() OVER (ORDER BY c.sparse_vector <=> query_sparse) AS rank
        FROM child_chunks c
        WHERE c.tenant_id = target_tenant_id
          AND c.access_level <= user_access_level
          AND query_sparse IS NOT NULL
        ORDER BY c.sparse_vector <=> query_sparse
        LIMIT top_candidates
    ),
    combined_scores AS (
        SELECT 
            COALESCE(d.child_id, s.child_id) AS child_id,
            d.rank AS dense_rank,
            s.rank AS sparse_rank,
            (COALESCE(alpha / (rrf_k + d.rank), 0.0) + 
             COALESCE(beta / (rrf_k + s.rank), 0.0))::FLOAT AS rrf_score
        FROM dense_search d
        FULL OUTER JOIN sparse_search s ON d.child_id = s.child_id
    )
    SELECT 
        c.id AS child_id,
        p.id AS parent_id,
        p.document_id AS document_id,
        c.content AS content,
        cs.dense_rank,
        cs.sparse_rank,
        cs.rrf_score
    FROM combined_scores cs
    JOIN child_chunks c ON cs.child_id = c.id
    JOIN parent_chunks p ON c.parent_id = p.id
    ORDER BY cs.rrf_score DESC
    LIMIT top_candidates;
END;
$$;
```

---

## 4. Настройка Row-Level Security (RLS)

### 4.0. Контракт «RLS + SQLAlchemy» (обязательный)

SQLAlchemy использует **пул соединений** под одной ролью БД. Без явной установки GUC в начале **каждой** транзакции RLS не сработает, а при переиспользовании соединения значение «протечёт» в соседний запрос. Поэтому установка `app.current_tenant_id` — обязательный системный контракт:

```python
# packages/core/db/session.py
from sqlalchemy import event, text
from sqlalchemy.orm import Session

@event.listens_for(Session, "after_begin")
def set_tenant_guc(session: Session, transaction, connection) -> None:
    """SET LOCAL живет строго в пределах транзакции -> безопасно при пуле соединений."""
    tenant_id = session.info.get("tenant_id")
    # Уровень допуска пересчитывается на бэкенде из таблицы users; НИКОГДА из payload клиента.
    user_access_level = session.info.get("user_access_level")
    connection.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),  # is_local=true == SET LOCAL
        {"tid": str(tenant_id) if tenant_id else ""},
    )
    connection.execute(
        # Вторичный контур RLS по уровню. Пусто -> 0 (Public) в политиках (fail-closed).
        text("SELECT set_config('app.user_access_level', :lvl, true)"),
        {"lvl": str(int(user_access_level)) if user_access_level is not None else ""},
    )
```

- `session.info["tenant_id"]` и `session.info["user_access_level"]` проставляются middleware / DI-контейнером из аутентифицированного контекста — **никогда** из тела запроса клиента.
- `app.user_access_level` ставится **тем же** механизмом (`SET LOCAL` в той же транзакции), что и `app.current_tenant_id`, чтобы значение не «протекло» по пулу соединений в соседний запрос.
- Процессы админки и воркеры ходят под ролью `service_role` (`BYPASSRLS`); ключ `service_role` существует **только на сервере** и никогда не попадает во фронтенд-бандл.
- ⛔ **Query-путь конечного пользователя НИКОГДА не исполняется под `service_role`** — иначе фильтр по уровню (и по тенанту) обходится через `BYPASSRLS`. `service_role` допустим только для воркеров инжеста, миграций и backoffice-контура.
- Если `tenant_id` не установлен, политики изоляции дают пустую выборку (fail-closed), а не полный доступ. Если `app.user_access_level` не установлен — трактуется как `0` (Public), т.е. максимально ограничительно.
- Запрещено: `SET` без `LOCAL`, установка GUC вне транзакции, передача `tenant_id`/`access_level` параметром API.

### 4.1. Политики изоляции

Для обеспечения безопасности в многоарендной (Multi-Tenant) SaaS и On-Premise среде настраивается двухконтурный RLS:
1. **Service Role**: служебные процессы (фоновые воркеры Taskiq, CLI миграции) работают под ролью `service_role`.
2. **Арендная изоляция сессий (Tenant Isolation)**: для запросов пула сессий устанавливается `app.current_tenant_id` (`SET LOCAL app.current_tenant_id = '...'`).

```sql
-- Включение RLS
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE storage_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE licenses ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE parent_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE child_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages ENABLE ROW LEVEL SECURITY;

-- 1. Сервисный аккаунт имеет полный доступ (Service Role Key / Superuser)
CREATE POLICY service_all_tenants ON tenants FOR ALL TO service_role USING (true);
CREATE POLICY service_all_licenses ON licenses FOR ALL TO service_role USING (true);
CREATE POLICY service_all_users ON users FOR ALL TO service_role USING (true);
CREATE POLICY service_all_documents ON documents FOR ALL TO service_role USING (true);
CREATE POLICY service_all_parent_chunks ON parent_chunks FOR ALL TO service_role USING (true);
CREATE POLICY service_all_child_chunks ON child_chunks FOR ALL TO service_role USING (true);
CREATE POLICY service_all_conversations ON conversations FOR ALL TO service_role USING (true);
CREATE POLICY service_all_messages ON messages FOR ALL TO service_role USING (true);
CREATE POLICY service_all_storage_credentials ON storage_credentials FOR ALL TO service_role USING (true);
CREATE POLICY service_all_audit_log ON audit_log FOR ALL TO service_role USING (true);

-- storage_credentials и audit_log доступны ТОЛЬКО service_role:
-- клиентских политик для ролей authenticated/anon для них не существует (fail-closed).

-- 2. Клиентские политики с изоляцией по app.current_tenant_id
CREATE POLICY tenant_isolation_tenants ON tenants 
    FOR SELECT TO authenticated, anon
    USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_licenses ON licenses 
    FOR SELECT TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_users ON users 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

-- documents/parent_chunks/child_chunks: двойной предикат (tenant + уровень).
-- COALESCE(..., '0') -> отсутствие GUC трактуется как Public (fail-closed по уровню).
CREATE POLICY tenant_isolation_documents ON documents 
    FOR ALL TO authenticated, anon
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        AND access_level <= COALESCE(NULLIF(current_setting('app.user_access_level', true), ''), '0')::int
    );

CREATE POLICY tenant_isolation_parent_chunks ON parent_chunks 
    FOR ALL TO authenticated, anon
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        AND access_level <= COALESCE(NULLIF(current_setting('app.user_access_level', true), ''), '0')::int
    );

CREATE POLICY tenant_isolation_child_chunks ON child_chunks 
    FOR ALL TO authenticated, anon
    USING (
        tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        AND access_level <= COALESCE(NULLIF(current_setting('app.user_access_level', true), ''), '0')::int
    );

CREATE POLICY tenant_isolation_conversations ON conversations 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_messages ON messages 
    FOR ALL TO authenticated, anon
    USING (
        conversation_id IN (
            SELECT id FROM conversations 
            WHERE tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        )
    );

-- 3. RLS для таблиц Этапа B (группы/коллекции/ACL) — изоляция по tenant_id.
ALTER TABLE groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_collections ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_acl ENABLE ROW LEVEL SECURITY;

CREATE POLICY service_all_groups ON groups FOR ALL TO service_role USING (true);
CREATE POLICY service_all_user_groups ON user_groups FOR ALL TO service_role USING (true);
CREATE POLICY service_all_collections ON collections FOR ALL TO service_role USING (true);
CREATE POLICY service_all_document_collections ON document_collections FOR ALL TO service_role USING (true);
CREATE POLICY service_all_document_acl ON document_acl FOR ALL TO service_role USING (true);

CREATE POLICY tenant_isolation_groups ON groups 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_user_groups ON user_groups 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_collections ON collections 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_document_collections ON document_collections 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_document_acl ON document_acl 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
```

---

## 5. Регламент миграций (Alembic)

Миграции на чужой инфраструктуре (`on_premise_box`) — **самая частая причина упавшего обновления**. Контракт:

1. **Единый инструмент**: `alembic` поверх SQLAlchemy 2.0 (для Supabase-хостинга используется та же миграционная цепочка, а не `supabase migrations`).
2. **Автоприменение на старте**: entrypoint контейнера выполняет `alembic upgrade head` до запуска API/воркера; при ошибке контейнер падает (fail-fast), а не стартует с рассогласованной схемой.
3. **Pre-upgrade backup hook** (обязателен для Box): перед `upgrade head` выполняется `pg_dump` в смонтированный volume; при невозможности создать бэкап апгрейд **не начинается**.
4. **Additive-only**: разрешены `CREATE TABLE`, `ADD COLUMN` (nullable или с `DEFAULT`), `CREATE INDEX CONCURRENTLY`, `CREATE POLICY`.
   ⛔ Запрещены деструктивные операции: `DROP TABLE`, `DROP COLUMN`, сужающий `ALTER TYPE`, `NOT NULL` без значения по умолчанию, переименования. Удаление устаревших полей выполняется отдельным «expand → migrate → contract» циклом **минимум через один мажорный релиз**.
5. **Идемпотентность**: весь DDL пишется с `IF NOT EXISTS` / `CREATE OR REPLACE`, чтобы повторный прогон на частично применённой схеме был безопасен.
6. **Проверка в CI**: тест `alembic upgrade head` + `alembic downgrade -1` на `testcontainers-postgres`, а также линтер, блокирующий деструктивные ключевые слова в новых ревизиях.
