"""Ed25519 license verification and issuing helper (`rag_core.admin.license_crypto`).

Implements RFC 8032 Ed25519 digital signature verification and signing for license tokens
(PASETO v4.public and JWT EdDSA formats) according to `core/licensing_and_distribution.md` §3
and `core/license_server.md`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

from rag_core.contracts.enums import LicenseFeature, LicenseMode, LicensePlan
from rag_core.contracts.models import LicensePayload
from rag_core.licensing.errors import InvalidLicenseError


class LicenseValidationError(InvalidLicenseError):
    """Raised when license token signature, expiry or hardware binding is invalid.

    Subclasses :class:`rag_core.licensing.errors.InvalidLicenseError` so the two licensing stacks
    (the Stage I admin codec and the Stage K ``rag_core.licensing`` validator) share ONE error
    family: a consumer catching ``LicenseError``/``InvalidLicenseError`` also catches this.
    """


# --- Pure-Python RFC 8032 Ed25519 Implementation ---
# Curve25519 constants
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)

# Base point By = 4/5 mod p
_By = 4 * pow(5, _P - 2, _P) % _P
_Bx = (_By**2 - 1) * pow(_D * _By**2 + 1, _P - 2, _P) % _P
_Bx = pow(_Bx, (_P + 3) // 8, _P)
if (_Bx**2 - (_By**2 - 1) * pow(_D * _By**2 + 1, _P - 2, _P)) % _P != 0:
    _Bx = (_Bx * _I) % _P
if _Bx % 2 != 0:
    _Bx = _P - _Bx
_B = (_Bx, _By)


def _point_add(P: tuple[int, int], Q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = P
    x2, y2 = Q
    denom = (_D * x1 * x2 * y1 * y2) % _P
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + denom, _P - 2, _P)) % _P
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - denom, _P - 2, _P)) % _P
    return (x3, y3)


def _point_mul(s: int, P: tuple[int, int]) -> tuple[int, int]:
    Q = (0, 1)
    P_cur = P
    while s > 0:
        if s & 1:
            Q = _point_add(Q, P_cur)
        P_cur = _point_add(P_cur, P_cur)
        s >>= 1
    return Q


def _encode_point(P: tuple[int, int]) -> bytes:
    x, y = P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _recover_x(y: int, sign: int) -> int:
    if y >= _P:
        raise ValueError("y coordinate >= p")
    x2 = ((y**2 - 1) * pow(_D * y**2 + 1, _P - 2, _P)) % _P
    if x2 == 0:
        if sign != 0:
            raise ValueError("Zero x coordinate with non-zero sign")
        return 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x**2 - x2) % _P != 0:
        x = (x * _I) % _P
    if (x**2 - x2) % _P != 0:
        raise ValueError("No quadratic residue for x")
    if (x & 1) != sign:
        x = _P - x
    return x


def _decode_point(b: bytes) -> tuple[int, int]:
    if len(b) != 32:
        raise ValueError("Point bytes length must be 32")
    y_int = int.from_bytes(b, "little")
    sign = (y_int >> 255) & 1
    y = y_int & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return (x, y)


def ed25519_generate_keypair() -> tuple[bytes, bytes]:
    """Generates a random (private_seed, public_key) pair."""
    seed = secrets.token_bytes(32)
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    A = _point_mul(a, _B)
    public_key = _encode_point(A)
    return seed, public_key


def ed25519_sign(message: bytes, private_seed: bytes) -> bytes:
    """Signs message bytes with Ed25519 private seed (RFC 8032)."""
    h = hashlib.sha512(private_seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    A = _point_mul(a, _B)
    public_key = _encode_point(A)

    r_digest = hashlib.sha512(h[32:] + message).digest()
    r = int.from_bytes(r_digest, "little") % _L
    R = _point_mul(r, _B)
    R_bytes = _encode_point(R)

    k_digest = hashlib.sha512(R_bytes + public_key + message).digest()
    k = int.from_bytes(k_digest, "little") % _L

    S = (r + k * a) % _L
    S_bytes = S.to_bytes(32, "little")
    return R_bytes + S_bytes


def ed25519_verify(message: bytes, signature: bytes, public_key: bytes) -> bool:
    """Verifies Ed25519 signature (RFC 8032)."""
    if len(signature) != 64 or len(public_key) != 32:
        return False
    try:
        A = _decode_point(public_key)
        R_bytes = signature[:32]
        S_bytes = signature[32:]
        S = int.from_bytes(S_bytes, "little")
        if S >= _L:
            return False

        R = _decode_point(R_bytes)
        k_digest = hashlib.sha512(R_bytes + public_key + message).digest()
        k = int.from_bytes(k_digest, "little") % _L

        # Check: [S]B == R + [k]A
        SB = _point_mul(S, _B)
        kA = _point_mul(k, A)
        R_kA = _point_add(R, kA)

        return _encode_point(SB) == _encode_point(R_kA)
    except Exception:
        return False


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    padded = s + "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def create_signed_license_token(
    payload: LicensePayload | dict[str, Any],
    private_seed: bytes,
    format_type: str = "paseto",
) -> str:
    """Creates a signed license token (PASETO v4.public or JWT EdDSA)."""
    if isinstance(payload, LicensePayload):
        payload_dict = payload.model_dump(mode="json")
    else:
        payload_dict = payload

    payload_bytes = json.dumps(payload_dict, separators=(",", ":"), sort_keys=True).encode("utf-8")

    if format_type.lower() == "paseto":
        # PASETO v4.public header: v4.public.
        header = b"v4.public."
        # PASETO authentication data: pre-auth encode header and payload
        # PASETO v4.public signs M2 = PAE(header, payload, footer, implicit_assertion)
        sig = ed25519_sign(header + payload_bytes, private_seed)
        body = _b64url_encode(payload_bytes + sig)
        return f"v4.public.{body}"
    else:
        # JWT EdDSA
        hdr = _b64url_encode(b'{"alg":"EdDSA","typ":"JWT"}')
        pld = _b64url_encode(payload_bytes)
        signing_input = f"{hdr}.{pld}".encode("ascii")
        sig = ed25519_sign(signing_input, private_seed)
        sig_b64 = _b64url_encode(sig)
        return f"{hdr}.{pld}.{sig_b64}"


def verify_license_token(
    token: str,
    public_key_bytes: bytes,
    expected_hardware_id: str | None = None,
    allow_expired: bool = False,
) -> LicensePayload:
    """Verifies a signed license token (PASETO v4.public or JWT EdDSA).

    Performs:
    1. Signature verification against public_key_bytes.
    2. Payload structure validation into `LicensePayload`.
    3. Expiry date check against UTC time.
    4. Hardware ID check if bound to an airgap hardware node.
    """
    clean_token = token.strip()
    payload_dict: dict[str, Any]

    if clean_token.startswith("v4.public."):
        # PASETO v4.public
        raw_b64 = clean_token[len("v4.public.") :]
        try:
            raw_bytes = _b64url_decode(raw_b64)
        except Exception as exc:
            raise LicenseValidationError(f"Invalid PASETO token base64: {exc}") from exc

        if len(raw_bytes) < 64:
            raise LicenseValidationError("PASETO token body too short")

        payload_bytes = raw_bytes[:-64]
        signature = raw_bytes[-64:]

        header = b"v4.public."
        valid = ed25519_verify(header + payload_bytes, signature, public_key_bytes)
        if not valid:
            raise LicenseValidationError("Invalid license signature (Ed25519 verification failed)")

        try:
            payload_dict = json.loads(payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise LicenseValidationError(f"Malformed license payload: {exc}") from exc

    elif "." in clean_token and len(clean_token.split(".")) == 3:
        # JWT EdDSA
        parts = clean_token.split(".")
        hdr_b64, pld_b64, sig_b64 = parts
        signing_input = f"{hdr_b64}.{pld_b64}".encode("ascii")
        try:
            signature = _b64url_decode(sig_b64)
            payload_bytes = _b64url_decode(pld_b64)
        except Exception as exc:
            raise LicenseValidationError(f"Invalid JWT base64 parts: {exc}") from exc

        valid = ed25519_verify(signing_input, signature, public_key_bytes)
        if not valid:
            raise LicenseValidationError("Invalid license signature (EdDSA verification failed)")

        try:
            payload_dict = json.loads(payload_bytes.decode("utf-8"))
        except Exception as exc:
            raise LicenseValidationError(f"Malformed JWT license payload: {exc}") from exc

    else:
        raise LicenseValidationError(
            "Unrecognized license token format (expected PASETO v4.public or JWT)"
        )

    # Validate schema
    try:
        # Reconcile features
        features = [LicenseFeature(f) for f in payload_dict.get("features", [])]
        plan = LicensePlan(payload_dict.get("plan", LicensePlan.STANDARD))
        mode = LicenseMode(payload_dict.get("mode", LicenseMode.ONLINE_SUBSCRIPTION))

        issued_at_val = payload_dict.get("issued_at")
        if isinstance(issued_at_val, str):
            issued_at = datetime.fromisoformat(issued_at_val.replace("Z", "+00:00"))
        elif isinstance(issued_at_val, (int, float)):
            issued_at = datetime.fromtimestamp(issued_at_val, tz=UTC)
        else:
            issued_at = datetime.now(UTC)

        expires_at_val = payload_dict.get("expires_at")
        expires_at: datetime | None = None
        if isinstance(expires_at_val, str):
            expires_at = datetime.fromisoformat(expires_at_val.replace("Z", "+00:00"))
        elif isinstance(expires_at_val, (int, float)):
            expires_at = datetime.fromtimestamp(expires_at_val, tz=UTC)

        parsed_payload = LicensePayload(
            license_id=str(payload_dict.get("license_id", "")),
            client_id=str(payload_dict.get("client_id", "default")),
            plan=plan,
            mode=mode,
            issued_at=issued_at,
            expires_at=expires_at,
            max_documents=int(payload_dict.get("max_documents", 10_000)),
            max_users=int(payload_dict.get("max_users", 50)),
            hardware_id=payload_dict.get("hardware_id"),
            telemetry_opt_out=bool(payload_dict.get("telemetry_opt_out", True)),
            features=features,
        )
    except Exception as exc:
        raise LicenseValidationError(f"Invalid license fields: {exc}") from exc

    # Check expiration
    if not allow_expired and parsed_payload.expires_at is not None:
        now_utc = datetime.now(UTC)
        if now_utc > parsed_payload.expires_at:
            exp_str = parsed_payload.expires_at.isoformat()
            raise LicenseValidationError(f"License expired at {exp_str} (current: {now_utc})")

    # Check hardware binding
    if (
        expected_hardware_id
        and parsed_payload.hardware_id
        and parsed_payload.mode == LicenseMode.AIRGAP_ENTERPRISE
        and parsed_payload.hardware_id != expected_hardware_id
    ):
        raise LicenseValidationError(
            f"Hardware mismatch: license bound to '{parsed_payload.hardware_id}', "
            f"but current is '{expected_hardware_id}'"
        )

    return parsed_payload
