# Storage Schema & Database DDL Specification (`core/storage_schema.md`)

## 1. Database schema overview

The storage is based on **PostgreSQL 15+ / `pgvector` (0.7+)**. **Supabase is just one hosting option** for the same Postgres (alongside self-hosted Docker, a managed cloud or the customer's local DBMS in `on_premise_box` mode); it is **not** a mandatory Supabase dependency. For the dev environment SQLite via SQLAlchemy is acceptable (without the vector arm).

The schema supports:
- A split into **Parent** chunks (for forming the LLM context) and **Child** chunks (for precise vector and sparse search).
- Support for **Dense (1024-dim)** and **Sparse (250002-dim)** vectors of the `BAAI/bge-m3` model.
- **HNSW** indexes for high-speed vector and sparse search.
- A **dual-contour RBAC model**:
  - **Primary contour (Primary Filter)**: the `access_level` filter is applied in the query path — contractually at the application level (`BaseVectorStore` / `Retriever`), and in the pgvector path additionally inside the `hybrid_search_rrf` function (its internal detail, see §3) via the predicate `WHERE d.access_level <= user_access_level`. The `user_access_level` value is determined by the backend based on the authenticated user (the `users` table) and cannot be forged by the client.
  - **Secondary contour (Defense-in-depth)**: PostgreSQL **Row-Level Security (RLS)** for data isolation on direct table queries.
- Storage of users (`users`), sessions (`conversations`), messages (`messages`) and feedback marks (thumbs up/down).

---

## 2. PostgreSQL DDL Migration

```sql
-- 0. Ensuring runtime-environment compatibility (DB roles)
-- Three-role model (single source of truth, reconciled with IMPLEMENTATION_PLAN.md §3.1):
--   * service_role     — ingest workers, migrations, the backoffice contour; BYPASSRLS (bypasses RLS).
--   * authenticated    — the base client role on which ALL isolation policies hang (§4.1).
--   * app_user         — the DEDICATED application role for user-retrieval WITHOUT BYPASSRLS
--                        (connection via POSTGRES_APP_DSN, the Settings.postgres_app_dsn field).
--                        It is a MEMBER of authenticated, so it inherits the
--                        tenant_isolation_* policies (otherwise default-deny -> 0 rows).
--   The app_user password is set operationally (ALTER ROLE app_user PASSWORD / a secret), NOT in the migration.
--   POSTGRES_DSN (admin: service_role/postgres, BYPASSRLS) for user-retrieval is FORBIDDEN — it bypasses RLS.
-- The anon role/anon key are NOT used (DB access is via POSTGRES_DSN/POSTGRES_APP_DSN, not via PostgREST).
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
        CREATE ROLE service_role BYPASSRLS;
    END IF;
    -- The authenticated role already exists in Supabase; for self-hosted/Docker we create it (WITHOUT BYPASSRLS).
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
        CREATE ROLE authenticated NOBYPASSRLS;
    END IF;
    -- The anon role also pre-exists in Supabase; for self-hosted/Docker we create it (WITHOUT BYPASSRLS).
    -- It is referenced by the §4.1 policies (FOR ... TO authenticated, anon), so it MUST exist as a role
    -- even though DB access in this product never happens under anon (CREATE POLICY would otherwise fail).
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'anon') THEN
        CREATE ROLE anon NOBYPASSRLS;
    END IF;
    -- The dedicated application role for user-retrieval (WITHOUT BYPASSRLS), login via POSTGRES_APP_DSN.
    -- The password is set operationally (a secret), NOT in the migration.
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
        CREATE ROLE app_user LOGIN NOBYPASSRLS;
    END IF;
    -- KEY: app_user inherits all client policies defined TO authenticated (§4.1).
    GRANT authenticated TO app_user;
END
$$;

-- 0.1 Schema/table privileges for `authenticated` (migration 0008_schema_grants).
-- On Supabase these come from pg_default_acl, but a plain Postgres (self-hosted /
-- on_premise_box / the pgvector container) has none, so WITHOUT these grants the app_user
-- retrieval path fails with `permission denied for table ...` BEFORE RLS is reached and the
-- fail-closed contract (§4) is never even exercisable. Role attributes are also normalized
-- (ALTER ROLE ... (NO)BYPASSRLS) because CREATE ROLE above is IF NOT EXISTS-only. Idempotent.
-- Role-attribute normalization is idempotent AND tolerant of managed/reserved roles: on
-- Supabase `service_role`/`authenticated`/`anon` may only be ALTERed by a superuser, so an
-- insufficient_privilege there is expected and skipped (they already carry the right attributes).
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('service_role', true),
            ('authenticated', false),
            ('anon', false),
            ('app_user', false)
        ) AS t(rolname, want_bypassrls)
    LOOP
        BEGIN
            IF r.want_bypassrls THEN
                EXECUTE format('ALTER ROLE %I BYPASSRLS', r.rolname);
            ELSE
                EXECUTE format('ALTER ROLE %I NOBYPASSRLS', r.rolname);
            END IF;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'skip ALTER ROLE % (reserved/managed role)', r.rolname;
        END;
    END LOOP;
END
$$;
GRANT USAGE ON SCHEMA public TO authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO authenticated;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO authenticated;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO authenticated;

-- 1. Enabling extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Clients/tenants table (multi-tenancy for SaaS and Box)
CREATE TABLE IF NOT EXISTS tenants (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    slug TEXT NOT NULL UNIQUE,                        -- unique tenant identifier (company-slug)
    name TEXT NOT NULL,                               -- organization/client name
    public_key_hash TEXT,                             -- hash of the bound public key
    is_active BOOLEAN NOT NULL DEFAULT true,
    storage_type TEXT NOT NULL DEFAULT 's3',          -- 's3', 'minio', 'local'
    s3_bucket TEXT,
    s3_prefix TEXT,                                   -- base prefix path in the bucket (e.g. tenants/uuid/)
    allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb, -- strict domain allowlist for the Web-widget CORS (WITHOUT wildcard '*'; see channels.md)
    config JSONB NOT NULL DEFAULT '{}'::jsonb,        -- e.g. observability: {url, username, provisioned} (WITHOUT password); branding: {...}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);

-- 3. Licenses and subscriptions table
CREATE TABLE IF NOT EXISTS licenses (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    license_key TEXT NOT NULL UNIQUE,                 -- the signed Ed25519 license token
    plan TEXT NOT NULL DEFAULT 'standard'             -- canonical enum LicensePlan (modules_spec.md)
        CHECK (plan IN ('standard', 'pro', 'enterprise_box')),
    mode TEXT NOT NULL DEFAULT 'online_subscription'  -- canonical enum LicenseMode: persistence of the license mode
        CHECK (mode IN ('online_subscription', 'airgap_enterprise')),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ,                           -- license expiry (NULL for perpetual On-Premise)
    max_documents INTEGER NOT NULL DEFAULT 1000,
    max_users INTEGER NOT NULL DEFAULT 10,
    is_revoked BOOLEAN NOT NULL DEFAULT false,
    hardware_id TEXT,                                 -- hardware binding for the Box version
    -- Canonical feature registry = enum LicenseFeature (modules_spec.md); the default is reconciled with LicensePayload/LicenseIssueRequest.
    features JSONB NOT NULL DEFAULT '["hybrid_search", "s3_connector", "flashrank_rerank"]'::jsonb
        CHECK (
            jsonb_typeof(features) = 'array'
            AND features <@ '["web_widget","telegram","rest_sse","cli","enterprise_acl","collections","sso_oidc","hybrid_search","s3_connector","flashrank_rerank","observability_forward"]'::jsonb
        ),
    last_heartbeat_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_licenses_tenant_id ON licenses(tenant_id);
CREATE INDEX IF NOT EXISTS idx_licenses_key ON licenses(license_key);

-- 4. Users and access-rights table
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    channel_user_id TEXT NOT NULL,                    -- Telegram user ID or an external Auth ID
    channel TEXT NOT NULL DEFAULT 'telegram',         -- ChannelType: 'web', 'telegram', 'rest_sse', 'cli' (legacy 'fastapi' -> 'rest_sse')
    access_level INTEGER NOT NULL DEFAULT 0,          -- 0: Public, 1: Internal, 2: Confidential, 3: Strict
    is_active BOOLEAN NOT NULL DEFAULT true,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_channel_user UNIQUE (tenant_id, channel_user_id)
);

CREATE INDEX IF NOT EXISTS idx_users_tenant_channel ON users(tenant_id, channel_user_id);

-- 5. Documents table (source file metadata)
CREATE TABLE IF NOT EXISTS documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL DEFAULT 's3',           -- 's3', 'local_folder', 'confluence'
    source_uri TEXT NOT NULL,                         -- file path or S3 URI
    content_hash TEXT NOT NULL,                       -- SHA-256 hash of the content
    status TEXT NOT NULL DEFAULT 'pending',           -- 'pending', 'processing', 'indexed', 'failed'
    error_message TEXT,
    language TEXT NOT NULL DEFAULT 'ru',
    ingestion_job_id UUID,                            -- ID of the ingest task in the Taskiq queue
    access_level INTEGER NOT NULL DEFAULT 0,          -- 0: Public, 1: Internal, 2: Confidential, 3: Strict
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,      -- headings, author, pages, tags
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_tenant_content_hash UNIQUE (tenant_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_documents_tenant_status ON documents(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_documents_tenant_access_level ON documents(tenant_id, access_level);

-- 6. Parent chunks table (Parent Chunks: 800 - 1200 tokens)
CREATE TABLE IF NOT EXISTS parent_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- Denormalized access level (cascaded from documents); defense-in-depth for direct queries to parent
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_id ON parent_chunks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_document_id ON parent_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_access ON parent_chunks(tenant_id, access_level);

-- 7. Child chunks table (Child Chunks: 150 - 250 tokens)
CREATE TABLE IF NOT EXISTS child_chunks (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    parent_id UUID NOT NULL REFERENCES parent_chunks(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    token_count INTEGER NOT NULL,
    chunk_index INTEGER NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- Denormalized access level for a fast filter
    embedding vector(1024),                           -- Dense BGE-M3 (1024 dim)
    sparse_vector sparsevec(250002),                  -- Sparse BGE-M3 (a 250002-token dictionary)
    embedding_model_id TEXT NOT NULL DEFAULT 'BAAI/bge-m3',
    embedding_version INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_child_chunks_tenant_access ON child_chunks(tenant_id, access_level);
CREATE INDEX IF NOT EXISTS idx_child_chunks_parent_id ON child_chunks(parent_id);

-- HNSW index for dense vector search (Cosine distance)
CREATE INDEX IF NOT EXISTS idx_child_chunks_embedding_hnsw 
ON child_chunks USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- HNSW index for sparse vectors (Sparse Cosine / IP)
CREATE INDEX IF NOT EXISTS idx_child_chunks_sparse_hnsw 
ON child_chunks USING hnsw (sparse_vector sparsevec_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- 8. Dialog sessions and messages tables
CREATE TABLE IF NOT EXISTS conversations (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,                            -- user ID in Telegram / Channel
    channel TEXT NOT NULL DEFAULT 'telegram',         -- ChannelType: 'web', 'telegram', 'rest_sse', 'cli' (legacy 'fastapi' -> 'rest_sse')
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_tenant_user ON conversations(tenant_id, user_id);

-- 9. Dialog messages table
CREATE TABLE IF NOT EXISTS messages (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,                               -- 'user' | 'assistant' | 'system'
    content TEXT NOT NULL,
    feedback INTEGER DEFAULT NULL,                    -- 1 (👍), -1 (👎), NULL
    feedback_comment TEXT,
    langfuse_trace_id TEXT,                           -- Link to the trace in Langfuse
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);

-- 10. Tenant storage credentials (BaseObjectStorage: S3 / MinIO / R2 / local)
-- IMPORTANT: secrets are stored ONLY in encrypted form (AES-GCM with a key from KMS / an env master key).
-- Plaintext keys in the DB are forbidden; decryption is performed exclusively on the backend.
CREATE TABLE IF NOT EXISTS storage_credentials (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 's3',              -- 's3' | 'minio' | 'r2' | 'local'
    endpoint_url TEXT,
    region TEXT DEFAULT 'auto',
    bucket TEXT,
    prefix TEXT,                                      -- base per-tenant prefix ({tenant_id}/)
    access_key_id_enc BYTEA,                          -- AES-GCM ciphertext
    secret_access_key_enc BYTEA,                      -- AES-GCM ciphertext
    encryption_key_id TEXT,                           -- key identifier in KMS / rotation
    nonce BYTEA,                                      -- AES-GCM nonce
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_storage_credentials_tenant_provider UNIQUE (tenant_id, provider, bucket)
);

CREATE INDEX IF NOT EXISTS idx_storage_credentials_tenant_id ON storage_credentials(tenant_id);

-- 11. Administrators action log (Audit Log of the private admin contour)
CREATE TABLE IF NOT EXISTS audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
    actor_user_id UUID,                               -- admin-panel user (fastapi-users)
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

-- 12. Notification channel for the admin SSE (real-time ingest statuses)
-- A documents.status change is published via NOTIFY; in-memory pub/sub is forbidden.
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

### 2.1. Channel migration (`channel`) and the domain allowlist

> Context: `UserContext.channel` was moved to `ChannelType` (`web/telegram/rest_sse/cli`, see [`modules_spec.md`](./modules_spec.md)).
> The legacy value `'fastapi'` is out of sync and is not parsed by the str-enum — a backfill is required.
> The `allowed_origins` default is fixed from `'["*"]'` to `'[]'` (a wildcard by default is a security bug).

```sql
-- Channel backfill: legacy 'fastapi' -> 'rest_sse'
UPDATE users          SET channel = 'rest_sse' WHERE channel = 'fastapi';
UPDATE conversations  SET channel = 'rest_sse' WHERE channel = 'fastapi';

-- Removing the unsafe wildcard default for existing tenants that did not configure the allowlist manually.
-- NOTE: perform deliberately — the widget stops being served until an origin is added explicitly.
UPDATE tenants SET allowed_origins = '[]'::jsonb WHERE allowed_origins = '["*"]'::jsonb;
```

### 2.2. Vendor contour tables (Backoffice) — additive-only

> Context: this is the vendor's **cross-tenant** data (see [`vendor_backoffice.md`](./vendor_backoffice.md), [`license_server.md`](./license_server.md)).
> All backoffice tables are **outside tenant-RLS** (no `app.current_tenant_id` column/policy) and are accessible **only** to the backoffice-contour `service_role`.
> In the `on_premise_box` artifact this contour is **not deployed**; the tables exist on the vendor infrastructure (a separate backoffice DB/schema is recommended).
> The private Ed25519 license-signing key is **not stored** here — only in the signing service / HSM / KMS.

```sql
-- 13. Buyers (legal entities). SEPARATE from tenants: 1 customer : N tenants.
CREATE TABLE IF NOT EXISTS customers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    legal_name TEXT NOT NULL,
    display_name TEXT,
    inn TEXT,                                          -- INN / tax id
    contact_email TEXT NOT NULL,
    contact_phone TEXT,
    contract_number TEXT,
    status TEXT NOT NULL DEFAULT 'trial',             -- 'trial', 'active', 'suspended', 'churned'
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, -- additional buyer attributes
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 14. Backoffice users (vendor staff). Do NOT confuse with users (tenant users).
CREATE TABLE IF NOT EXISTS backoffice_users (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email TEXT NOT NULL UNIQUE,
    hashed_password TEXT NOT NULL,                     -- Argon2id
    role TEXT NOT NULL DEFAULT 'support',             -- BackofficeRole: 'superadmin', 'support', 'finance', 'sales'
    is_active BOOLEAN NOT NULL DEFAULT true,
    totp_secret_enc BYTEA,                             -- 2FA is mandatory for all backoffice roles
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 15. Orders (a minimal billing skeleton; full billing — an external provider).
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    plan TEXT NOT NULL,
    amount_minor BIGINT,                              -- amount in minor units (kopecks/cents)
    currency TEXT NOT NULL DEFAULT 'RUB',
    status TEXT NOT NULL DEFAULT 'pending',          -- 'pending', 'paid', 'cancelled', 'refunded'
    external_provider TEXT,                           -- 'stripe' | 'paddle' | 'manual' | ...
    external_ref TEXT,                               -- payment id in the external provider
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);

-- 16. Activation codes 'RAG-ENT-XXXX-YYYY-ZZZZ' (issued by the vendor per order).
CREATE TABLE IF NOT EXISTS activation_codes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    code TEXT NOT NULL UNIQUE,                        -- 'RAG-ENT-XXXX-YYYY-ZZZZ'
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    order_id UUID REFERENCES orders(id) ON DELETE SET NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued',           -- 'issued', 'redeemed', 'revoked', 'expired'  (code lifecycle)
    redeemed_by_tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_activation_codes_customer ON activation_codes(customer_id);
CREATE INDEX IF NOT EXISTS idx_activation_codes_status ON activation_codes(status);

-- 17. Registry of sold/deployed boxes (on-premise installations).
CREATE TABLE IF NOT EXISTS box_deployments (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    customer_id UUID NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
    tenant_id UUID REFERENCES tenants(id) ON DELETE SET NULL,
    image_version TEXT,                              -- version of the delivered closed image
    delivered_at TIMESTAMPTZ,
    hardware_id TEXT,                               -- installation fingerprint
    last_heartbeat_at TIMESTAMPTZ,
    grace_period_expires_at TIMESTAMPTZ,
    restricted_mode BOOLEAN NOT NULL DEFAULT false,
    telemetry_opt_out BOOLEAN NOT NULL DEFAULT true,          -- respected by the vendor; trace/metric forwarding only when false
    observability_forward_enabled BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_box_deployments_customer ON box_deployments(customer_id);
CREATE INDEX IF NOT EXISTS idx_box_deployments_hardware ON box_deployments(hardware_id);

-- 18. Global vendor audit (cross-tenant; separate from the tenant-scoped audit_log).
--     Break-glass entries (impersonation) of vendor staff into a foreign tenant are also written here.
CREATE TABLE IF NOT EXISTS backoffice_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    actor_backoffice_user_id UUID REFERENCES backoffice_users(id) ON DELETE SET NULL,
    actor_email TEXT,
    action TEXT NOT NULL,                            -- 'license.issue', 'license.revoke', 'tenant.impersonate', ... (action type)
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

-- 19. Extending licenses with vendor issuance/revocation fields (additive, nullable).
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS issued_by UUID REFERENCES backoffice_users(id);
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS customer_id UUID REFERENCES customers(id);
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;
ALTER TABLE licenses ADD COLUMN IF NOT EXISTS revocation_reason TEXT;
```

> RLS: the backoffice tables (`customers`, `backoffice_users`, `orders`, `activation_codes`, `box_deployments`, `backoffice_audit_log`) are **not included** in the tenant-isolation policies of §4. Access to them is exclusively under the backoffice `service_role`; there are no client policies (`authenticated`/`anon`) for them (fail-closed).

### 2.3. The `access_level` re-classification contract (Stage A, P0)

> Context: `access_level` is **denormalized** in `child_chunks` and `parent_chunks` for a cheap primary filter. The source of truth is `documents.access_level`.
> Any change of a document's level must **cascadingly and transactionally** rewrite the level in all its chunks, otherwise a window of inconsistency appears (Confidential content leaks as Public or vice versa).

```sql
-- (1) One-time backfill of existing data when introducing the parent_chunks.access_level column.
--     Without it old parent rows will remain Public (DEFAULT 0) and previously Confidential content will be "revealed".
UPDATE parent_chunks p
   SET access_level = d.access_level
  FROM documents d
 WHERE p.document_id = d.id
   AND p.access_level IS DISTINCT FROM d.access_level;

-- A safeguard for child_chunks (in case of previously inconsistent rows):
UPDATE child_chunks c
   SET access_level = d.access_level
  FROM parent_chunks p
  JOIN documents d ON p.document_id = d.id
 WHERE c.parent_id = p.id
   AND c.access_level IS DISTINCT FROM d.access_level;

-- (2) Cascade re-classification trigger: a documents.access_level change in a SINGLE transaction
--     rewrites parent_chunks.access_level and child_chunks.access_level.
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

- **Transactionality is mandatory.** A Taskiq cascade task is allowed only with an explicit "eventually consistent" mark and a temporary **fail-closed** (until the synchronization completes the document's chunks are considered inaccessible), otherwise a window of inconsistency appears.
- On ingest, new `parent_chunks`/`child_chunks` inherit `access_level` from their `documents` (set by the worker on write).

### 2.4. Groups, collections and per-document ACL (Stage B, P1 — behind the `enterprise_acl` feature flag)

> Context: the flat `access_level` scale (0-3) covers ~80% of cases and remains the **default primary filter**.
> For enterprise flexibility ("department A sees only its own documents, even if all are Internal") an optional layer is added: principal groups + collections (knowledge bases) + a fine-grained "principal -> document" ACL.
> All tables are **additive-only**, under tenant-RLS (§4). The features are enabled by the `enterprise_acl`/`collections` license flag (see [`licensing_and_distribution.md`](./licensing_and_distribution.md)); when the flag is off only `access_level` works.

```sql
-- 20. Principal groups (mapped to the org structure / IdP groups).
CREATE TABLE IF NOT EXISTS groups (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    access_level INTEGER NOT NULL DEFAULT 0,          -- the group's base clearance level
    external_ref TEXT,                                -- the group id in the IdP (OIDC/SAML/SCIM), NULL for local ones
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_groups_tenant_name UNIQUE (tenant_id, name)
);

CREATE INDEX IF NOT EXISTS idx_groups_tenant_id ON groups(tenant_id);

-- 21. Users' membership in groups (m2m).
CREATE TABLE IF NOT EXISTS user_groups (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    group_id UUID NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, group_id)
);

CREATE INDEX IF NOT EXISTS idx_user_groups_group ON user_groups(group_id);
CREATE INDEX IF NOT EXISTS idx_user_groups_tenant ON user_groups(tenant_id);

-- 22. Collections (knowledge bases / folders, with hierarchy).
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

-- 23. Binding documents to collections (m2m).
CREATE TABLE IF NOT EXISTS document_collections (
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    collection_id UUID NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, collection_id)
);

CREATE INDEX IF NOT EXISTS idx_document_collections_collection ON document_collections(collection_id);

-- 24. Fine-grained ACL: "principal (user|group) -> document". permission — an enum designed for extension.
CREATE TABLE IF NOT EXISTS document_acl (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL,                     -- 'user' | 'group'
    principal_id UUID NOT NULL,                       -- users.id | groups.id (by principal_type)
    permission TEXT NOT NULL DEFAULT 'read',          -- 'read' (the enum is designed for extension: 'write'/'manage'/...)
    granted_by UUID,                                  -- users.id of the admin who granted it (audit)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_document_acl UNIQUE (tenant_id, document_id, principal_type, principal_id, permission)
);

CREATE INDEX IF NOT EXISTS idx_document_acl_document ON document_acl(document_id);
CREATE INDEX IF NOT EXISTS idx_document_acl_principal ON document_acl(tenant_id, principal_type, principal_id);
```

- **Effective-access semantics** (computed by `PolicyEvaluator`, see [`modules_spec.md`](./modules_spec.md)): a document is accessible to a subject if
  `access_level(doc) <= max(user.access_level, max(group.access_level))` **OR** there is an explicit `document_acl` row for the `user` / one of their `group`s.
  When `enterprise_acl` is off the second term (ACL/groups) is ignored — only plain `access_level` remains.
- The return contract of `PolicyEvaluator` is a **set/predicate of allowed resources** (`allowed_document_ids` / an SQL predicate), applied **before** top-k/RRF (not a post-filter), otherwise ranking and RRF break.

---

## 3. SQL stored function for hybrid search (an internal detail of `PgVectorStore`)

> ⚠️ **Contract status**: the `hybrid_search_rrf` function is an **internal optimization detail of the `PgVectorStore` implementation**, not a system contract.
> The contract is `BaseVectorStore.search_dense()` / `BaseVectorStore.search_sparse()` + RRF fusion in the **application layer** (`Retriever.fuse_rrf`, see [`modules_spec.md`](./modules_spec.md) §2.3).
> The function is called only when `capabilities.supports_hybrid_rrf = True` and must yield a result identical to the code fusion. This preserves the optimized path for pgvector without turning the "swappable vector store" into a fiction.

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
-- Pin search_path (SECURITY DEFINER hardening): resolve objects/operators only from trusted
-- schemas, preventing search_path hijacking by the caller.
SET search_path = pg_catalog, public
AS $$
BEGIN
    -- By design the primary access filter trusts target_tenant_id / user_access_level: they are
    -- computed by the backend from the authenticated user (never the client body), see §4.0.
    -- RLS is the secondary defense-in-depth contour.
    -- Tune HNSW accuracy for the query (P95 latency < 400ms while keeping high Recall)
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
          -- Near-zero sparse norm -> cosine distance is numerically unstable (division by ~0).
          -- Skip the sparse arm below the cutoff and fall back to dense-only RRF.
          AND l2_norm(query_sparse) > 1e-6
          AND l2_norm(c.sparse_vector) > 1e-6
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

## 4. Row-Level Security (RLS) configuration

### 4.0. The "RLS + SQLAlchemy" contract (mandatory)

SQLAlchemy uses a **connection pool** under a single DB role. Without explicitly setting the GUC at the start of **every** transaction RLS will not work, and when a connection is reused the value will "leak" into a neighboring query. Therefore setting `app.current_tenant_id` is a mandatory system contract:

```python
# packages/rag_core/db/session.py
from sqlalchemy import event, text
from sqlalchemy.orm import Session

@event.listens_for(Session, "after_begin")
def set_tenant_guc(session: Session, transaction, connection) -> None:
    """SET LOCAL lives strictly within the transaction -> safe with a connection pool."""
    tenant_id = session.info.get("tenant_id")
    # The clearance level is recomputed on the backend from the users table; NEVER from the client payload.
    user_access_level = session.info.get("user_access_level")
    connection.execute(
        text("SELECT set_config('app.current_tenant_id', :tid, true)"),  # is_local=true == SET LOCAL  # noqa
        {"tid": str(tenant_id) if tenant_id else ""},
    )
    connection.execute(
        # The secondary level-based RLS contour. Empty -> 0 (Public) in the policies (fail-closed).
        text("SELECT set_config('app.user_access_level', :lvl, true)"),
        {"lvl": str(int(user_access_level)) if user_access_level is not None else ""},
    )
```

- `session.info["tenant_id"]` and `session.info["user_access_level"]` are set by the middleware / DI container from the authenticated context — **never** from the client request body.
- `app.user_access_level` is set by the **same** mechanism (`SET LOCAL` in the same transaction) as `app.current_tenant_id`, so the value does not "leak" across the connection pool into a neighboring query.
- Admin processes and workers operate under the `service_role` role (`BYPASSRLS`); the `service_role` key exists **only on the server** and never gets into the frontend bundle.
- ⛔ **The end-user query path is NEVER executed under `service_role`** — otherwise the level (and tenant) filter is bypassed via `BYPASSRLS`. `service_role` is allowed only for ingest workers, migrations and the backoffice contour.
- If `tenant_id` is not set, the isolation policies yield an empty result set (fail-closed), not full access. If `app.user_access_level` is not set — it is treated as `0` (Public), i.e. maximally restrictive.
- Forbidden: `SET` without `LOCAL`, setting the GUC outside a transaction, passing `tenant_id`/`access_level` as an API parameter.

### 4.1. Isolation policies

To ensure security in a Multi-Tenant SaaS and On-Premise environment, a dual-contour RLS is configured:
1. **Service Role**: service processes (background Taskiq workers, CLI migrations) run under the `service_role` role (`BYPASSRLS`).
2. **Tenant Isolation (session-level)**: for session-pool queries `app.current_tenant_id` is set (`SET LOCAL app.current_tenant_id = '...'`).

> ⚠️ **The user-retrieval role = `app_user` (WITHOUT `BYPASSRLS`), connects via `POSTGRES_APP_DSN` (`Settings.postgres_app_dsn`).** The policies below are declared `TO authenticated`, and `app_user` is made a **member** of `authenticated` (`GRANT authenticated TO app_user` in DDL section 0), so it inherits them. Without this membership RLS would return **0 rows** (default-deny) for the dedicated role. The `anon` role is kept in the policies for compatibility with Supabase PostgREST, but in this product DB access goes only via `app_user`/`service_role`.
>
> ⛔ **Two DSNs, do not confuse:** `POSTGRES_DSN` (`Settings.postgres_dsn`) — admin/migrations/workers (`service_role`/`postgres`, bypasses RLS); `POSTGRES_APP_DSN` (`Settings.postgres_app_dsn`) — user-retrieval (`app_user`, RLS applies). User-retrieval via `POSTGRES_DSN` is forbidden: a superuser/`BYPASSRLS` bypasses RLS, and the fail-closed contract of §4.0 will not work.

```sql
-- Enabling RLS
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

-- 1. The service account has full access (Service Role Key / Superuser)
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

-- storage_credentials and audit_log are accessible ONLY to service_role:
-- there are no client policies for the authenticated/anon roles for them (fail-closed).

-- 2. Client policies with isolation by app.current_tenant_id
CREATE POLICY tenant_isolation_tenants ON tenants 
    FOR SELECT TO authenticated, anon
    USING (id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_licenses ON licenses 
    FOR SELECT TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

CREATE POLICY tenant_isolation_users ON users 
    FOR ALL TO authenticated, anon
    USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);

-- documents/parent_chunks/child_chunks: a double predicate (tenant + level).
-- COALESCE(..., '0') -> a missing GUC is treated as Public (fail-closed by level).
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

-- 3. RLS for the Stage B tables (groups/collections/ACL) — isolation by tenant_id.
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

## 5. Migration procedure (Alembic)

Migrations on foreign infrastructure (`on_premise_box`) are **the most common cause of a failed update**. The contract:

1. **A single tool**: `alembic` on top of SQLAlchemy 2.0 (for Supabase hosting the same migration chain is used, not `supabase migrations`).
2. **Auto-apply on start**: the container entrypoint runs `alembic upgrade head` before starting the API/worker; on an error the container fails (fail-fast) rather than starting with an inconsistent schema.
3. **Pre-upgrade backup hook** (mandatory for Box): before `upgrade head` a `pg_dump` is made into a mounted volume; if a backup cannot be created the upgrade **does not start**.
4. **Additive-only**: allowed are `CREATE TABLE`, `ADD COLUMN` (nullable or with `DEFAULT`), `CREATE INDEX CONCURRENTLY`, `CREATE POLICY`.
   ⛔ Destructive operations are forbidden: `DROP TABLE`, `DROP COLUMN`, a narrowing `ALTER TYPE`, `NOT NULL` without a default value, renames. Removing deprecated fields is done via a separate "expand → migrate → contract" cycle **at least one major release later**.
5. **Idempotency**: all DDL is written with `IF NOT EXISTS` / `CREATE OR REPLACE`, so a re-run on a partially applied schema is safe.
6. **CI check**: an `alembic upgrade head` + `alembic downgrade -1` test on `testcontainers-postgres`, as well as a linter blocking destructive keywords in new revisions.
