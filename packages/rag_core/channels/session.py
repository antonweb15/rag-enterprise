"""Web widget session handshake and origin validation (`core/channels.md` §5).

Handles:
- Strict Origin allowlist validation (without wildcard '*');
- data-api-url mode-dependent handling (ignored in saas_cloud, applied in on_premise_box);
- Short-lived JWT session token creation and verification (TTL=3600s);
- User context resolution with fail-closed anonymous access_level=0 (Public).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Literal
from uuid import UUID

from rag_core.contracts.enums import AccessLevel, ChannelType, DeploymentMode
from rag_core.contracts.models import UserContext

logger = logging.getLogger(__name__)

DEFAULT_SESSION_TTL_SECONDS = 3600


class SessionSecurityError(Exception):
    """Raised when origin or session validation fails."""


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def create_session_jwt(
    *,
    tenant_id: UUID,
    user_id: UUID,
    auth_mode: Literal["anonymous", "oidc"],
    secret: str,
    ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    access_level: AccessLevel = AccessLevel.PUBLIC,
    group_ids: list[UUID] | None = None,
    subject: str | None = None,
    now: float | None = None,
) -> str:
    """Signs a short-lived HS256 session JWT for the web widget."""
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + ttl_seconds

    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "tenant_id": str(tenant_id),
        "auth_mode": auth_mode,
        "access_level": int(access_level),
        "group_ids": [str(g) for g in (group_ids or [])],
        "iat": issued_at,
        "exp": expires_at,
    }
    if subject:
        payload["external_subject"] = subject

    header_bytes = json.dumps(header, separators=(",", ":")).encode("utf-8")
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    unsigned_token = f"{_base64url_encode(header_bytes)}.{_base64url_encode(payload_bytes)}"
    signature = hmac.new(
        secret.encode("utf-8"), unsigned_token.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{unsigned_token}.{_base64url_encode(signature)}"


def verify_session_jwt(
    token: str,
    secret: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Verifies HS256 signature and expiration of session JWT; returns decoded claims."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise SessionSecurityError("Invalid session token format")

    header_b64, payload_b64, signature_b64 = parts
    unsigned_token = f"{header_b64}.{payload_b64}"

    try:
        decoded_header_bytes = _base64url_decode(header_b64)
        raw_header = json.loads(decoded_header_bytes.decode("utf-8"))
        if not isinstance(raw_header, dict) or raw_header.get("alg") != "HS256":
            raise SessionSecurityError("Unsupported or invalid token algorithm")
    except SessionSecurityError:
        raise
    except Exception as exc:
        raise SessionSecurityError("Malformed session token header") from exc

    expected_sig = hmac.new(
        secret.encode("utf-8"), unsigned_token.encode("ascii"), hashlib.sha256
    ).digest()
    actual_sig = _base64url_decode(signature_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise SessionSecurityError("Invalid session token signature")

    try:
        decoded_bytes = _base64url_decode(payload_b64)
        raw_payload = json.loads(decoded_bytes.decode("utf-8"))
        if not isinstance(raw_payload, dict):
            raise ValueError("Payload must be a JSON object")
        payload: dict[str, Any] = raw_payload
    except Exception as exc:
        raise SessionSecurityError("Malformed session token payload") from exc

    current_time = int(now if now is not None else time.time())
    # Reject tokens issued in the future beyond 60s clock skew
    iat = payload.get("iat")
    if iat is not None and iat > current_time + 60:
        raise SessionSecurityError("Session token issued in the future")

    if payload.get("exp", 0) < current_time:
        raise SessionSecurityError("Session token has expired")

    return payload


def validate_origin(origin: str | None, allowed_origins: list[str]) -> bool:
    """Checks Origin against strict allowlist without wildcards."""
    if not origin:
        return False

    cleaned_origin = origin.strip().rstrip("/")
    for allowed in allowed_origins:
        cleaned_allowed = allowed.strip().rstrip("/")
        # Wildcards are strictly forbidden (core/channels.md §5.3)
        if "*" in cleaned_allowed:
            continue
        if cleaned_origin.lower() == cleaned_allowed.lower():
            return True
    return False


def resolve_api_url_for_mode(
    *,
    deployment_mode: DeploymentMode,
    configured_api_url: str,
    client_data_api_url: str | None,
    origin: str | None,
    allowed_origins: list[str],
) -> str:
    """Resolves data-api-url strictly according to deployment_mode (core/channels.md §5.2).

    - saas_cloud: data-api-url is IGNORED; logged as suspicious if passed.
    - on_premise_box: data-api-url is APPLIED after Origin allowlist check.
    """
    if deployment_mode == DeploymentMode.SAAS_CLOUD:
        if client_data_api_url and client_data_api_url.strip() != configured_api_url.strip():
            logger.warning(
                "Suspicious data-api-url override in SaaS Cloud mode ignored: %s",
                client_data_api_url,
            )
        return configured_api_url

    # On-Premise Box mode
    if client_data_api_url:
        if not validate_origin(origin, allowed_origins):
            logger.warning(
                "Origin %s rejected during data-api-url resolution in Box mode",
                origin,
            )
            raise SessionSecurityError(
                f"Origin '{origin}' is not allowed for data-api-url override in Box mode"
            )
        return client_data_api_url.strip()

    return configured_api_url


def build_widget_user_context(
    *,
    claims: dict[str, Any],
) -> UserContext:
    """Builds a verified UserContext from session token claims."""
    user_id = UUID(claims["sub"])
    tenant_id = UUID(claims["tenant_id"])
    access_level = AccessLevel(claims.get("access_level", AccessLevel.PUBLIC))
    group_ids = [UUID(g) for g in claims.get("group_ids", [])]
    external_subject = claims.get("external_subject")

    return UserContext(
        user_id=user_id,
        tenant_id=tenant_id,
        channel_user_id=str(user_id),
        channel=ChannelType.WEB_WIDGET,
        access_level=access_level,
        group_ids=group_ids,
        external_subject=external_subject,
    )
