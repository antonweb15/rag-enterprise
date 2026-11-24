"""Backoffice session security & RBAC (`core/vendor_backoffice.md` §3, §7).

The vendor contour has its OWN user model (``backoffice_users`` with :class:`BackofficeRole`),
independent of the tenant ``AdminRole``. Access is capability-based: each role maps to the set
of capabilities from the RBAC matrix (``core/vendor_backoffice.md`` §3). Password hashing, TOTP
2FA and CSRF primitives are reused from the already-audited ``rag_core.admin.auth`` module (a
single implementation, no divergence); only the session-JWT role claim and the RBAC matrix are
backoffice-specific.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from enum import StrEnum
from typing import Any
from uuid import UUID

from rag_core.admin.auth import AuthError, RBACPermissionDeniedError, _b64url_decode, _b64url_encode
from rag_core.contracts.enums import BackofficeRole


class BackofficeCapability(StrEnum):
    """Discrete, auditable vendor capabilities (the columns of the §3 RBAC matrix)."""

    DASHBOARD_VIEW = "dashboard:view"
    CUSTOMER_VIEW = "customer:view"
    CUSTOMER_WRITE = "customer:write"
    ORDER_VIEW = "order:view"
    ORDER_WRITE = "order:write"
    CODE_VIEW = "code:view"
    CODE_GENERATE = "code:generate"
    LICENSE_VIEW = "license:view"
    LICENSE_ISSUE = "license:issue"
    LICENSE_REVOKE = "license:revoke"
    LICENSE_RENEW = "license:renew"
    LICENSE_UPGRADE = "license:upgrade"
    BOX_VIEW = "box:view"
    BOX_WRITE = "box:write"
    IMPERSONATE = "impersonate"
    AUDIT_VIEW = "audit:view"
    USER_MANAGE = "user:manage"
    # SaaS tenant lifecycle (Stage N): provisioning / offboarding / backup-restore.
    TENANT_PROVISION = "tenant:provision"
    TENANT_OFFBOARD = "tenant:offboard"
    TENANT_BACKUP = "tenant:backup"


# The capability matrix from core/vendor_backoffice.md §3. ``superadmin`` holds every capability.
_SUPPORT_CAPS: frozenset[BackofficeCapability] = frozenset(
    {
        BackofficeCapability.DASHBOARD_VIEW,
        BackofficeCapability.CUSTOMER_VIEW,
        BackofficeCapability.ORDER_VIEW,
        BackofficeCapability.CODE_VIEW,
        BackofficeCapability.LICENSE_VIEW,
        BackofficeCapability.BOX_VIEW,
        BackofficeCapability.IMPERSONATE,  # break-glass, always audited
        BackofficeCapability.AUDIT_VIEW,
    }
)
_FINANCE_CAPS: frozenset[BackofficeCapability] = frozenset(
    {
        BackofficeCapability.DASHBOARD_VIEW,
        BackofficeCapability.CUSTOMER_VIEW,
        BackofficeCapability.ORDER_VIEW,
        BackofficeCapability.ORDER_WRITE,
        BackofficeCapability.LICENSE_VIEW,
        BackofficeCapability.LICENSE_RENEW,
        BackofficeCapability.LICENSE_UPGRADE,
        BackofficeCapability.BOX_VIEW,
    }
)
_SALES_CAPS: frozenset[BackofficeCapability] = frozenset(
    {
        BackofficeCapability.DASHBOARD_VIEW,
        BackofficeCapability.CUSTOMER_VIEW,
        BackofficeCapability.CUSTOMER_WRITE,
        BackofficeCapability.ORDER_VIEW,
        BackofficeCapability.ORDER_WRITE,
        BackofficeCapability.CODE_VIEW,
        BackofficeCapability.CODE_GENERATE,
        BackofficeCapability.BOX_VIEW,
    }
)
_ALL_CAPS: frozenset[BackofficeCapability] = frozenset(BackofficeCapability)

_ROLE_CAPABILITIES: dict[BackofficeRole, frozenset[BackofficeCapability]] = {
    BackofficeRole.SUPERADMIN: _ALL_CAPS,
    BackofficeRole.SUPPORT: _SUPPORT_CAPS,
    BackofficeRole.FINANCE: _FINANCE_CAPS,
    BackofficeRole.SALES: _SALES_CAPS,
}


def role_capabilities(role: BackofficeRole | str) -> frozenset[BackofficeCapability]:
    """The capability set granted to a backoffice role (empty for an unknown role)."""
    try:
        role_enum = BackofficeRole(str(role))
    except ValueError:
        return frozenset()
    return _ROLE_CAPABILITIES.get(role_enum, frozenset())


def check_backoffice_capability(
    role: BackofficeRole | str, capability: BackofficeCapability
) -> bool:
    """True iff ``role`` is granted ``capability`` by the §3 matrix."""
    return capability in role_capabilities(role)


def enforce_backoffice_capability(
    role: BackofficeRole | str, capability: BackofficeCapability, *, is_active: bool = True
) -> None:
    """Raises :class:`RBACPermissionDeniedError` if the role lacks the capability or is inactive."""
    if not is_active:
        raise RBACPermissionDeniedError("Backoffice account is inactive")
    if not check_backoffice_capability(role, capability):
        raise RBACPermissionDeniedError(f"Backoffice role '{role}' lacks capability '{capability}'")


# --- Session JWT (HS256, separate claim namespace from the tenant admin session) ---
def create_backoffice_session_jwt(
    user_id: UUID,
    email: str,
    role: BackofficeRole,
    secret: str,
    ttl_seconds: int = 3600,
    mfa_verified: bool = True,
) -> str:
    """Signs an HS256 JWT for the backoffice CookieTransport (``ctx='backoffice'`` claim).

    ``mfa_verified=False`` mints a limited session usable only for the 2FA-enrollment endpoints
    until TOTP (mandatory for all backoffice roles) is set up.
    """
    if len(secret) < 32:
        raise ValueError("JWT secret must be at least 32 bytes")
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": str(role),
        "ctx": "backoffice",
        "mfa": mfa_verified,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
        "jti": secrets.token_hex(16),
    }
    hdr_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{hdr_b64}.{payload_b64}".encode("ascii")
    sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    return f"{hdr_b64}.{payload_b64}.{_b64url_encode(sig)}"


def verify_backoffice_session_jwt(token: str, secret: str) -> dict[str, Any]:
    """Verifies and decodes a backoffice HS256 JWT, enforcing the ``ctx='backoffice'`` claim.

    The context check is a contour-isolation guard: a tenant admin session token must NEVER be
    accepted by the backoffice (and vice versa), even if both used the same signing secret.
    """
    if len(secret) < 32:
        raise ValueError("JWT secret must be at least 32 bytes")
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("Invalid JWT token format", status_code=401)
    hdr_b64, payload_b64, sig_b64 = parts
    signing_input = f"{hdr_b64}.{payload_b64}".encode("ascii")
    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_sig, _b64url_decode(sig_b64)):
        raise AuthError("Invalid session signature", status_code=401)
    try:
        payload: dict[str, Any] = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception as exc:
        raise AuthError(f"Malformed session payload: {exc}", status_code=401) from exc
    if payload.get("ctx") != "backoffice":
        raise AuthError("Not a backoffice session token", status_code=401)
    now = time.time()
    if "exp" in payload and now > payload["exp"]:
        raise AuthError("Session token expired", status_code=401)
    if "nbf" in payload and now < payload["nbf"]:
        raise AuthError("Session token not active yet", status_code=401)
    return payload
