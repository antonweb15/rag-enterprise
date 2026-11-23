"""Licensing error hierarchy (`IMPLEMENTATION_PLAN.md` Stage K).

A single exception tree for the client validator and the vendor license server. All licensing
failures derive from :class:`LicenseError`, so a consumer can catch the whole family with one
``except``. ``fail-closed`` is the rule: when a token cannot be verified the core must treat the
installation as unlicensed (Restricted Mode), never as licensed.
"""

from __future__ import annotations


class LicenseError(Exception):
    """Base class for every licensing failure (client validator + vendor server)."""


class InvalidLicenseError(LicenseError):
    """The token is malformed, has an unknown key id, or its Ed25519 signature does not verify."""


class LicenseExpiredError(LicenseError):
    """The token's ``expires_at`` is in the past (perpetual box licenses never raise this)."""


class LicenseRevokedError(LicenseError):
    """The license id is present in the certificate revocation list (CRL)."""


class HardwareMismatchError(LicenseError):
    """The token's ``hardware_id`` does not match the local composite fingerprint."""


class QuotaExceededError(LicenseError):
    """A licensed quota (``max_documents`` / ``max_users``) has been exceeded."""

    def __init__(self, metric: str, limit: int, current: int) -> None:
        self.metric = metric
        self.limit = limit
        self.current = current
        super().__init__(f"License quota exceeded for '{metric}': {current} > limit {limit}")
