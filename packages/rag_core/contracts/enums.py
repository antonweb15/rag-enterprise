"""Canonical enumerations of the core (`core/modules_spec.md` §1).

These enums are the single source of truth shared across the Pydantic models, the DDL
(`core/storage_schema.md`) and the runtime configuration. String enums parse their string
literals for backward compatibility with persisted values.
"""

from __future__ import annotations

from enum import Enum, StrEnum


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"


class AccessLevel(int, Enum):
    PUBLIC = 0
    INTERNAL = 1
    CONFIDENTIAL = 2
    STRICT = 3


class IntentType(StrEnum):
    RAG_SEARCH = "rag_search"
    CHIT_CHAT = "chit_chat"
    META_COMMAND = "meta_command"


class AdminRole(StrEnum):
    ADMIN = "admin"  # full access: documents, license, users
    OPERATOR = "operator"  # upload and reindex without delete
    VIEWER = "viewer"  # read-only lists, chunks and the dashboard


class BackofficeRole(StrEnum):
    """Roles of the vendor contour (backoffice), SEPARATE from the tenant admin panel.

    These roles are NOT part of AdminRole and are not granted to tenant users;
    they are held by vendor staff. See core/vendor_backoffice.md.
    """

    SUPERADMIN = "superadmin"  # full cross-tenant access, management of backoffice users
    SUPPORT = "support"  # impersonation / "log in as" a tenant (break-glass); always audited
    FINANCE = "finance"  # billing, orders, invoices, renewals (no access to tenant data)
    SALES = "sales"  # customers, orders, activation codes (no access to tenant data)


class DeploymentMode(StrEnum):
    SAAS_CLOUD = "saas_cloud"
    ON_PREMISE_BOX = "on_premise_box"


class LicensePlan(StrEnum):
    """The canonical set of license plans.

    The single source of truth for the DDL (licenses.plan CHECK) and all Pydantic models;
    the default is STANDARD.
    """

    STANDARD = "standard"
    PRO = "pro"
    ENTERPRISE_BOX = "enterprise_box"


class LicenseMode(StrEnum):
    """License mode: online subscription (heartbeat) vs air-gapped enterprise-box.

    Air-gap uses offline validation + grace period. Persisted in licenses.mode.
    """

    ONLINE_SUBSCRIPTION = "online_subscription"
    AIRGAP_ENTERPRISE = "airgap_enterprise"


class LicenseFeature(StrEnum):
    """The SINGLE canonical registry of license feature flags (entitlements).

    The source of truth for LicensePayload/LicenseIssueRequest.features and for the CHECK
    constraint licenses.features (storage_schema.md). Any value outside this enum in features
    is forbidden (the DDL rejects it via '<@').
      - Delivery channels: WEB_WIDGET/TELEGRAM/REST_SSE/CLI.
      - Enterprise capabilities: ENTERPRISE_ACL/COLLECTIONS/SSO_OIDC.
      - Base ones: HYBRID_SEARCH/S3_CONNECTOR/FLASHRANK_RERANK.
      - OBSERVABILITY_FORWARD - the license ENTITLEMENT for forwarding anonymized traces to
        the vendor. Do NOT confuse with the runtime switch
        Settings/BoxDeployment.observability_forward_enabled.
    """

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


class ChannelType(StrEnum):
    """Delivery channels (see core/channels.md)."""

    WEB_WIDGET = "web"  # the primary end-user interface (Shadow DOM, white-label)
    TELEGRAM = "telegram"  # a secondary delivery channel
    REST_SSE = "rest_sse"  # headless REST / SSE for custom integrations
    CLI = "cli"  # typer console (air-gapped Box)


class ChannelEventType(StrEnum):
    TEXT_MESSAGE = "text_message"
    COMMAND = "command"  # /start, /help, /activate
    CALLBACK = "callback"  # 👍/👎 and other callback buttons
    ATTACHMENT = "attachment"
    EDIT = "edit"
    DELETE = "delete"


class AclPermission(StrEnum):
    READ = "read"  # the enum is designed for extension: 'write'/'manage'/...


class PrincipalType(StrEnum):
    USER = "user"
    GROUP = "group"


class CustomerStatus(StrEnum):
    TRIAL = "trial"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CHURNED = "churned"


class ActivationCodeStatus(StrEnum):
    ISSUED = "issued"
    REDEEMED = "redeemed"
    REVOKED = "revoked"
    EXPIRED = "expired"
