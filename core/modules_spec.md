# Python Modules & Contract Interfaces Specification (`core/modules_spec.md`)

## 1. Base entities and Pydantic V2 models

```python
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, AsyncGenerator, Dict, List, Optional, Tuple, Literal
from uuid import UUID, uuid4
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- Enumerations ---
class DocumentStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"

class AccessLevel(int, Enum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    STRICT = 3

class IntentType(str, Enum):
    RAG_SEARCH = "rag_search"
    CHIT_CHAT = "chit_chat"
    META_COMMAND = "meta_command"

class AdminRole(str, Enum):
    ADMIN = "admin"                            # full access: documents, license, users
    OPERATOR = "operator"                      # upload and reindex without delete
    VIEWER = "viewer"                          # read-only lists, chunks and the dashboard

class BackofficeRole(str, Enum):
    """Roles of the vendor contour (backoffice), SEPARATE from the tenant admin panel.
    These roles are NOT part of AdminRole and are not granted to tenant users;
    they are held by vendor staff. See core/vendor_backoffice.md."""
    SUPERADMIN = "superadmin"                  # full cross-tenant access, management of backoffice users
    SUPPORT = "support"                        # impersonation / "log in as" a tenant (break-glass) with mandatory auditing
    FINANCE = "finance"                        # billing, orders, invoices, renewals (no access to tenant data)
    SALES = "sales"                            # customers, orders, activation codes (no access to tenant data)

class DeploymentMode(str, Enum):
    SAAS_CLOUD = "saas_cloud"
    ON_PREMISE_BOX = "on_premise_box"

class LicensePlan(str, Enum):
    """The canonical set of license plans. The single source of truth for the DDL
    (licenses.plan CHECK) and all Pydantic models; the default is STANDARD."""
    STANDARD = "standard"
    PRO = "pro"
    ENTERPRISE_BOX = "enterprise_box"

class LicenseMode(str, Enum):
    """License mode: online subscription (heartbeat) vs air-gapped enterprise-box
    (offline validation + grace period). Persisted in licenses.mode."""
    ONLINE_SUBSCRIPTION = "online_subscription"
    AIRGAP_ENTERPRISE = "airgap_enterprise"

class LicenseFeature(str, Enum):
    """The SINGLE canonical registry of license feature flags (entitlements).
    The source of truth for LicensePayload/LicenseIssueRequest.features and for
    the CHECK constraint licenses.features (storage_schema.md). Any value
    outside this enum in features is forbidden (the DDL rejects it via '<@').
      - Delivery channels: WEB_WIDGET/TELEGRAM/REST_SSE/CLI.
      - Enterprise capabilities: ENTERPRISE_ACL/COLLECTIONS/SSO_OIDC.
      - Base ones: HYBRID_SEARCH/S3_CONNECTOR/FLASHRANK_RERANK.
      - OBSERVABILITY_FORWARD — the license ENTITLEMENT for forwarding anonymized
        traces to the vendor. Do NOT confuse with the runtime switch
        Settings/BoxDeployment.observability_forward_enabled (see the § below)."""
    WEB_WIDGET = "web_widget"
    TELEGRAM = "telegram"
    REST_SSE = "rest_sse"
    CLI = "cli"
    ENTERPRISE_ACL = "enterprise_acl"
    COLLECTIONS = "collections"
    SSO_OIDC = "sso_oidc"
    HYBRID_SEARCH = "hybrid_search"
    S3_CONNECTOR = "s3_connector"
    FLASHRANK_RERANK = "flashrank_rerank"
    OBSERVABILITY_FORWARD = "observability_forward"

# The canonical default feature set (reconciled between LicensePayload, LicenseIssueRequest
# and the DDL licenses.features DEFAULT). The Standard plan gets the base set.
# ⚠️ Delivery channels (WEB_WIDGET/TELEGRAM/REST_SSE/CLI) are NOT in the default and are granted
#    separately on license issuance/upgrade: enabling a channel has an operational cost
#    (a bot token, a widget config), and the right to a channel is an explicit vendor decision (channels.md §2).
DEFAULT_LICENSE_FEATURES: List[LicenseFeature] = [
    LicenseFeature.HYBRID_SEARCH,
    LicenseFeature.S3_CONNECTOR,
    LicenseFeature.FLASHRANK_RERANK,
]

# --- Delivery channels (see core/channels.md) ---
class ChannelType(str, Enum):
    WEB_WIDGET = "web"          # the primary end-user interface (Shadow DOM, white-label)
    TELEGRAM = "telegram"       # a secondary delivery channel
    REST_SSE = "rest_sse"       # headless REST / SSE for custom integrations
    CLI = "cli"                 # typer console (air-gapped Box)

class ChannelCapabilities(BaseModel):
    """Capabilities of a specific channel. Read ONLY at the channel boundary
    (render/delivery); the search business logic (retrieval) does NOT branch on them.
    The defaults are conservative: an absent/unknown capability does not break the render.
    Each adapter must have a contract test confirming the declared flags."""
    supports_streaming: bool = False
    supports_markdown: bool = False
    supports_citations: bool = False
    supports_feedback_buttons: bool = False
    supports_attachments: bool = False
    supports_reply_threading: bool = False
    supports_commands: bool = False
    supports_callback_payloads: bool = False
    supports_cancellation: bool = False
    max_message_length: Optional[int] = None    # None = no limit / set by the adapter (e.g. Telegram = 4096)
    max_attachments_per_message: int = 0

class ChannelEventType(str, Enum):
    TEXT_MESSAGE = "text_message"
    COMMAND = "command"         # /start, /help, /activate
    CALLBACK = "callback"       # 👍/👎 and other callback buttons
    ATTACHMENT = "attachment"
    EDIT = "edit"
    DELETE = "delete"

class AttachmentRef(BaseModel):
    file_id: str                                 # identifier in the external channel or a storage_ref
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None

class ChannelEvent(BaseModel):
    """A channel-neutral incoming event. Channel adapters must map
    native events into a ChannelEvent. The retrieval core accepts ONLY a ChannelEvent
    and knows nothing about the specific channel ('one input, two outputs')."""
    event_type: ChannelEventType = ChannelEventType.TEXT_MESSAGE
    user_id: UUID
    tenant_id: UUID
    channel: ChannelType
    channel_user_id: str
    session_id: Optional[str] = None
    text: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)   # callback_data, command arguments
    attachments: List[AttachmentRef] = Field(default_factory=list)
    created_at: datetime

# --- Typed branding (see core/channels.md §7) ---
class WidgetBranding(BaseModel):
    """Widget white-label. 100% controlled from the admin panel.
    NOTE: welcome_message/disclaimer are rendered as TEXT (not raw HTML),
    a CSP is mandatory on the widget; logo_url — only https + a Content-Type allowlist."""
    config_version: int = 1
    title: Optional[str] = Field(None, max_length=120)
    subtitle: Optional[str] = Field(None, max_length=200)
    logo_url: Optional[str] = Field(None, pattern=r"^https://")   # images only (Content-Type checked on the backend)
    primary_color: Optional[str] = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    welcome_message: Optional[str] = Field(None, max_length=1000)
    disclaimer: Optional[str] = Field(None, max_length=500)
    suggested_questions: List[Annotated[str, Field(max_length=120)]] = Field(default_factory=list, max_length=5)  # <=5 questions, <=120 chars each

class TelegramBranding(BaseModel):
    """Partial Telegram white-label. The avatar is NOT configured from the admin panel
    (only @BotFather; setMyPhoto does not exist in the Bot API)."""
    config_version: int = 1
    bot_name: Optional[str] = Field(None, max_length=64)         # setMyName
    description: Optional[str] = Field(None, max_length=512)      # setMyDescription
    short_description: Optional[str] = Field(None, max_length=120) # setMyShortDescription
    commands: Dict[Annotated[str, Field(max_length=32)], Annotated[str, Field(max_length=256)]] = Field(default_factory=dict, max_length=100)  # setMyCommands: {command: description}; Telegram limits
    welcome_message: Optional[str] = Field(None, max_length=1000)
    # photo — NOT configured from the admin panel (BotFather only)

class BrandingConfig(BaseModel):
    """The aggregating tenant branding model. Stored in tenants.config JSONB
    (additive-only, with config_version for migrating old tenants)."""
    config_version: int = 1
    widget: WidgetBranding = Field(default_factory=WidgetBranding)
    telegram: Optional[TelegramBranding] = None

# --- Licensing and tenant models ---
class LicensePayload(BaseModel):
    # license_id is canonically a UUID; in the signed token (JWT/PASETO) it is carried
    # as its string form (a claim), hence the type is str here. In the DB (licenses.id,
    # LicenseStatus/LicenseRevokeRequest/HeartbeatPayload) — UUID. See licensing_and_distribution.md §2.
    license_id: str
    client_id: str
    plan: LicensePlan = LicensePlan.STANDARD
    mode: LicenseMode = LicenseMode.ONLINE_SUBSCRIPTION
    issued_at: datetime
    expires_at: Optional[datetime] = None      # None for perpetual On-Premise licenses
    max_documents: int = 1000
    max_users: int = 10
    hardware_id: Optional[str] = None
    telemetry_opt_out: bool = True
    # The single registry = LicenseFeature; the default = DEFAULT_LICENSE_FEATURES (reconciled with LicenseIssueRequest and the DDL).
    features: List[LicenseFeature] = Field(default_factory=lambda: list(DEFAULT_LICENSE_FEATURES))

class TenantContext(BaseModel):
    tenant_id: UUID
    slug: str
    name: str
    is_active: bool = True
    storage_type: str = "s3"
    s3_bucket: Optional[str] = None
    s3_prefix: Optional[str] = None
    allowed_origins: List[str] = Field(default_factory=list) # A strict domain list without wildcard

class WebWidgetSessionRequest(BaseModel):
    slug: str
    auth_mode: Literal["anonymous", "oidc"] = "anonymous"   # Stage C: 'oidc' requires a valid id_token/assertion
    id_token: Optional[str] = None                          # OIDC id_token / SAML assertion (verified on the backend)

class WebWidgetSessionResponse(BaseModel):
    session_token: str
    tenant_id: UUID
    expires_in: int = 3600
    auth_mode: Literal["anonymous", "oidc"] = "anonymous"
    # For 'oidc': subject + groups are placed into the session_token (JWT) on the backend after IdP verification;
    # the resulting access_level is recomputed from the groups/user, not from the client's claim.

# --- User and context models ---
class UserContext(BaseModel):
    user_id: UUID                      # Mandatory ID from the users table (no uuid4 fallback)
    tenant_id: UUID                    # ID of the bound tenant
    channel_user_id: str
    channel: ChannelType = ChannelType.TELEGRAM
    access_level: AccessLevel = AccessLevel.PUBLIC
    group_ids: List[UUID] = Field(default_factory=list)   # group membership (Stage B, enterprise_acl); empty when the flag is off
    external_subject: Optional[str] = None                # the subject from the IdP (OIDC/SAML 'sub'), NULL for local/anonymous
    # IMPORTANT (invariant): access_level/group_ids are ALWAYS recomputed on the backend from the DB (users/user_groups)
    # and/or a verified IdP claim; they are NEVER taken from the request body or the client payload.
    # Backward compatibility: the str-enum parses old string literals ('telegram'/'web')
    # for a transition period. The legacy value 'fastapi' in the DB is migrated to 'rest_sse' (backfill),
    # since the str-enum will not parse it. New code must use ChannelType.

# --- Document and chunk models ---
class RawDocument(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    source_type: str = "s3"
    source_uri: str
    content_hash: str
    language: str = "ru"
    access_level: AccessLevel = AccessLevel.PUBLIC
    raw_content: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ParentChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    content: str
    token_count: int
    chunk_index: int
    access_level: AccessLevel = AccessLevel.PUBLIC   # denormalized from documents (cascade); defense-in-depth on a direct query of parent content (hydration)
    metadata: Dict[str, Any] = Field(default_factory=dict)

class ChildChunk(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    parent_id: UUID
    content: str
    token_count: int
    chunk_index: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    dense_embedding: Optional[List[float]] = None
    sparse_embedding: Optional[Dict[int, float]] = None
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_version: int = 1

# --- Search and generation models ---
class RetrievalCandidate(BaseModel):
    child_id: UUID
    parent_id: UUID
    document_id: UUID
    content: str
    dense_rank: Optional[int] = None
    sparse_rank: Optional[int] = None
    rrf_score: float
    rerank_score: Optional[float] = None

class GenerationCitation(BaseModel):
    citation_index: int                # The footnote index in the answer text [1], [2]
    chunk_id: UUID                     # The real chunk UUID in child_chunks
    document_title: str
    source_uri: str
    page: Optional[int] = None

class RAGResponse(BaseModel):
    answer: str
    citations: List[GenerationCitation] = Field(default_factory=list)
    confidence_passed: bool = True
    trace_id: Optional[str] = None

# --- Access control models: groups / collections / ACL (Stage B, behind the enterprise_acl flag) ---
# Correspond to the groups/collections/document_acl tables (see core/storage_schema.md §2.4).
class AclPermission(str, Enum):
    READ = "read"                              # the enum is designed for extension: 'write'/'manage'/...

class PrincipalType(str, Enum):
    USER = "user"
    GROUP = "group"

class Group(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    access_level: AccessLevel = AccessLevel.PUBLIC         # the group's base clearance level
    external_ref: Optional[str] = None                     # the group id in the IdP (OIDC/SAML/SCIM), NULL for local ones

class Collection(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    access_level: AccessLevel = AccessLevel.PUBLIC
    parent_id: Optional[UUID] = None                       # collection hierarchy (knowledge bases/folders)

class DocumentAclEntry(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    principal_type: PrincipalType
    principal_id: UUID                                     # users.id | groups.id (by principal_type)
    permission: AclPermission = AclPermission.READ
    granted_by: Optional[UUID] = None                     # users.id of the admin (audit)

class AccessDecision(BaseModel):
    """The result of computing a subject's effective access to resources.

    IMPORTANT: for RAG the main contract is NOT a bool, but a SET/PREDICATE of allowed resources,
    applied BEFORE top-k/RRF (otherwise a post-filter breaks ranking and RRF).
    - effective_access_level — the maximum level among user.access_level and their groups (primary filter).
    - allowed_document_ids — an explicit allowlist from the ACL (combined with the level filter by OR).
    - denied_document_ids — explicit denials (take priority; fail-closed on conflict).
    """
    effective_access_level: AccessLevel = AccessLevel.PUBLIC
    allowed_document_ids: Optional[List[UUID]] = None      # None => level-only filter (ACL disabled/empty)
    denied_document_ids: List[UUID] = Field(default_factory=list)

# --- Admin-panel models (see core/admin_panel.md) ---
class AdminConfig(BaseModel):
    """Runtime config for the single SPA build: mode differences without forks."""
    deployment_mode: DeploymentMode
    features: List[str] = Field(default_factory=list)   # 'tenant_management', 'license_activation', 'local_file_manager', ...
    branding: Dict[str, Any] = Field(default_factory=dict)
    api_version: str = "v1"

class DocumentUploadRequest(BaseModel):
    tenant_id: UUID
    file_name: str
    content_hash: str                                   # sha256, computed on the client for early deduplication
    size_bytes: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    relative_path: Optional[str] = None                 # path inside the uploaded folder (webkitdirectory)

class DocumentListItem(BaseModel):
    id: UUID
    source_uri: str
    file_name: str
    status: DocumentStatus
    access_level: AccessLevel = AccessLevel.PUBLIC
    size_bytes: Optional[int] = None
    parent_chunk_count: int = 0
    child_chunk_count: int = 0
    error_message: Optional[str] = None
    created_at: datetime
    updated_at: datetime

class ChunkView(BaseModel):
    """Embeddings are NOT returned by default: 1024 float x N chunks = megabytes of JSON.
    dense_embedding/sparse_embedding are filled only with include_vectors=true and only for a single chunk."""
    chunk_id: UUID
    parent_id: Optional[UUID] = None
    level: Literal["parent", "child"]
    content: str
    token_count: int
    chunk_index: int
    metadata: Dict[str, Any] = Field(default_factory=dict)
    dense_norm: Optional[float] = None
    has_sparse_vector: bool = False
    dense_embedding: Optional[List[float]] = None
    sparse_embedding: Optional[Dict[int, float]] = None

class LicenseActivationRequest(BaseModel):
    activation_code: Optional[str] = None               # 'RAG-ENT-XXXX-YYYY-ZZZZ'
    license_token: Optional[str] = None                 # PASETO v4.public / JWT (Ed25519)
    license_file_b64: Optional[str] = None              # the content of the uploaded .key file

class MachineFingerprint(BaseModel):
    hardware_id: str                                    # sha256(cpu_id + mac_addr)
    hostname: str
    collected_at: datetime

class LicenseStatus(BaseModel):
    """The source of truth for the license is only the backend. The frontend displays this object as is."""
    is_valid: bool
    plan: LicensePlan
    mode: LicenseMode
    issued_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    max_documents: int = 0
    max_users: int = 0
    used_documents: int = 0
    used_users: int = 0
    hardware_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    grace_period_expires_at: Optional[datetime] = None
    restricted_mode: bool = False
    restricted_reason: Optional[str] = None

class AuditLogEntry(BaseModel):
    id: UUID
    tenant_id: Optional[UUID] = None
    actor_user_id: UUID
    actor_email: str
    action: str                                         # 'document.upload', 'document.delete', 'license.activate', ...  (action type)
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime

class IngestQueueItem(BaseModel):
    document_id: UUID
    file_name: str
    status: DocumentStatus
    stage: Optional[str] = None                         # 'parsing' | 'splitting' | 'embedding' | 'upserting'  (worker stage)
    retry_count: int = 0
    error_message: Optional[str] = None
    updated_at: datetime

class DashboardStats(BaseModel):
    documents_total: int = 0
    documents_by_status: Dict[DocumentStatus, int] = Field(default_factory=dict)
    child_chunks_total: int = 0
    storage_bytes_used: int = 0
    queries_last_24h: int = 0
    p95_retrieval_latency_ms: Optional[float] = None

# --- Vendor Backoffice & License Server (see core/vendor_backoffice.md, core/license_server.md) ---
class CustomerStatus(str, Enum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CHURNED = "churned"

class Customer(BaseModel):
    """Legal entity/buyer. SEPARATE from a tenant (technical installation).
    Relationship 1 customer : N tenants (a cloud tenant + box installations)."""
    id: UUID
    legal_name: str
    display_name: Optional[str] = None
    inn: Optional[str] = None                            # INN / tax id
    contact_email: str
    contact_phone: Optional[str] = None
    contract_number: Optional[str] = None
    status: CustomerStatus = CustomerStatus.TRIAL
    created_at: datetime
    updated_at: datetime

class ActivationCodeStatus(str, Enum):
    ISSUED = "issued"
    REDEEMED = "redeemed"
    REVOKED = "revoked"
    EXPIRED = "expired"

class ActivationCode(BaseModel):
    """A code like 'RAG-ENT-XXXX-YYYY-ZZZZ', issued by the vendor per order."""
    code: str                                           # 'RAG-ENT-XXXX-YYYY-ZZZZ'
    customer_id: UUID
    order_id: Optional[UUID] = None
    plan: LicensePlan
    status: ActivationCodeStatus = ActivationCodeStatus.ISSUED
    redeemed_by_tenant_id: Optional[UUID] = None
    expires_at: Optional[datetime] = None
    created_at: datetime

class LicenseIssueRequest(BaseModel):
    """Vendor license ISSUANCE (as opposed to the self-service LicenseActivationRequest).
    Signing — only with the signing service's offline Ed25519 key, see core/license_server.md."""
    customer_id: UUID
    tenant_id: UUID
    plan: LicensePlan = LicensePlan.STANDARD
    mode: LicenseMode = LicenseMode.ONLINE_SUBSCRIPTION
    max_documents: int = 1000
    max_users: int = 10
    expires_at: Optional[datetime] = None               # None => a perpetual box license
    hardware_id: Optional[str] = None                   # binding for an airgap box license
    # The same canonical default as LicensePayload and the DDL licenses.features (desync removed).
    features: List[LicenseFeature] = Field(default_factory=lambda: list(DEFAULT_LICENSE_FEATURES))

class LicenseRevokeRequest(BaseModel):
    license_id: UUID
    reason: str

class BoxDeployment(BaseModel):
    """Registry of sold/deployed boxes (on-premise installations)."""
    id: UUID
    customer_id: UUID
    tenant_id: Optional[UUID] = None
    image_version: Optional[str] = None                 # version of the delivered closed image
    delivered_at: Optional[datetime] = None
    hardware_id: Optional[str] = None
    last_heartbeat_at: Optional[datetime] = None
    grace_period_expires_at: Optional[datetime] = None
    restricted_mode: bool = False
    telemetry_opt_out: bool = True                       # respected by the vendor; forwarding only when false
    observability_forward_enabled: bool = False

class ObservabilityConfig(BaseModel):
    """The cloud client's access to their Langfuse project (read-only Viewer).
    Stored in tenants.config as NON-SECRET: the password is never stored here."""
    url: str                                             # deep-link https://<host>/project/<project_id>
    username: str                                        # obs-<slug>@<domain>
    provisioned: bool = False                            # project/user/key created in Langfuse

class TelemetryConsent(BaseModel):
    """Consent to telemetry/forwarding (see core/licensing_and_distribution.md §3.3)."""
    telemetry_opt_out: bool = True
    observability_forward_enabled: bool = False

class TraceContext(BaseModel):
    """Mandatory attributes of every trace (see core/eval_and_ops.md §2.3)."""
    tenant_id: UUID
    environment: DeploymentMode = DeploymentMode.SAAS_CLOUD
    deployment_id: Optional[UUID] = None                 # for on_premise_box
    channel: ChannelType = ChannelType.WEB_WIDGET
    session_id: Optional[UUID] = None
    user_id: Optional[UUID] = None
    access_level: AccessLevel = AccessLevel.PUBLIC
    license_id: Optional[UUID] = None

class HeartbeatPayload(BaseModel):
    """Periodic installation heartbeat (see core/license_server.md §4.2).
    Only anonymized aggregates; texts/documents/PII are not transmitted."""
    license_id: UUID
    hardware_id: str
    version: str                                         # the box's image_version
    requests_last_period: int = 0
    uptime_seconds: int = 0
    p95_latency_ms: Optional[float] = None
    telemetry_opt_out: bool = True

class ImpersonationGrant(BaseModel):
    """Break-glass entry of a vendor employee into a foreign tenant. Every entry — to the audit."""
    backoffice_user_id: UUID
    target_tenant_id: UUID
    reason: str
    expires_at: datetime                                # a short-lived scope (minutes/hours)

# --- Application settings (pydantic-settings) ---
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Deployment & Licensing
    deployment_mode: DeploymentMode = DeploymentMode.SAAS_CLOUD
    license_key: Optional[str] = None
    license_public_key: Optional[str] = None
    license_server_url: Optional[str] = "https://license.your-rag-domain.com"

    # Vendor Backoffice & License Server (ONLY on the vendor side; NOT included in the Box artifact)
    # See core/vendor_backoffice.md, core/license_server.md.
    backoffice_enabled: bool = False                       # a separate vendor contour/--app; always False on the client
    backoffice_host: str = "127.0.0.1"                    # a separate host/port, do not mix with the tenant admin panel
    backoffice_port: int = 8090
    backoffice_rate_limit_per_minute: int = 60            # a separate (private) policy for backoffice
    backoffice_totp_required: bool = True                 # 2FA mandatory for all backoffice roles
    # The private Ed25519 license-signing key is NOT stored in the web/DB: only in the signing service / HSM / KMS.
    license_signing_service_url: Optional[str] = None     # the signing-service endpoint (sign/verify in different processes)
    license_crl_url: Optional[str] = None                 # the revocation list for an online revocation check

    # Supabase / Postgres
    supabase_url: Optional[str] = None
    supabase_service_role_key: Optional[str] = None
    # The administrative connection (Alembic migrations, ingest workers, backoffice).
    # A role with BYPASSRLS (service_role / postgres) is NEVER used for user-retrieval:
    # a superuser/BYPASSRLS bypasses RLS (see storage_schema.md §4).
    postgres_dsn: str = "postgresql://postgres:postgrespassword@localhost:5432/postgres"
    # The user-retrieval connection: the app_user role (NOBYPASSRLS, a member of authenticated) —
    # otherwise the tenant_isolation_* policies will not apply and will return 0 rows (default-deny).
    postgres_app_dsn: Optional[str] = None

    # S3 Object Storage (Cloudflare R2 / MinIO / Selectel / AWS)
    s3_endpoint_url: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_default_bucket: str = "enterprise-rag-documents"
    s3_region_name: str = "auto"

    # LLM & Embedder
    groq_api_key: Optional[str] = None
    gemini_api_key: Optional[str] = None
    ollama_base_url: str = "http://localhost:11434"
    embedding_model: str = "BAAI/bge-m3"
    embedding_batch_size: int = 32

    # RAG Parameters
    retrieval_top_candidates: int = 30
    rrf_k: int = 60
    rrf_alpha: float = 0.6
    rrf_beta: float = 0.4
    confidence_threshold: float = 0.35
    max_history_turns: int = 5
    generation_temperature: float = 0.1   # determinism/factuality of RAG; recommended <= 0.3
    generation_max_tokens: int = 1024     # the LLM answer token limit

    # Channels (see core/channels.md)
    telegram_bot_token: Optional[str] = None      # by default ONE shared bot; a separate bot per tenant — a paid option

    # Admin Panel & Session Auth (see core/admin_panel.md)
    admin_host: str = "127.0.0.1"                 # in Box mode the port is not published outward
    admin_port: int = 8080
    admin_static_dir: Optional[str] = "apps/admin/dist"   # serving the SPA as StaticFiles (a single container for Box)
    jwt_secret: Optional[str] = None              # HS256, per-install, >= 32 bytes; generated on the first start
    session_cookie_name: str = "rag_admin_session"
    session_ttl_seconds: int = 3600
    session_cookie_secure: bool = True            # HttpOnly + Secure + SameSite=Lax, NOT localStorage
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    csrf_enabled: bool = True
    totp_required_for_admin: bool = True
    admin_rate_limit_per_minute: int = 120        # the private policy (separate from the public widget)
    widget_rate_limit_per_minute: int = 20        # the public policy

    # Vector Store & Object Storage ports
    vector_store_provider: Literal["pgvector", "qdrant", "chroma"] = "pgvector"
    object_storage_provider: Literal["s3", "minio", "r2", "local"] = "s3"
    object_storage_local_root: str = "./data"

    # Observability (see core/eval_and_ops.md §2, core/admin_panel.md §11)
    langfuse_host: str = "https://cloud.langfuse.com"    # production: the vendor's self-hosted URL; Cloud Free Tier — dev only
    langfuse_public_key: Optional[str] = None
    langfuse_secret_key: Optional[str] = None
    langfuse_project_id: Optional[str] = None            # one project per tenant (model B)
    langfuse_disable_signup: bool = True                 # self-signup disabled — access is granted by the vendor
    trace_sample_rate: float = 1.0                       # the fraction of traces to record (cost control)
    trace_retention_days: int = 30
    trace_mask_fields: List[str] = Field(default_factory=lambda: ["password", "api_key", "token"])
    telemetry_opt_out: bool = True                       # 152-FZ/GDPR: telemetry is disabled by default
    observability_forward_enabled: bool = False          # CANONICAL flag name: forwarding of anonymized metrics/traces from Box to the vendor (opt-in). The former name telemetry_forward_enabled is a deprecated alias and MUST NOT be used.
    # ⚠️ TWO DIFFERENT CONCEPTS — do not confuse:
    #   1) LicenseFeature.OBSERVABILITY_FORWARD ("observability_forward") — an ENTITLEMENT (a license right),
    #      held in LicensePayload.features; it determines WHETHER forwarding to the vendor is allowed at all.
    #   2) Settings/BoxDeployment.observability_forward_enabled — the client's RUNTIME switch (opt-in).
    #   Forwarding is enabled ONLY when both are true: the entitlement in the license AND the runtime flag True (AND telemetry_opt_out=False).

    # Identity / end-user SSO (Stage C; gated by the sso_oidc license feature, see core/licensing_and_distribution.md)
    oidc_enabled: bool = False                           # enabling the authenticated widget mode
    oidc_issuer: Optional[str] = None                    # the IdP URL (OIDC discovery)
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_jwks_url: Optional[str] = None                  # for offline verification of the id_token signature
    oidc_groups_claim: str = "groups"                    # the name of the claim with the group list in id_token
```

