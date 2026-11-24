"""Admin session security, authentication and RBAC (`packages/rag_core/admin/auth.py`).

Canonical specification - `core/admin_panel.md` §4, `core/modules_spec.md` §4.
- HTTP-only cookie transport (`CookieTransport`, HttpOnly + Secure + SameSite=Lax, Path=/api/v1).
- Session tokens signed with HS256 (`JWT_SECRET`, >= 32 bytes). Never use Ed25519 license key.
- CSRF double-submit protection (`X-CSRF-Token` header on unsafe methods).
- TOTP 2FA (RFC 6238) for AdminRole.ADMIN and optional for other roles.
- RBAC role hierarchy: admin >= operator >= viewer.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from typing import Any, cast
from uuid import UUID

from rag_core.contracts.enums import AdminRole
from rag_core.contracts.models import AdminUserRead


class AuthError(Exception):
    """Base authentication/authorization error."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class CSRFError(AuthError):
    """CSRF token mismatch or missing."""

    def __init__(self, message: str = "Invalid or missing CSRF token") -> None:
        super().__init__(message=message, status_code=403)


class RBACPermissionDeniedError(AuthError):
    """Insufficient privileges for operation."""

    def __init__(self, message: str = "Insufficient permissions for this operation") -> None:
        super().__init__(message=message, status_code=403)


