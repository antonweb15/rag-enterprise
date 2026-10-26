# Storage Schema & Database DDL Specification (`core/storage_schema.md`)

## 1. Обзор схемы базы данных

Хранилище базируется на **Supabase (PostgreSQL 15+)** с расширением **`pgvector` (0.7+)**.
Схема поддерживает:
- Разделение на **Parent** (для формирования контекста LLM) и **Child** чанки (для точного векторного и sparse поиска).
- Поддержку **Dense (1024-dim)** и **Sparse (250002-dim)** векторов модели `BAAI/bge-m3`.
- **HNSW** индексы для высокоскоростного векторного и sparse поиска.
- **Двухконтурную модель RBAC**:
  - **Первичный контур (Primary Filter)**: предикат `WHERE d.access_level <= user_access_level` внутри хранимой функции `hybrid_search_rrf`. Значение `user_access_level` определяется бэкендом на основе аутентифицированного пользователя (таблица `users`) и не может быть подделано клиентом.
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
    allowed_origins JSONB NOT NULL DEFAULT '["*"]'::jsonb, -- список доменов для CORS Web-виджета
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
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
    channel TEXT NOT NULL DEFAULT 'telegram',         -- 'telegram', 'web', 'fastapi'
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
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_id ON parent_chunks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_document_id ON parent_chunks(document_id);

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
    channel TEXT NOT NULL DEFAULT 'telegram',         -- 'telegram', 'web', 'fastapi'
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
```

---

## 3. SQL Хранимая функция гибридного поиска (Hybrid Search with RRF & Multi-Tenancy)

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

Для обеспечения безопасности в многоарендной (Multi-Tenant) SaaS и On-Premise среде настраивается двухконтурный RLS:
1. **Service Role**: служебные процессы (фоновые воркеры Taskiq, CLI миграции) работают под ролью `service_role`.
2. **Арендная изоляция сессий (Tenant Isolation)**: для запросов пула сессий устанавливается `app.current_tenant_id` (`SET LOCAL app.current_tenant_id = '...'`).

```sql
-- Включение RLS
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
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

CREATE POLICY tenant_isolation_documents ON documents 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_parent_chunks ON parent_chunks 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_child_chunks ON child_chunks 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

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
```