---

## 2. Abstract core interfaces

```python
from abc import ABC, abstractmethod

# --- 2.0. Licensing & Multi-Tenancy Interfaces ---
class BaseLicenseValidator(ABC):
    @abstractmethod
    def validate_token(self, token: str) -> LicensePayload:
        """Offline validation of the Ed25519 digital signature and the token's expiry."""
        pass

    @abstractmethod
    async def verify_heartbeat(self, license_payload: LicensePayload) -> bool:
        """An online ping to the master license server to verify the absence of revocation (with a 72h grace period)."""
        pass

    @abstractmethod
    async def enforce_quota(self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]) -> None:
        """Checks license quotas (max_documents, max_users), raising QuotaExceededError on exceeding."""
        pass

class BaseLicenseServer(ABC):
    """The server half of licensing (vendor contour ONLY, see core/license_server.md).
    The private Ed25519 signing key lives in a separate signing service / HSM / KMS,
    not in this process and not in the backoffice DB."""

    @abstractmethod
    async def issue_license(self, request: LicenseIssueRequest) -> str:
        """Issues a signed Ed25519 license token (issuance). Returns license_token."""
        pass

    @abstractmethod
    async def revoke_license(self, request: LicenseRevokeRequest) -> None:
        """Revokes a license: marks revoked_at/reason + publishes to the CRL (revocation list)."""
        pass

    @abstractmethod
    async def register_heartbeat(self, hardware_id: str, license_id: UUID) -> None:
        """Receives a heartbeat from a box installation: updates last_heartbeat_at, computes grace/restricted."""
        pass

    @abstractmethod
    async def generate_activation_code(self, customer_id: UUID, plan: LicensePlan) -> ActivationCode:
        """Generates a code 'RAG-ENT-XXXX-YYYY-ZZZZ' per order/customer."""
        pass

class BaseTenantResolver(ABC):
    @abstractmethod
    async def resolve_tenant(self, identifier: str) -> TenantContext:
        """Resolves the tenant context by API key, slug or widget domain."""
        pass

    @abstractmethod
    async def enforce_quota(self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]) -> None:
        """Checks the tenant's current resource consumption against the set limits."""
        pass

# --- 2.0.2. Policy Decision Point (the single access-decision point; Stage B/D) ---
class BasePolicyEvaluator(ABC):
    """The single PDP (Policy Decision Point): computes a subject's effective access.
    Called BEFORE the search (Retriever/VectorStore), on parent-content hydration
    and in the citation resolver (citation re-check). Returns NOT a bool, but a predicate/set
    of allowed resources, to preserve the correctness of top-k/RRF (see AccessDecision).
    When the enterprise_acl flag is off, only user.access_level is considered (ACL/groups are ignored)."""

    @abstractmethod
    async def effective_access(self, user: UserContext) -> AccessDecision:
        """Effective right = f(user.access_level, group.access_level, ACL allow/deny).
        effective_access_level = max(user.access_level, max(group.access_level))."""
        pass

    @abstractmethod
    async def can_access_document(self, user: UserContext, document_id: UUID) -> bool:
        """A point-check (used for citation re-check/parent hydration). fail-closed."""
        pass

# --- 2.0.1. Storage Ports (swappable implementations) ---
class VectorStoreCapabilities(BaseModel):
    supports_sparse: bool = True
    supports_hybrid_rrf: bool = False        # True only where a native server implementation exists (PgVectorStore)
    max_dimensions: int = 1024

class BaseVectorStore(ABC):
    """The vector-store port. The contract is the separate search arms;
    RRF fusion is performed in the application layer (Retriever), not inside the store."""

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Capability flags of a specific implementation (pgvector / Qdrant / Chroma)."""
        pass

    @abstractmethod
    async def upsert_chunks(self, tenant_id: UUID, chunks: List[ChildChunk]) -> None:
        """Idempotent write/update of child chunks together with their vectors."""
        pass

    @abstractmethod
    async def search_dense(
        self, tenant_id: UUID, query_dense: List[float], k: int = 30, user_access_level: int = 0,
        allowed_document_ids: Optional[List[UUID]] = None
    ) -> List[RetrievalCandidate]:
        """The dense arm: Top-k by cosine distance with a tenant_id and access_level filter.
        With enterprise_acl, allowed_document_ids (from AccessDecision) is applied as an extra predicate
        BEFORE LIMIT k — not as a post-filter, so as not to break top-k/RRF."""
        pass

    @abstractmethod
    async def search_sparse(
        self, tenant_id: UUID, query_sparse: Dict[int, float], k: int = 30, user_access_level: int = 0,
        allowed_document_ids: Optional[List[UUID]] = None
    ) -> List[RetrievalCandidate]:
        """The lexical arm (sparsevec / BM25). If supports_sparse=False — returns an empty list.
        allowed_document_ids is applied the same way as in search_dense (a predicate before LIMIT)."""
        pass

    @abstractmethod
    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        """Cascade deletion of a document's vectors (for DELETE /admin/documents/{id})."""
        pass

class BaseObjectStorage(ABC):
    """The object-storage port (S3 / MinIO / R2 / local). All operations are strictly per-tenant."""

    @abstractmethod
    async def put(self, tenant_id: UUID, key: str, data: bytes, content_type: Optional[str] = None) -> str:
        """Writes an object under the prefix {tenant_id}/{key}. Returns a storage_ref (URI)."""
        pass

    @abstractmethod
    async def get(self, tenant_id: UUID, key: str) -> bytes:
        pass

    @abstractmethod
    async def delete(self, tenant_id: UUID, key: str) -> None:
        pass

    @abstractmethod
    async def list(self, tenant_id: UUID, prefix: str = "", limit: int = 1000) -> List[str]:
        pass

# --- 2.1. Embedder Interface ---
class BaseEmbedder(ABC):
    @abstractmethod
    async def embed_documents(
        self, texts: List[str]
    ) -> Tuple[List[List[float]], List[Dict[int, float]]]:
        """A single BGE-M3 pass: generation of dense (1024-dim) and sparse representations."""
        pass

    @abstractmethod
    async def embed_query(
        self, text: str
    ) -> Tuple[List[float], Dict[int, float]]:
        """Generation of the dense and sparse vector for a search query."""
        pass

# --- 2.2. Query Rewriter & Intent Router ---
class BaseQueryRewriter(ABC):
    @abstractmethod
    async def rewrite_query(self, query: str, history: List[Dict[str, str]]) -> str:
        """Query Condensation: transforming a follow-up question into a standalone query."""
        pass

class BaseIntentRouter(ABC):
    @abstractmethod
    async def route_intent(self, query: str) -> IntentType:
        """Intent routing: RAG search, Chit-Chat or Meta command."""
        pass

# --- 2.3. Retriever & Reranker Interface ---
class BaseRetriever(ABC):
    @abstractmethod
    async def retrieve(
        self, query: str, tenant_id: UUID, user_access_level: int = 0, top_k: int = 30,
        allowed_document_ids: Optional[List[UUID]] = None
    ) -> List[RetrievalCandidate]:
        """Hybrid Dense + Sparse search with RRF fusion and isolation by tenant_id.
        user_access_level and allowed_document_ids are taken from AccessDecision (PolicyEvaluator),
        computed on the backend; the client does not pass them."""
        pass

class Retriever(BaseRetriever):
    """The reference implementation: calls BaseVectorStore.search_dense() and .search_sparse(),
    then performs RRF fusion IN CODE (α=0.6, β=0.4, k=60).

    RRF is an application-layer contract, not SQL. The SQL function hybrid_search_rrf remains
    an internal optimization detail of PgVectorStore (called only when
    capabilities.supports_hybrid_rrf = True and identical in result to the code fusion).
    """

    def __init__(self, vector_store: BaseVectorStore, embedder: BaseEmbedder) -> None: ...

    @staticmethod
    def fuse_rrf(
        dense: List[RetrievalCandidate],
        sparse: List[RetrievalCandidate],
        k: int = 60,
        alpha: float = 0.6,
        beta: float = 0.4,
    ) -> List[RetrievalCandidate]:
        """RRF(d) = alpha/(k + rank_dense(d)) + beta/(k + rank_sparse(d)).
        A document absent from a list => rank infinity => the term's contribution is 0."""
        ...

class BaseReranker(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, candidates: List[RetrievalCandidate]
    ) -> List[RetrievalCandidate]:
        """Re-ranking of candidates (FlashRank) with rerank_score computation."""
        pass

class BaseConfidenceGate(ABC):
    @abstractmethod
    def evaluate(self, candidates: List[RetrievalCandidate], threshold: Optional[float] = None) -> bool:
        """Checks max(rerank_score) >= threshold. If threshold=None, settings.confidence_threshold is used."""
        pass

# --- 2.4. LLM Provider Interface ---
class BaseLLM(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        """Generation of full text or valid JSON.
        When temperature/max_tokens=None, settings.generation_temperature
        and settings.generation_max_tokens are used (a single source of values, no hardcode)."""
        pass

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming token generation (Token Stream).
        When temperature/max_tokens=None, settings.generation_temperature
        and settings.generation_max_tokens are used."""
        pass

# --- 2.5. Document Source Interface ---
class BaseDocumentSource(ABC):
    @abstractmethod
    async def fetch_documents(self) -> List[RawDocument]:
        """Loading documents from a source (local folder, S3, Confluence)."""
        pass

    @abstractmethod
    async def watch_for_changes(self) -> AsyncGenerator[RawDocument, None]:
        """A generator of file add/change events with debounce."""
        pass

# --- 2.6. Channel Interface (see core/channels.md) ---
class BaseChannel(ABC):
    """The delivery-channel port. The invariant is 'one input, two outputs':
    receive() normalizes native events into ChannelEvent (input),
    send_response()/stream_response() are the output. The retrieval core uses
    only ChannelEvent and capabilities flags at the channel boundary."""

    @property
    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """The adapter's capabilities (streaming/markdown/citations/buttons/...).
        Confirmed by the adapter's mandatory contract test."""
        pass

    @abstractmethod
    async def start(self) -> None:
        """Starts the channel's incoming-message handler."""
        pass

    @abstractmethod
    async def receive(self) -> AsyncGenerator[ChannelEvent, None]:
        """A generator of normalized incoming channel events (ChannelEvent)."""
        pass

    @abstractmethod
    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Sends the composed response to the user."""
        pass

    @abstractmethod
    async def stream_response(
        self, user_id: str, token_stream: AsyncGenerator[str, None], initial_message_id: Optional[str] = None
    ) -> RAGResponse:
        """Streaming response delivery with update throttling and assembly of the final RAGResponse."""
        pass

    async def request_cancellation(self, user_id: str) -> None:
        """An optional generation-cancellation hook. The core calls it but is not obliged to;
        adapters without supports_cancellation may leave it unoverridden (no-op)."""
        return None

# --- 2.5. Observability (Tracing Sink) ---
class BaseTraceSink(ABC):
    """The trace-writing port (implementation — Langfuse). Fire-and-forget writes:
    a sink failure does not block the RAG response (see core/eval_and_ops.md §2.5)."""

    @abstractmethod
    async def start_trace(self, ctx: TraceContext, name: str, input: Dict[str, Any]) -> str:
        """Opens a trace; returns a trace_id for correlation (messages.langfuse_trace_id)."""
        pass

    @abstractmethod
    async def log_span(self, trace_id: str, name: str, *, latency_ms: float, input: Dict[str, Any], output: Dict[str, Any], metadata: Dict[str, Any]) -> None:
        """Writes a span (intent/retrieval/rerank/llm_call) with mandatory attributes."""
        pass

    @abstractmethod
    async def log_score(self, trace_id: str, name: str, value: float) -> None:
        """Online feedback 👍/👎 (+1/-1) and LLM-as-a-Judge metrics."""
        pass

    async def flush(self) -> None:
        """An optional buffer-flush hook (best-effort, raises no exceptions)."""
        return None
```