# --- 1. Password Hashing (Argon2id with PBKDF2-HMAC-SHA256 fallback/compatibility) ---
def hash_password(password: str) -> str:
    """Hashes a password using Argon2id if available, otherwise PBKDF2-HMAC-SHA256 (600k iters)."""
    try:
        import argon2

        ph = argon2.PasswordHasher(
            time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=argon2.Type.ID
        )
        # NOTE: assign-then-return (not cast) so this type-checks in BOTH environments:
        # when argon2-cffi stubs are present `hash()` is typed `str` (a cast would be flagged
        # `redundant-cast`), and when argon2 is untyped it returns `Any` (returning it directly
        # would trip `warn-return-any`). A typed local variable satisfies strict mypy either way.
        hashed: str = ph.hash(password)
        return hashed
    except ImportError:
        salt = secrets.token_hex(16)
        iterations = 600_000
        derived = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt.encode("utf-8"),
            iterations,
        )
        b64_hash = base64.b64encode(derived).decode("ascii")
        return f"pbkdf2_sha256${iterations}${salt}${b64_hash}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifies a plain password against stored hash in constant time."""
    if hashed_password.startswith("$argon2id$") or hashed_password.startswith("$argon2i$"):
        try:
            import argon2

            ph = argon2.PasswordHasher()
            return cast(bool, ph.verify(hashed_password, plain_password))
        except Exception:
            return False

    parts = hashed_password.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
        return False
    try:
        iterations = int(parts[1])
        salt = parts[2]
        expected_hash = base64.b64decode(parts[3].encode("ascii"))
    except Exception:
        return False

    derived = hashlib.pbkdf2_hmac(
        "sha256",
        plain_password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    return hmac.compare_digest(derived, expected_hash)


# --- 2. TOTP 2FA (RFC 6238 implementation) ---
def generate_totp_secret() -> str:
    """Generates a random base32 TOTP secret (160-bit / 20 bytes)."""
    raw_bytes = secrets.token_bytes(20)
    return base64.b32encode(raw_bytes).decode("ascii").rstrip("=")


def get_totp_uri(secret: str, email: str, issuer: str = "Enterprise RAG") -> str:
    """Generates standard otpauth:// URI for QR code generation."""
    clean_secret = secret.strip().replace(" ", "").upper()
    return (
        f"otpauth://totp/{issuer}:{email}"
        f"?secret={clean_secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"
    )


def generate_totp_code(
    secret: str, for_time: float | None = None, time_step: int = 30, digits: int = 6
) -> str:
    """Generates current 6-digit TOTP code for the given secret."""
    now = time.time() if for_time is None else for_time
    time_counter = int(now // time_step)
    counter_bytes = struct.pack(">Q", time_counter)

    # Pad base32 secret to multiple of 8 chars
    clean_secret = secret.strip().replace(" ", "").upper()
    missing_padding = (8 - len(clean_secret) % 8) % 8
    padded_secret = clean_secret + ("=" * missing_padding)

    try:
        key_bytes = base64.b32decode(padded_secret, casefold=True)
    except Exception as exc:
        raise ValueError(f"Invalid base32 TOTP secret: {exc}") from exc

    h = hmac.new(key_bytes, counter_bytes, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code_int = struct.unpack(">I", h[offset : offset + 4])[0] & 0x7FFFFFFF
    code = str(code_int % (10**digits)).zfill(digits)
    return code


def verify_totp_code(
    secret: str,
    code: str,
    time_step: int = 30,
    digits: int = 6,
    valid_window: int = 1,
) -> bool:
    """Verifies a 6-digit TOTP code with time drift window (default +/- 1 step = 30s)."""
    clean_code = code.strip()
    if len(clean_code) != digits or not clean_code.isdigit():
        return False

    now = time.time()
    for step_offset in range(-valid_window, valid_window + 1):
        test_time = now + (step_offset * time_step)
        expected = generate_totp_code(
            secret, for_time=test_time, time_step=time_step, digits=digits
        )
        if hmac.compare_digest(clean_code, expected):
            return True
    return False


# --- 3. JWT Session Tokens (HS256 with per-install JWT_SECRET) ---
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def create_admin_session_jwt(
    user_id: UUID,
    email: str,
    role: AdminRole,
    tenant_id: UUID | None,
    secret: str,
    ttl_seconds: int = 3600,
    mfa_verified: bool = True,
) -> str:
    """Signs an HS256 JWT session token for Admin SPA CookieTransport.

    ``mfa_verified=False`` mints a *limited* session: it can only reach the auth self-service
    endpoints (``/auth/me``, ``/auth/2fa/*``, ``/auth/logout``) until the admin completes the
    mandatory TOTP enrollment, after which they must log in again to obtain a full session.
    """
    if len(secret) < 32:
        raise ValueError("JWT_SECRET must be at least 32 bytes")

    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "sub": str(user_id),
        "email": email,
        "role": str(role),
        "tenant_id": str(tenant_id) if tenant_id is not None else None,
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
    sig_b64 = _b64url_encode(sig)

    return f"{hdr_b64}.{payload_b64}.{sig_b64}"


def verify_admin_session_jwt(token: str, secret: str) -> dict[str, Any]:
    """Verifies and decodes HS256 JWT session token."""
    if len(secret) < 32:
        raise ValueError("JWT_SECRET must be at least 32 bytes")

    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("Invalid JWT token format", status_code=401)

    hdr_b64, payload_b64, sig_b64 = parts
    signing_input = f"{hdr_b64}.{payload_b64}".encode("ascii")

    expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    actual_sig = _b64url_decode(sig_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthError("Invalid session signature", status_code=401)

    try:
        payload_bytes = _b64url_decode(payload_b64)
        payload: dict[str, Any] = json.loads(payload_bytes.decode("utf-8"))
    except Exception as exc:
        raise AuthError(f"Malformed session payload: {exc}", status_code=401) from exc

    # Contour isolation (core/vendor_backoffice.md): a vendor backoffice session token
    # (ctx='backoffice') must NEVER be accepted by the tenant admin contour, even when both
    # contours share the same HS256 signing secret. Legacy admin tokens carry no ctx claim.
    if payload.get("ctx") == "backoffice":
        raise AuthError("Not a tenant admin session token", status_code=401)

    now = time.time()
    if "exp" in payload and now > payload["exp"]:
        raise AuthError("Session token expired", status_code=401)
    if "nbf" in payload and now < payload["nbf"]:
        raise AuthError("Session token not active yet", status_code=401)

    return payload


# --- 4. CSRF Protection ---
def generate_csrf_token() -> str:
    """Generates a cryptographically strong random CSRF token."""
    return secrets.token_urlsafe(32)


def verify_csrf_token(header_token: str | None, session_csrf_token: str | None) -> bool:
    """Validates double-submit / session CSRF token."""
    if not header_token or not session_csrf_token:
        return False
    return hmac.compare_digest(header_token, session_csrf_token)


# --- 5. RBAC Hierarchy ---
# Role hierarchy: admin (level 3) > operator (level 2) > viewer (level 1)
_ROLE_HIERARCHY: dict[AdminRole, int] = {
    AdminRole.VIEWER: 1,
    AdminRole.OPERATOR: 2,
    AdminRole.ADMIN: 3,
}


def check_role_permission(user_role: AdminRole | str, required_role: AdminRole) -> bool:
    """Checks whether the user's role meets the minimum required role."""
    try:
        current_enum = AdminRole(str(user_role))
    except ValueError:
        return False
    return _ROLE_HIERARCHY.get(current_enum, 0) >= _ROLE_HIERARCHY.get(required_role, 0)


def enforce_role(user: AdminUserRead, required_role: AdminRole) -> None:
    """Raises RBACPermissionDeniedError if user does not satisfy the required role."""
    if not user.is_active:
        raise RBACPermissionDeniedError("User account is inactive")
    if not check_role_permission(user.role, required_role):
        raise RBACPermissionDeniedError(
            f"Role '{user.role}' is insufficient. Required: '{required_role}'"
        )
