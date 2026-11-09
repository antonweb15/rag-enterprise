"""core schema: roles, extensions, tenant-scoped tables, ingest NOTIFY trigger

Canonical DDL - core/storage_schema.md §2 (items 0-12). Additive-only, idempotent
(IF NOT EXISTS / CREATE OR REPLACE), safe to re-run.

Revision ID: 0001_core_schema
Revises:
Create Date: 2026-11-09
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_core_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 0. Three-role model (storage_schema.md §0): service_role (BYPASSRLS),
    #    authenticated (all isolation policies), app_user (user-retrieval WITHOUT BYPASSRLS,
    #    a member of authenticated). The app_user password is set operationally, NOT in the
    #    migration.
    op.execute("""
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'service_role') THEN
            CREATE ROLE service_role BYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'authenticated') THEN
            CREATE ROLE authenticated NOBYPASSRLS;
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app_user') THEN
            CREATE ROLE app_user LOGIN NOBYPASSRLS;
        END IF;
        GRANT authenticated TO app_user;
    END
    $$;
    """)

    # 1. Extensions
    op.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
    op.execute("CREATE EXTENSION IF NOT EXISTS vector;")

    # 2. tenants
    op.execute("""
    CREATE TABLE IF NOT EXISTS tenants (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        slug TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        public_key_hash TEXT,
        is_active BOOLEAN NOT NULL DEFAULT true,
        storage_type TEXT NOT NULL DEFAULT 's3',
        s3_bucket TEXT,
        s3_prefix TEXT,
        allowed_origins JSONB NOT NULL DEFAULT '[]'::jsonb,
        config JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_tenants_slug ON tenants(slug);
    """)

    # 3. licenses
    op.execute("""
    CREATE TABLE IF NOT EXISTS licenses (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        license_key TEXT NOT NULL UNIQUE,
        plan TEXT NOT NULL DEFAULT 'standard'
            CHECK (plan IN ('standard', 'pro', 'enterprise_box')),
        mode TEXT NOT NULL DEFAULT 'online_subscription'
            CHECK (mode IN ('online_subscription', 'airgap_enterprise')),
        issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        expires_at TIMESTAMPTZ,
        max_documents INTEGER NOT NULL DEFAULT 1000,
        max_users INTEGER NOT NULL DEFAULT 10,
        is_revoked BOOLEAN NOT NULL DEFAULT false,
        hardware_id TEXT,
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
    """)

    # 4. users
    op.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        channel_user_id TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'telegram',
        access_level INTEGER NOT NULL DEFAULT 0,
        is_active BOOLEAN NOT NULL DEFAULT true,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_tenant_channel_user UNIQUE (tenant_id, channel_user_id)
    );
    CREATE INDEX IF NOT EXISTS idx_users_tenant_channel ON users(tenant_id, channel_user_id);
    """)

    # 5. documents
    op.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        source_type TEXT NOT NULL DEFAULT 's3',
        source_uri TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        error_message TEXT,
        language TEXT NOT NULL DEFAULT 'ru',
        ingestion_job_id UUID,
        access_level INTEGER NOT NULL DEFAULT 0,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_tenant_content_hash UNIQUE (tenant_id, content_hash)
    );
    CREATE INDEX IF NOT EXISTS idx_documents_tenant_status ON documents(tenant_id, status);
    CREATE INDEX IF NOT EXISTS idx_documents_tenant_access_level ON documents(tenant_id, access_level);
    """)

    # 6. parent_chunks
    op.execute("""
    CREATE TABLE IF NOT EXISTS parent_chunks (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL,
        access_level INTEGER NOT NULL DEFAULT 0,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_id ON parent_chunks(tenant_id);
    CREATE INDEX IF NOT EXISTS idx_parent_chunks_document_id ON parent_chunks(document_id);
    CREATE INDEX IF NOT EXISTS idx_parent_chunks_tenant_access ON parent_chunks(tenant_id, access_level);
    """)

    # 7. child_chunks (+ HNSW dense/sparse indexes)
    op.execute("""
    CREATE TABLE IF NOT EXISTS child_chunks (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        parent_id UUID NOT NULL REFERENCES parent_chunks(id) ON DELETE CASCADE,
        content TEXT NOT NULL,
        token_count INTEGER NOT NULL,
        chunk_index INTEGER NOT NULL,
        access_level INTEGER NOT NULL DEFAULT 0,
        embedding vector(1024),
        sparse_vector sparsevec(250002),
        embedding_model_id TEXT NOT NULL DEFAULT 'BAAI/bge-m3',
        embedding_version INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_child_chunks_tenant_access ON child_chunks(tenant_id, access_level);
    CREATE INDEX IF NOT EXISTS idx_child_chunks_parent_id ON child_chunks(parent_id);
    CREATE INDEX IF NOT EXISTS idx_child_chunks_embedding_hnsw
        ON child_chunks USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    CREATE INDEX IF NOT EXISTS idx_child_chunks_sparse_hnsw
        ON child_chunks USING hnsw (sparse_vector sparsevec_cosine_ops)
        WITH (m = 16, ef_construction = 64);
    """)

    # 8-9. conversations / messages
    op.execute("""
    CREATE TABLE IF NOT EXISTS conversations (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        channel TEXT NOT NULL DEFAULT 'telegram',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_conversations_tenant_user ON conversations(tenant_id, user_id);

    CREATE TABLE IF NOT EXISTS messages (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        feedback INTEGER DEFAULT NULL,
        feedback_comment TEXT,
        langfuse_trace_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
    """)

    # 10. storage_credentials (secrets only in encrypted form)
    op.execute("""
    CREATE TABLE IF NOT EXISTS storage_credentials (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        provider TEXT NOT NULL DEFAULT 's3',
        endpoint_url TEXT,
        region TEXT DEFAULT 'auto',
        bucket TEXT,
        prefix TEXT,
        access_key_id_enc BYTEA,
        secret_access_key_enc BYTEA,
        encryption_key_id TEXT,
        nonce BYTEA,
        is_active BOOLEAN NOT NULL DEFAULT true,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_storage_credentials_tenant_provider UNIQUE (tenant_id, provider, bucket)
    );
    CREATE INDEX IF NOT EXISTS idx_storage_credentials_tenant_id ON storage_credentials(tenant_id);
    """)

    # 11. audit_log
    op.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        actor_user_id UUID,
        actor_email TEXT,
        action TEXT NOT NULL,
        object_type TEXT,
        object_id TEXT,
        ip_address INET,
        user_agent TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_created ON audit_log(tenant_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor_user_id);
    """)

    # 12. Ingest status NOTIFY trigger (channel ingest_events)
    op.execute("""
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
    """)


def downgrade() -> None:
    # Reverse direction (for the CI upgrade/downgrade check). Roles and extensions
    # are not dropped (they may be shared by the cluster/other schemas).
    op.execute("DROP TRIGGER IF EXISTS trg_documents_status_notify ON documents;")
    op.execute("DROP FUNCTION IF EXISTS notify_document_status();")
    for table in (
        "audit_log",
        "storage_credentials",
        "messages",
        "conversations",
        "child_chunks",
        "parent_chunks",
        "documents",
        "users",
        "licenses",
        "tenants",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE;")
