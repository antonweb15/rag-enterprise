"""Pydantic V2 domain models of the core (`core/modules_spec.md` §1).

The full model set from the specification: licensing, tenants/users, documents/chunks,
search/generation, access control (groups/collections/ACL), the admin panel, the vendor
backoffice and observability.

All domain models forbid unknown fields (`extra="forbid"`): a payload with surplus keys is
rejected, which is a defense-in-depth measure at the contract boundary (no silent drift).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from rag_core.contracts.enums import (
    AccessLevel,
    AclPermission,
    ActivationCodeStatus,
    ChannelEventType,
    ChannelType,
    CustomerStatus,
    DeploymentMode,
    DocumentStatus,
    LicenseFeature,
    LicenseMode,
    LicensePlan,
    PrincipalType,
)


class CoreModel(BaseModel):
    """Shared base for all core domain models.

    `extra="forbid"` rejects surplus fields at the contract boundary (defense-in-depth, no
    silent drift). Enum members are kept as objects (not coerced to raw values) for type safety.
    """

    model_config = ConfigDict(extra="forbid")


# The canonical default feature set (reconciled between LicensePayload, LicenseIssueRequest
# and the DDL licenses.features DEFAULT). The Standard plan gets the base set.
# Delivery channels (WEB_WIDGET/TELEGRAM/REST_SSE/CLI) are NOT in the default and are granted
# separately on license issuance/upgrade: enabling a channel has an operational cost
# (a bot token, a widget config), and the right to a channel is an explicit vendor decision.
# It is an immutable tuple so the canonical default cannot be mutated in place by a caller;
# each model copies it into a fresh list via `default_factory`.
DEFAULT_LICENSE_FEATURES: tuple[LicenseFeature, ...] = (
    LicenseFeature.HYBRID_SEARCH,
    LicenseFeature.S3_CONNECTOR,
    LicenseFeature.FLASHRANK_RERANK,
)

# The canonical embedding model id, kept in one place (used by ChildChunk and Settings so the
# default never drifts between the contract and the runtime config).
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"


# --- Delivery channels (see core/channels.md) ---
class ChannelCapabilities(CoreModel):
    """Capabilities of a specific channel. Read ONLY at the channel boundary.

    The search business logic (retrieval) does NOT branch on them. The defaults are
    conservative: an absent/unknown capability does not break the render. Each adapter must
    have a contract test confirming the declared flags.
    """

    supports_streaming: bool = False
    supports_markdown: bool = False
    supports_citations: bool = False
    supports_feedback_buttons: bool = False
    supports_attachments: bool = False
    supports_reply_threading: bool = False
    supports_commands: bool = False
    supports_callback_payloads: bool = False
    supports_cancellation: bool = False
    max_message_length: int | None = None  # None = no limit / set by the adapter (Telegram = 4096)
    max_attachments_per_message: int = 0


class AttachmentRef(CoreModel):
    file_id: str  # identifier in the external channel or a storage_ref
    file_name: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None


class ChannelEvent(CoreModel):
    """A channel-neutral incoming event.

    Channel adapters must map native events into a ChannelEvent. The retrieval core accepts
    ONLY a ChannelEvent and knows nothing about the specific channel ('one input, two outputs').
    """

    event_type: ChannelEventType = ChannelEventType.TEXT_MESSAGE
    user_id: UUID
    tenant_id: UUID
    channel: ChannelType
    channel_user_id: str
    session_id: str | None = None
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)  # callback_data, command arguments
    attachments: list[AttachmentRef] = Field(default_factory=list)
    created_at: datetime


# --- Typed branding (see core/channels.md §7) ---
class WidgetBranding(CoreModel):
    """Widget white-label. 100% controlled from the admin panel.

    NOTE: welcome_message/disclaimer are rendered as TEXT (not raw HTML), a CSP is mandatory on
    the widget; logo_url - only https + a Content-Type allowlist.
    """

    config_version: int = 1
    title: str | None = Field(None, max_length=120)
    subtitle: str | None = Field(None, max_length=200)
    logo_url: str | None = Field(None, pattern=r"^https://")  # images only (Content-Type checked)
    primary_color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    welcome_message: str | None = Field(None, max_length=1000)
    disclaimer: str | None = Field(None, max_length=500)
    # <=5 questions, <=120 chars each
    suggested_questions: list[Annotated[str, Field(max_length=120)]] = Field(
        default_factory=list, max_length=5
    )


class TelegramBranding(CoreModel):
    """Partial Telegram white-label.

    The avatar is NOT configured from the admin panel (only @BotFather; setMyPhoto does not
    exist in the Bot API).
    """

    config_version: int = 1
    bot_name: str | None = Field(None, max_length=64)  # setMyName
    description: str | None = Field(None, max_length=512)  # setMyDescription
    short_description: str | None = Field(None, max_length=120)  # setMyShortDescription
    # setMyCommands: {command: description}; Telegram limits
    commands: dict[Annotated[str, Field(max_length=32)], Annotated[str, Field(max_length=256)]] = (
        Field(default_factory=dict, max_length=100)
    )
    welcome_message: str | None = Field(None, max_length=1000)
    # photo - NOT configured from the admin panel (BotFather only)


class BrandingConfig(CoreModel):
    """The aggregating tenant branding model.

    Stored in tenants.config JSONB (additive-only, with config_version for migrating old
    tenants).
    """

    config_version: int = 1
    widget: WidgetBranding = Field(default_factory=WidgetBranding)
    telegram: TelegramBranding | None = None


# --- Licensing and tenant models ---
class LicensePayload(CoreModel):
    # license_id is canonically a UUID; in the signed token (JWT/PASETO) it is carried
    # as its string form (a claim), hence the type is str here. In the DB (licenses.id,
    # LicenseStatus/LicenseRevokeRequest/HeartbeatPayload) - UUID.
    license_id: str
    client_id: str
    plan: LicensePlan = LicensePlan.STANDARD
    mode: LicenseMode = LicenseMode.ONLINE_SUBSCRIPTION
    issued_at: datetime
    expires_at: datetime | None = None  # None for perpetual On-Premise licenses
    max_documents: int = 1000
    max_users: int = 10
    hardware_id: str | None = None
    telemetry_opt_out: bool = True
    # The single registry = LicenseFeature; default = DEFAULT_LICENSE_FEATURES.
    features: list[LicenseFeature] = Field(default_factory=lambda: list(DEFAULT_LICENSE_FEATURES))


class TenantContext(CoreModel):
    tenant_id: UUID
    slug: str
    name: str
    is_active: bool = True
    storage_type: str = "s3"
    s3_bucket: str | None = None
    s3_prefix: str | None = None
    allowed_origins: list[str] = Field(default_factory=list)  # strict domain list, no wildcard


class WebWidgetSessionRequest(CoreModel):
    slug: str
    # Stage C: 'oidc' requires a valid id_token/assertion
    auth_mode: Literal["anonymous", "oidc"] = "anonymous"
    id_token: str | None = None  # OIDC id_token / SAML assertion (verified on the backend)


class WebWidgetSessionResponse(CoreModel):
    session_token: str
    tenant_id: UUID
    expires_in: int = 3600
    auth_mode: Literal["anonymous", "oidc"] = "anonymous"
    # For 'oidc': subject + groups are placed into the session_token (JWT) on the backend after
    # IdP verification; the resulting access_level is recomputed from the groups/user.


# --- User and context models ---
class UserContext(CoreModel):
    user_id: UUID  # Mandatory ID from the users table (no uuid4 fallback)
    tenant_id: UUID  # ID of the bound tenant
    channel_user_id: str
    channel: ChannelType = ChannelType.TELEGRAM
    access_level: AccessLevel = AccessLevel.PUBLIC
    group_ids: list[UUID] = Field(default_factory=list)  # group membership (enterprise_acl)
    external_subject: str | None = None  # IdP subject (OIDC/SAML 'sub'), NULL for local/anonymous
    # IMPORTANT (invariant): access_level/group_ids are ALWAYS recomputed on the backend from
    # the DB (users/user_groups) and/or a verified IdP claim; NEVER from the request body.


# --- Document and chunk models ---
class RawDocument(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    source_type: str = "s3"
    source_uri: str
    content_hash: str
    language: str = "ru"
    access_level: AccessLevel = AccessLevel.PUBLIC
    raw_content: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParentChunk(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    content: str
    token_count: int
    chunk_index: int
    # denormalized from documents (cascade); defense-in-depth on direct parent hydration
    access_level: AccessLevel = AccessLevel.PUBLIC
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChildChunk(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    parent_id: UUID
    content: str
    token_count: int
    chunk_index: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    dense_embedding: list[float] | None = None
    sparse_embedding: dict[int, float] | None = None
    embedding_model_id: str = DEFAULT_EMBEDDING_MODEL
    embedding_version: int = 1


# --- Search and generation models ---
class RetrievalCandidate(CoreModel):
    child_id: UUID
    parent_id: UUID
    document_id: UUID
    content: str
    # Per-arm raw signals (populated by the vector store BEFORE fusion): cosine similarity for
    # the dense arm, the sparse/BM25 score for the lexical arm. Optional - an arm that did not
    # return the candidate leaves its score as None.
    dense_score: float | None = None
    sparse_score: float | None = None
    dense_rank: int | None = None
    sparse_rank: int | None = None
    # Populated by Retriever.fuse_rrf; 0.0 before fusion (an arm candidate carries no meaningful
    # value until RRF runs), so the store does not need to fabricate a placeholder.
    rrf_score: float = 0.0
    rerank_score: float | None = None


class GenerationCitation(CoreModel):
    citation_index: int  # The footnote index in the answer text [1], [2]
    chunk_id: UUID  # The real chunk UUID in child_chunks
    document_title: str
    source_uri: str
    page: int | None = None


class RAGResponse(CoreModel):
    answer: str
    citations: list[GenerationCitation] = Field(default_factory=list)
    confidence_passed: bool = True
    trace_id: str | None = None


# --- Access control models: groups / collections / ACL (Stage B, behind enterprise_acl) ---
class Group(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    access_level: AccessLevel = AccessLevel.PUBLIC  # the group's base clearance level
    external_ref: str | None = None  # group id in the IdP (OIDC/SAML/SCIM), NULL for local


class Collection(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    name: str
    access_level: AccessLevel = AccessLevel.PUBLIC
    parent_id: UUID | None = None  # collection hierarchy (knowledge bases/folders)


class DocumentAclEntry(CoreModel):
    id: UUID = Field(default_factory=uuid4)
    tenant_id: UUID
    document_id: UUID
    principal_type: PrincipalType
    principal_id: UUID  # users.id | groups.id (by principal_type)
    permission: AclPermission = AclPermission.READ
    granted_by: UUID | None = None  # users.id of the admin (audit)


class AccessDecision(CoreModel):
    """The result of computing a subject's effective access to resources.

    IMPORTANT: for RAG the main contract is NOT a bool, but a SET/PREDICATE of allowed
    resources, applied BEFORE top-k/RRF (otherwise a post-filter breaks ranking and RRF).
    - effective_access_level - the maximum level among user.access_level and their groups.
    - allowed_document_ids - an explicit allowlist from the ACL (combined by OR with the level).
    - denied_document_ids - explicit denials (take priority; fail-closed on conflict).
    """

    effective_access_level: AccessLevel = AccessLevel.PUBLIC
    allowed_document_ids: list[UUID] | None = None  # None => level-only filter (ACL disabled)
    denied_document_ids: list[UUID] = Field(default_factory=list)


# --- Admin-panel models (see core/admin_panel.md) ---
class AdminConfig(CoreModel):
    """Runtime config for the single SPA build: mode differences without forks."""

    deployment_mode: DeploymentMode
    features: list[str] = Field(default_factory=list)  # 'tenant_management', 'license_activation'
    branding: dict[str, Any] = Field(default_factory=dict)
    api_version: str = "v1"


class DocumentUploadRequest(CoreModel):
    tenant_id: UUID
    file_name: str
    content_hash: str  # sha256, computed on the client for early deduplication
    size_bytes: int
    access_level: AccessLevel = AccessLevel.PUBLIC
    relative_path: str | None = None  # path inside the uploaded folder (webkitdirectory)


class DocumentListItem(CoreModel):
    id: UUID
    source_uri: str
    file_name: str
    status: DocumentStatus
    access_level: AccessLevel = AccessLevel.PUBLIC
    size_bytes: int | None = None
    parent_chunk_count: int = 0
    child_chunk_count: int = 0
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ChunkView(CoreModel):
    """Embeddings are NOT returned by default: 1024 float x N chunks = megabytes of JSON.

    dense_embedding/sparse_embedding are filled only with include_vectors=true and only for a
    single chunk.
    """

    chunk_id: UUID
    parent_id: UUID | None = None
    level: Literal["parent", "child"]
    content: str
    token_count: int
    chunk_index: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    dense_norm: float | None = None
    has_sparse_vector: bool = False
    dense_embedding: list[float] | None = None
    sparse_embedding: dict[int, float] | None = None


class LicenseActivationRequest(CoreModel):
    activation_code: str | None = None  # 'RAG-ENT-XXXX-YYYY-ZZZZ'
    license_token: str | None = None  # PASETO v4.public / JWT (Ed25519)
    license_file_b64: str | None = None  # the content of the uploaded .key file


class MachineFingerprint(CoreModel):
    hardware_id: str  # sha256(cpu_id + mac_addr)
    hostname: str
    collected_at: datetime


class LicenseStatus(CoreModel):
    """The source of truth for the license is only the backend. The frontend displays it as is."""

    is_valid: bool
    plan: LicensePlan
    mode: LicenseMode
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    max_documents: int = 0
    max_users: int = 0
    used_documents: int = 0
    used_users: int = 0
    hardware_id: str | None = None
    last_heartbeat_at: datetime | None = None
    grace_period_expires_at: datetime | None = None
    restricted_mode: bool = False
    restricted_reason: str | None = None


class AuditLogEntry(CoreModel):
    id: UUID
    tenant_id: UUID | None = None
    actor_user_id: UUID
    actor_email: str
    action: str  # 'document.upload', 'document.delete', 'license.activate', ... (action type)
    object_type: str | None = None
    object_id: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class IngestQueueItem(CoreModel):
    document_id: UUID
    file_name: str
    status: DocumentStatus
    stage: str | None = None  # 'parsing' | 'splitting' | 'embedding' | 'upserting'
    retry_count: int = 0
    error_message: str | None = None
    updated_at: datetime


class DashboardStats(CoreModel):
    documents_total: int = 0
    documents_by_status: dict[DocumentStatus, int] = Field(default_factory=dict)
    child_chunks_total: int = 0
    storage_bytes_used: int = 0
    queries_last_24h: int = 0
    p95_retrieval_latency_ms: float | None = None


# --- Vendor Backoffice & License Server (see core/vendor_backoffice.md, core/license_server.md) ---
class Customer(CoreModel):
    """Legal entity/buyer. SEPARATE from a tenant (technical installation).

    Relationship 1 customer : N tenants (a cloud tenant + box installations).
    """

    id: UUID
    legal_name: str
    display_name: str | None = None
    inn: str | None = None  # INN / tax id
    contact_email: str
    contact_phone: str | None = None
    contract_number: str | None = None
    status: CustomerStatus = CustomerStatus.TRIAL
    created_at: datetime
    updated_at: datetime


class ActivationCode(CoreModel):
    """A code like 'RAG-ENT-XXXX-YYYY-ZZZZ', issued by the vendor per order."""

    code: str  # 'RAG-ENT-XXXX-YYYY-ZZZZ'
    customer_id: UUID
    order_id: UUID | None = None
    plan: LicensePlan
    status: ActivationCodeStatus = ActivationCodeStatus.ISSUED
    redeemed_by_tenant_id: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime


class LicenseIssueRequest(CoreModel):
    """Vendor license ISSUANCE (as opposed to the self-service LicenseActivationRequest).

    Signing - only with the signing service's offline Ed25519 key, see core/license_server.md.
    """

    customer_id: UUID
    tenant_id: UUID
    plan: LicensePlan = LicensePlan.STANDARD
    mode: LicenseMode = LicenseMode.ONLINE_SUBSCRIPTION
    max_documents: int = 1000
    max_users: int = 10
    expires_at: datetime | None = None  # None => a perpetual box license
    hardware_id: str | None = None  # binding for an airgap box license
    # The same canonical default as LicensePayload and the DDL licenses.features.
    features: list[LicenseFeature] = Field(default_factory=lambda: list(DEFAULT_LICENSE_FEATURES))


class LicenseRevokeRequest(CoreModel):
    license_id: UUID
    reason: str


class BoxDeployment(CoreModel):
    """Registry of sold/deployed boxes (on-premise installations)."""

    id: UUID
    customer_id: UUID
    tenant_id: UUID | None = None
    image_version: str | None = None  # version of the delivered closed image
    delivered_at: datetime | None = None
    hardware_id: str | None = None
    last_heartbeat_at: datetime | None = None
    grace_period_expires_at: datetime | None = None
    restricted_mode: bool = False
    telemetry_opt_out: bool = True  # respected by the vendor; forwarding only when false
    observability_forward_enabled: bool = False


class ObservabilityConfig(CoreModel):
    """The cloud client's access to their Langfuse project (read-only Viewer).

    Stored in tenants.config as NON-SECRET: the password is never stored here.
    """

    url: str  # deep-link https://<host>/project/<project_id>
    username: str  # obs-<slug>@<domain>
    provisioned: bool = False  # project/user/key created in Langfuse


class TelemetryConsent(CoreModel):
    """Consent to telemetry/forwarding (see core/licensing_and_distribution.md §3.3)."""

    telemetry_opt_out: bool = True
    observability_forward_enabled: bool = False


class TraceContext(CoreModel):
    """Mandatory attributes of every trace (see core/eval_and_ops.md §2.3)."""

    tenant_id: UUID
    environment: DeploymentMode = DeploymentMode.SAAS_CLOUD
    deployment_id: UUID | None = None  # for on_premise_box
    channel: ChannelType = ChannelType.WEB_WIDGET
    session_id: UUID | None = None
    user_id: UUID | None = None
    access_level: AccessLevel = AccessLevel.PUBLIC
    license_id: UUID | None = None


class HeartbeatPayload(CoreModel):
    """Periodic installation heartbeat (see core/license_server.md §4.2).

    Only anonymized aggregates; texts/documents/PII are not transmitted.
    """

    license_id: UUID
    hardware_id: str
    version: str  # the box's image_version
    requests_last_period: int = 0
    uptime_seconds: int = 0
    p95_latency_ms: float | None = None
    telemetry_opt_out: bool = True


class ImpersonationGrant(CoreModel):
    """Break-glass entry of a vendor employee into a foreign tenant. Every entry - to the audit."""

    backoffice_user_id: UUID
    target_tenant_id: UUID
    reason: str
    expires_at: datetime  # a short-lived scope (minutes/hours)
