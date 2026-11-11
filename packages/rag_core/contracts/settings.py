"""Application settings (pydantic-settings) - the single source of runtime config.

`core/modules_spec.md` §1. Field names in UPPER case become the environment variable names
(see `.env.example`). `extra="ignore"` keeps the app tolerant to surplus env vars (unlike the
domain models, which forbid extra fields).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from rag_core.contracts.enums import DeploymentMode
from rag_core.contracts.models import DEFAULT_EMBEDDING_MODEL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deployment & Licensing
    deployment_mode: DeploymentMode = DeploymentMode.SAAS_CLOUD
    license_key: str | None = None
    license_public_key: str | None = None
    license_server_url: str | None = "https://license.your-rag-domain.com"

    # Vendor Backoffice & License Server (ONLY on the vendor side; NOT in the Box artifact).
    # See core/vendor_backoffice.md, core/license_server.md.
    backoffice_enabled: bool = False  # a separate vendor contour/--app; always False on the client
    backoffice_host: str = "127.0.0.1"  # a separate host/port, do not mix with the admin panel
    backoffice_port: int = 8090
    backoffice_rate_limit_per_minute: int = 60  # a separate (private) policy for backoffice
    backoffice_totp_required: bool = True  # 2FA mandatory for all backoffice roles
    # The private Ed25519 license-signing key is NOT stored in the web/DB: only in the signing
    # service / HSM / KMS.
    license_signing_service_url: str | None = None  # the signing-service endpoint
    license_crl_url: str | None = None  # the revocation list for an online revocation check

    # Supabase / Postgres
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    # The administrative connection (Alembic migrations, ingest workers, backoffice).
    # A role with BYPASSRLS (service_role / postgres) is NEVER used for user-retrieval:
    # a superuser/BYPASSRLS bypasses RLS (see storage_schema.md §4).
    postgres_dsn: str = "postgresql://postgres:postgrespassword@localhost:5432/postgres"
    # The user-retrieval connection: the app_user role (NOBYPASSRLS, a member of authenticated) -
    # otherwise the tenant_isolation_* policies will not apply and will return 0 rows.
    postgres_app_dsn: str | None = None

    # S3 Object Storage (Cloudflare R2 / MinIO / Selectel / AWS)
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_default_bucket: str = "enterprise-rag-documents"
    s3_region_name: str = "auto"

    # LLM & Embedder
    groq_api_key: str | None = None
    gemini_api_key: str | None = None
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_batch_size: int = 32
    # The ACTIVE embedding version stamped on every newly-ingested chunk. Bumping it is the atomic
    # re-embedding switch (core/architecture.md §3 / IMPLEMENTATION_PLAN.md Stage D): new ingests
    # write this version, and `ReembeddingService.reembed_corpus` backfills the corpus to it in
    # batches while old rows stay queryable; the retrieval layer (Stage E) filters by the active
    # version, so flipping this single value cuts over without mixed dimensions or search downtime.
    embedding_version: int = 1

    # Ingestion: token-aware Parent-Child splitter bounds (core/architecture.md §3) and the
    # durable-queue resilience policy (core/architecture.md §4). Single source of these values -
    # the pipeline/splitter/retry read them from here instead of hardcoding constants.
    chunk_parent_min_tokens: int = 800
    chunk_parent_max_tokens: int = 1200
    chunk_child_min_tokens: int = 150
    chunk_child_max_tokens: int = 250
    chunk_child_overlap_tokens: int = 30
    ingest_max_retries: int = 3
    ingest_retry_base_delay: float = 1.0
    ingest_retry_max_delay: float = 5.0
    file_watcher_debounce_seconds: float = 2.5

    # RAG Parameters
    retrieval_top_candidates: int = 30
    rrf_k: int = 60
    rrf_alpha: float = 0.6
    rrf_beta: float = 0.4
    confidence_threshold: float = 0.35
    max_history_turns: int = 5
    generation_temperature: float = 0.1  # determinism/factuality of RAG; recommended <= 0.3
    generation_max_tokens: int = 1024  # the LLM answer token limit

    # Channels (see core/channels.md)
    telegram_bot_token: str | None = None  # by default ONE shared bot; a separate bot - paid

    # Admin Panel & Session Auth (see core/admin_panel.md)
    admin_host: str = "127.0.0.1"  # in Box mode the port is not published outward
    admin_port: int = 8080
    admin_static_dir: str | None = "apps/admin/dist"  # serving the SPA as StaticFiles (Box)
    jwt_secret: str | None = None  # HS256, per-install, >= 32 bytes; generated on first start
    session_cookie_name: str = "rag_admin_session"
    session_ttl_seconds: int = 3600
    session_cookie_secure: bool = True  # HttpOnly + Secure + SameSite=Lax, NOT localStorage
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True
    totp_required_for_admin: bool = True
    admin_rate_limit_per_minute: int = 120  # the private policy (separate from the public widget)
    widget_rate_limit_per_minute: int = 20  # the public policy

    # Vector Store & Object Storage ports
    vector_store_provider: Literal["pgvector", "qdrant", "chroma"] = "pgvector"
    object_storage_provider: Literal["s3", "minio", "r2", "local"] = "s3"
    object_storage_local_root: str = "./data"

    # Observability (see core/eval_and_ops.md §2, core/admin_panel.md §11)
    langfuse_host: str = "https://cloud.langfuse.com"  # prod: vendor self-hosted; Cloud - dev only
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None
    langfuse_project_id: str | None = None  # one project per tenant (model B)
    langfuse_disable_signup: bool = True  # self-signup disabled - access is granted by the vendor
    trace_sample_rate: float = 1.0  # the fraction of traces to record (cost control)
    trace_retention_days: int = 30
    trace_mask_fields: list[str] = Field(default_factory=lambda: ["password", "api_key", "token"])
    telemetry_opt_out: bool = True  # 152-FZ/GDPR: telemetry is disabled by default
    # CANONICAL flag name: forwarding of anonymized metrics/traces from Box to the vendor (opt-in).
    # The former name telemetry_forward_enabled is a deprecated alias and MUST NOT be used.
    observability_forward_enabled: bool = False
    # TWO DIFFERENT CONCEPTS - do not confuse:
    #   1) LicenseFeature.OBSERVABILITY_FORWARD ("observability_forward") - an ENTITLEMENT
    #      (a license right); it determines WHETHER forwarding to the vendor is allowed at all.
    #   2) Settings/BoxDeployment.observability_forward_enabled - the client's RUNTIME switch.
    #   Forwarding is enabled ONLY when both are true (AND telemetry_opt_out=False).

    # Identity / end-user SSO (Stage C; gated by the sso_oidc license feature)
    oidc_enabled: bool = False  # enabling the authenticated widget mode
    oidc_issuer: str | None = None  # the IdP URL (OIDC discovery)
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_jwks_url: str | None = None  # for offline verification of the id_token signature
    oidc_groups_claim: str = "groups"  # the name of the claim with the group list in id_token