---

## 3. Background tasks (Taskiq Tasks Contract)

```python
# src/tasks/ingestion_tasks.py
from taskiq import TaskiqDepends
from taskiq_aio_pika import AioPikaBroker  # Or PostgresBroker / InMemoryBroker

# Queue broker initialization
broker = AioPikaBroker()

@broker.task
async def process_document_task(
    tenant_id: UUID,
    storage_ref: str,                 # reference to the object in BaseObjectStorage ({tenant_id}/{key})
    content_hash: str,
    access_level: int = 0
) -> None:
    """
    1. Check the tenant's active quota (enforce_quota(tenant_id, 'documents')).
    2. Check for content_hash presence in the documents table for this tenant_id (idempotency).
    3. Read the file via BaseObjectStorage.get(tenant_id, key) and parse the structure (Docling / layout parser).
    4. Token-aware splitting into Parent and Child chunks with tenant_id and access_level set.
    5. Generate batches of BGE-M3 embeddings (batch_size=32).
    6. Transactional write (BaseVectorStore.upsert_chunks) and status transition to 'indexed'.
    7. Publish a progress event: NOTIFY ingest_events (for the admin SSE). In-memory pub/sub is forbidden.
    """
    pass
```

---

## 4. Admin session security contract (`fastapi-users`)

