"""Licensing subsystem (`IMPLEMENTATION_PLAN.md` Stage K).

Two halves with a shared Ed25519 cryptographic contour:

* **Client (in the box artifact)** - :class:`Ed25519LicenseValidator` (offline signature/expiry/CRL
  + ``hardware_id`` binding), the composite :mod:`hardware` fingerprint, Grace/Restricted Mode and
  the heartbeat client hook. See ``core/licensing_and_distribution.md``.
* **Server (vendor contour ONLY, NOT shipped in the box)** - :class:`InMemoryLicenseServer`
  (issuance/revocation/heartbeat/CRL/activation codes). See ``core/license_server.md``.

The token codec (:mod:`tokens`) and the :class:`TokenSigner` port keep the private signing key out
of both the box artifact and the server process (`core/license_server.md` §2 p.1).
"""

from __future__ import annotations

from rag_core.licensing.errors import (
    HardwareMismatchError,
    InvalidLicenseError,
    LicenseError,
    LicenseExpiredError,
    LicenseRevokedError,
    QuotaExceededError,
)
from rag_core.licensing.hardware import (
    HardwareFingerprint,
    collect_signals,
    compute_hardware_id,
    current_fingerprint,
    fingerprint_similarity,
    hardware_id_matches,
)
from rag_core.licensing.server import (
    HeartbeatState,
    HeartbeatStatus,
    InMemoryLicenseServer,
    ReactivationRecord,
    RevocationEntry,
    SignedCRL,
)
from rag_core.licensing.tokens import (
    Ed25519KeyPair,
    Ed25519TokenSigner,
    LicenseKeyring,
    TokenSigner,
    generate_keypair,
    sign_license,
    verify_license,
)
from rag_core.licensing.validator import (
    DEFAULT_GRACE_PERIOD,
    DEFAULT_HEARTBEAT_INTERVAL,
    Ed25519LicenseValidator,
    LicenseState,
)

__all__ = [
    # errors
    "LicenseError",
    "InvalidLicenseError",
    "LicenseExpiredError",
    "LicenseRevokedError",
    "HardwareMismatchError",
    "QuotaExceededError",
    # tokens
    "Ed25519KeyPair",
    "Ed25519TokenSigner",
    "LicenseKeyring",
    "TokenSigner",
    "generate_keypair",
    "sign_license",
    "verify_license",
    # hardware
    "HardwareFingerprint",
    "collect_signals",
    "compute_hardware_id",
    "current_fingerprint",
    "fingerprint_similarity",
    "hardware_id_matches",
    # validator
    "Ed25519LicenseValidator",
    "LicenseState",
    "DEFAULT_GRACE_PERIOD",
    "DEFAULT_HEARTBEAT_INTERVAL",
    # server
    "InMemoryLicenseServer",
    "HeartbeatStatus",
    "HeartbeatState",
    "RevocationEntry",
    "ReactivationRecord",
    "SignedCRL",
]
