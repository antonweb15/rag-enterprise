"""Offline Ed25519 license validator + Grace/Restricted Mode (client side, in the box artifact).

Implements :class:`BaseLicenseValidator` (`core/modules_spec.md` §2.0). Responsibilities:

* ``validate_token`` - OFFLINE verification: Ed25519 signature (via the embedded public keyring),
  ``expires_at`` expiry, CRL revocation and the composite ``hardware_id`` binding
  (`core/licensing_and_distribution.md` §3.2);
* ``verify_heartbeat`` - the ``online_subscription`` heartbeat with a 72h Grace Period, after which
  the installation drops to Restricted Mode; ``airgap_enterprise`` disables the heartbeat entirely;
* ``enforce_quota`` - ``max_documents`` / ``max_users`` enforcement, raising
  :class:`QuotaExceededError`.

The validator is fail-closed: any verification failure keeps the installation unlicensed. It never
holds a private key - only public keys for offline verification (`§4.3`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Literal
from uuid import UUID

from rag_core.contracts.enums import LicenseMode
from rag_core.contracts.interfaces import BaseLicenseValidator
from rag_core.contracts.models import LicensePayload
from rag_core.licensing.errors import (
    HardwareMismatchError,
    InvalidLicenseError,
    LicenseExpiredError,
    LicenseRevokedError,
    QuotaExceededError,
)
from rag_core.licensing.tokens import LicenseKeyring, verify_license

logger = logging.getLogger(__name__)

# Defaults aligned with core/licensing_and_distribution.md §3.2 and core/license_server.md §2 p.4.
DEFAULT_GRACE_PERIOD = timedelta(hours=72)
DEFAULT_HEARTBEAT_INTERVAL = timedelta(hours=24)

# A usage provider answers "how many X does tenant T currently use?" (documents/users/requests).
UsageProvider = Callable[[UUID, str], int]
# A heartbeat client pings the master server; returns True when the license is live (not revoked).
HeartbeatClient = Callable[[LicensePayload], Awaitable[bool]]


class LicenseState(StrEnum):
    """The runtime licensing state driving feature gating in the core."""

    ACTIVE = "active"  # fully valid (verified signature, not expired, heartbeat fresh)
    GRACE = "grace"  # heartbeat temporarily unreachable, still within the 72h grace window
    RESTRICTED = "restricted"  # grace elapsed / invalid: demo limits, watermarks, no full search


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(moment: datetime) -> datetime:
    """Treats a naive datetime as UTC so comparisons never raise."""
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


class Ed25519LicenseValidator(BaseLicenseValidator):
    """The concrete offline :class:`BaseLicenseValidator` provider.

    ``public_keys`` is either a :class:`LicenseKeyring`, a ``kid -> PEM`` mapping, or ``None`` (set
    later). ``hardware_signals`` is the local composite fingerprint for the ``hardware_id`` binding
    check; ``usage_provider`` backs ``enforce_quota``; ``heartbeat_client`` does the online ping.
    ``grace_period`` is the 72h offline window; ``heartbeat_interval`` is the periodic ping cadence
    (a live result within the interval is trusted without re-pinging, ``§3.2``).
    """

    def __init__(
        self,
        public_keys: LicenseKeyring | Mapping[str, bytes] | None = None,
        *,
        hardware_signals: Mapping[str, str] | None = None,
        usage_provider: UsageProvider | None = None,
        heartbeat_client: HeartbeatClient | None = None,
        grace_period: timedelta = DEFAULT_GRACE_PERIOD,
        heartbeat_interval: timedelta = DEFAULT_HEARTBEAT_INTERVAL,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if isinstance(public_keys, LicenseKeyring):
            self._keyring = public_keys
        else:
            self._keyring = LicenseKeyring(public_keys)
        self._hardware_signals = dict(hardware_signals or {})
        self._usage_provider = usage_provider
        self._heartbeat_client = heartbeat_client
        self._grace_period = grace_period
        self._heartbeat_interval = heartbeat_interval
        self._clock = clock

        self._revoked: set[UUID] = set()
        self._payload: LicensePayload | None = None
        # The last time the heartbeat confirmed the license live (seeds the grace window).
        self._last_heartbeat_ok: datetime | None = None
        self._state: LicenseState = LicenseState.RESTRICTED

    # --- public state accessors -------------------------------------------------
    @property
    def state(self) -> LicenseState:
        """The current :class:`LicenseState` (ACTIVE / GRACE / RESTRICTED)."""
        return self._state

    @property
    def restricted(self) -> bool:
        """True when the core must run in Restricted Mode (demo limits, watermarks)."""
        return self._state is LicenseState.RESTRICTED

    @property
    def active_license(self) -> LicensePayload | None:
        """The last successfully validated payload (``None`` until ``validate_token``)."""
        return self._payload

    def load_crl(self, revoked_license_ids: list[UUID] | set[UUID]) -> None:
        """Loads a RAW (already-trusted) CRL snapshot.

        Prefer :meth:`load_signed_crl` on the wire: a bare id list carries no provenance, so this
        must only be used for an already-verified/local snapshot (`§3.2`).
        """
        self._revoked = {UUID(str(x)) for x in revoked_license_ids}

    def load_signed_crl(self, crl: object) -> None:
        """Verifies a SIGNED CRL against the embedded keyring BEFORE applying it (`§6`).

        The CRL (a :class:`rag_core.licensing.server.SignedCRL`) is signed with the same offline key
        as the license tokens; the client recomputes its canonical bytes and checks the Ed25519
        signature against each public key it embeds. An unsigned/forged/tampered CRL is refused with
        :class:`InvalidLicenseError` and the current revocation set is left untouched (fail-safe).
        """
        from rag_core.admin.license_crypto import ed25519_verify

        signing_bytes = crl.signing_bytes()  # type: ignore[attr-defined]
        signature = crl.signature  # type: ignore[attr-defined]
        verified = any(
            ed25519_verify(signing_bytes, signature, public_key)
            for public_key in self._keyring.as_mapping().values()
        )
        if not verified:
            raise InvalidLicenseError("Signed CRL failed verification against the embedded keyring")
        self._revoked = {UUID(str(x)) for x in crl.revoked_ids}  # type: ignore[attr-defined]

    # --- BaseLicenseValidator ---------------------------------------------------
    def validate_token(self, token: str) -> LicensePayload:
        """OFFLINE validation: signature, expiry, CRL and ``hardware_id`` binding.

        On success the payload is cached (for ``enforce_quota``) and the state becomes ACTIVE for
        both profiles: an ``airgap_enterprise`` license is fully autonomous (no heartbeat), while an
        ``online_subscription`` license is ACTIVE with its grace window armed - a subsequent missed
        heartbeat is what later drops it to GRACE/Restricted via :meth:`verify_heartbeat`.
        """
        payload = verify_license(token, self._keyring.as_mapping())

        now = self._clock()
        if payload.expires_at is not None and _as_aware(payload.expires_at) <= now:
            self._state = LicenseState.RESTRICTED
            raise LicenseExpiredError(
                f"License {payload.license_id} expired at {payload.expires_at.isoformat()}"
            )

        if UUID(payload.license_id) in self._revoked:
            self._state = LicenseState.RESTRICTED
            raise LicenseRevokedError(f"License {payload.license_id} is revoked (CRL)")

        if payload.hardware_id is not None and self._hardware_signals:
            from rag_core.licensing.hardware import compute_hardware_id

            local = compute_hardware_id(self._hardware_signals)
            if local != payload.hardware_id:
                self._state = LicenseState.RESTRICTED
                raise HardwareMismatchError(
                    f"License {payload.license_id} is bound to a different hardware_id"
                )

        self._payload = payload
        if payload.mode is LicenseMode.AIRGAP_ENTERPRISE:
            # Fully autonomous: no outbound heartbeat, signature+expiry+hardware are authoritative.
            self._state = LicenseState.ACTIVE
        else:
            # Online subscription: valid now, pending the first heartbeat (grace window armed).
            self._last_heartbeat_ok = now
            self._state = LicenseState.ACTIVE
        return payload

    async def verify_heartbeat(self, license_payload: LicensePayload) -> bool:
        """Online heartbeat with a 72h Grace Period -> Restricted Mode transition.

        * ``airgap_enterprise``: the heartbeat is disabled; always returns True (offline-only).
        * ``online_subscription``: pings the master server. On success the grace window resets and
          the state is ACTIVE. On a transient failure the box keeps running in GRACE until the 72h
          window elapses, after which it returns False and flips to Restricted Mode.
        """
        if license_payload.mode is LicenseMode.AIRGAP_ENTERPRISE:
            self._state = LicenseState.ACTIVE
            return True

        now = self._clock()

        # Periodic heartbeat cadence (§3.2): when a heartbeat already confirmed the license live
        # within ``heartbeat_interval``, trust that result and skip the network ping.
        if (
            self._last_heartbeat_ok is not None
            and now - self._last_heartbeat_ok <= self._heartbeat_interval
        ):
            return True

        live: bool | None = None
        if self._heartbeat_client is not None:
            try:
                live = await self._heartbeat_client(license_payload)
            except Exception:  # network/transport failure -> fall back to the grace window
                logger.warning(
                    "Heartbeat to the license server failed; entering grace evaluation",
                    exc_info=True,
                )
                live = None

        if live is True:
            self._last_heartbeat_ok = now
            self._state = LicenseState.ACTIVE
            return True
        if live is False:
            # The server positively reported the license as dead/revoked -> immediate restriction.
            self._state = LicenseState.RESTRICTED
            return False

        # No answer: stay alive while inside the grace window, otherwise restrict.
        anchor = self._last_heartbeat_ok or now
        if now - anchor <= self._grace_period:
            self._state = LicenseState.GRACE
            logger.warning(
                "License server unreachable; operating in grace mode until %s",
                (anchor + self._grace_period).isoformat(),
            )
            return True
        self._state = LicenseState.RESTRICTED
        logger.error("Grace period elapsed without a heartbeat; entering Restricted Mode")
        return False

    async def enforce_quota(
        self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]
    ) -> None:
        """Checks a licensed quota, raising :class:`QuotaExceededError` on excess.

        ``documents`` -> ``max_documents``, ``users`` -> ``max_users``. ``requests`` has no license
        cap (rate limiting lives in the transport layer), so it is a no-op here. Without a validated
        license the quota is treated as 0 (fail-closed) for the capped metrics.
        """
        if metric == "requests":
            return
        limit_map = {
            "documents": self._payload.max_documents if self._payload else 0,
            "users": self._payload.max_users if self._payload else 0,
        }
        limit = limit_map[metric]
        if self._usage_provider is None:
            return
        current = self._usage_provider(tenant_id, metric)
        if current > limit:
            raise QuotaExceededError(metric=metric, limit=limit, current=current)