```python
# services/api/admin/auth.py
from fastapi_users.authentication import AuthenticationBackend, CookieTransport, JWTStrategy

# HTTP-only cookie: the JWT NEVER reaches the JS context (no localStorage / sessionStorage)
cookie_transport = CookieTransport(
    cookie_name=settings.session_cookie_name,
    cookie_max_age=settings.session_ttl_seconds,
    cookie_path="/api/v1",
    cookie_httponly=True,      # mandatory
    cookie_secure=True,        # mandatory (HTTPS / SSH tunnel)
    cookie_samesite="lax",     # mandatory
)

def get_jwt_strategy() -> JWTStrategy:
    # HS256 + a per-install secret (>= 32 bytes), generated on first start and rotated on deploy.
    # It is FORBIDDEN to use LICENSE_SIGNING_KEY here (Ed25519, the vendor's offline key):
    # a compromise of a client's instance would allow issuing licenses on the vendor's behalf.
    return JWTStrategy(secret=settings.jwt_secret, lifetime_seconds=settings.session_ttl_seconds)

admin_auth_backend = AuthenticationBackend(
    name="admin_cookie",
    transport=cookie_transport,
    get_strategy=get_jwt_strategy,
)
```

**Mandatory rules:**
- ⛔ `localStorage` / `sessionStorage` for tokens are **forbidden**; the SPA does not send `Authorization: Bearer`.
- CSRF protection (`X-CSRF-Token` / double-submit cookie) is mandatory for `POST`, `PUT`, `PATCH`, `DELETE`.
- TOTP 2FA is mandatory for the `AdminRole.ADMIN` role.
- Every mutation writes an `AuditLogEntry` (actor, IP, User-Agent) in the same transaction.
- See details in [`admin_panel.md`](./admin_panel.md) §4.
