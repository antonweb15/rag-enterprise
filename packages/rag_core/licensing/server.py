"""In-memory reference License Server (VENDOR CONTOUR ONLY, `core/license_server.md`).

Implements :class:`BaseLicenseServer` (`core/modules_spec.md` §2.0): issuance, revocation, heartbeat
intake, the certificate revocation list (CRL), activation-code redemption and hardware
re-activation. This is the *server half* of licensing and is **NOT** part of the ``on_premise_box``
artifact (`core/license_server.md` §1).

Key decisions honoured here:

* **Single signing source** - tokens are produced ONLY through the injected :class:`TokenSigner`
  (the signing service / HSM / KMS); this process never holds the raw private key (`§2 p.1`).
* **Two profiles** - ``online_subscription`` (has ``expires_at`` + heartbeat, NOT hardware-bound)
  and ``airgap_enterprise`` (``hardware_id``-bound, may be perpetual) - validated at issuance
  (`§2 p.3`).
* **Fail-closed heartbeats** - a heartbeat for an unknown/never-issued license id, or a
  ``hardware_id`` that does not match the issued binding, is rejected (clone signal)
  (`deepseek-criticism.md` Step K §🔴3).
* **Signed CRL** - revocation is published as a snapshot SIGNED with the same offline key so the
  client can verify it before applying (`§6`).
* **Re-activation** - hardware changes are handled by an audited, rate-limited re-activation path
  (`§4.2`, `§5`, `IMPLEMENTATION_PLAN.md:269`).

The storage here is in-memory (dicts); a production server persists to ``licenses`` /
``box_deployments`` / ``backoffice_audit_log`` (`core/storage_schema.md`). It is a REFERENCE
implementation - persistence and the backoffice HTTP endpoints belong to later stages (L/M).
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import UUID, uuid4

from rag_core.contracts.enums import ActivationCodeStatus, LicenseMode, LicensePlan
from rag_core.contracts.interfaces import BaseLicenseServer
from rag_core.contracts.models import (
    ActivationCode,
    HeartbeatPayload,
    LicenseIssueRequest,
    LicensePayload,
    LicenseRevokeRequest,
)
from rag_core.licensing.errors import (
    HardwareMismatchError,
    InvalidLicenseError,
    LicenseError,
    LicenseRevokedError,
)
from rag_core.licensing.tokens import TokenSigner

DEFAULT_GRACE_PERIOD = timedelta(hours=72)
# A license may be re-activated onto new hardware only a few times (anti-abuse, `§5`).
DEFAULT_MAX_REACTIVATIONS = 3

# The activation-code alphabet (no ambiguous 0/O/1/I) and the RAG-ENT-XXXX-YYYY-ZZZZ shape.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_GROUPS = 3
_CODE_GROUP_LEN = 4


def _utcnow() -> datetime:
    return datetime.now(UTC)


class HeartbeatState(StrEnum):
    """The grace/restricted state returned to an installation (server-side vocabulary)."""

    ACTIVE = "active"
    GRACE = "grace"
    RESTRICTED = "restricted"
    REVOKED = "revoked"
    UNKNOWN = "unknown"  # the license id was never issued -> fail-closed


@dataclass
class RevocationEntry:
    """A single CRL record (`core/license_server.md` §4.2)."""

    license_id: UUID
    reason: str
    revoked_at: datetime


@dataclass
class SignedCRL:
    """A CRL snapshot SIGNED with the offline key; the client verifies it before applying (`§6`).

    ``signature`` is the Ed25519 signature over :meth:`signing_bytes` (the canonical JSON of the
    sorted revoked ids + ``issued_at``). The validator recomputes those bytes and checks the
    signature against its embedded public keyring; an unsigned/forged CRL is refused.
    """

    revoked_ids: list[str]
    issued_at: datetime
    signature: bytes

    def signing_bytes(self) -> bytes:
        """Canonical, deterministic bytes that are signed/verified (sorted ids + issued_at)."""
        payload = {
            "revoked_ids": sorted(self.revoked_ids),
            "issued_at": self.issued_at.isoformat(),
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


@dataclass
class HeartbeatStatus:
    """The computed grace/restricted status returned to an installation (`§4.2`)."""

    status: HeartbeatState
    grace_until: datetime | None
    restricted: bool


@dataclass
class ReactivationRecord:
    """An audited hardware re-activation event (`§4.2`, `§5`)."""

    license_id: UUID
    old_hardware_id: str | None
    new_hardware_id: str
    reason: str
    reactivated_at: datetime


@dataclass
class _HeartbeatRecord:
    hardware_id: str
    last_heartbeat_at: datetime
    last_payload: HeartbeatPayload | None = None


@dataclass
class _IssuedLicense:
    payload: LicensePayload
    token: str
    issued_at: datetime
    revoked: bool = False
    reactivations: list[ReactivationRecord] = field(default_factory=list)


class InMemoryLicenseServer(BaseLicenseServer):
    """A self-contained :class:`BaseLicenseServer` for the vendor backoffice / tests.

    ``signer`` is the mandatory :class:`TokenSigner` port (the only path to a signature). The server
    tracks issued licenses, the CRL, received heartbeats, activation codes and re-activation audit
    records in memory.
    """

    def __init__(
        self,
        signer: TokenSigner,
        *,
        grace_period: timedelta = DEFAULT_GRACE_PERIOD,
        max_reactivations: int = DEFAULT_MAX_REACTIVATIONS,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._signer = signer
        self._grace_period = grace_period
        self._max_reactivations = max_reactivations
        self._clock = clock

        self._issued: dict[UUID, _IssuedLicense] = {}
        self._revoked: dict[UUID, RevocationEntry] = {}
        self._heartbeats: dict[UUID, _HeartbeatRecord] = {}
        self._activation_codes: dict[str, ActivationCode] = {}
        # A minimal append-only audit trail (a production server writes backoffice_audit_log).
        self._audit: list[dict[str, object]] = []

    # --- BaseLicenseServer ------------------------------------------------------
    async def issue_license(self, request: LicenseIssueRequest) -> str:
        """Validates the plan/mode/hardware profile, then signs a :class:`LicensePayload`.

        Profile guard (`§2 p.3`): an ``airgap_enterprise`` license MUST be hardware-bound, while an
        ``online_subscription`` license MUST NOT carry a ``hardware_id`` and MUST have an
        ``expires_at``. Signing is delegated to the injected :class:`TokenSigner` (the key never
        enters this process).
        """
        self._validate_issue_profile(request)
        license_id = uuid4()
        payload = LicensePayload(
            license_id=str(license_id),
            client_id=str(request.customer_id),
            plan=request.plan,
            mode=request.mode,
            issued_at=self._clock(),
            expires_at=request.expires_at,
            max_documents=request.max_documents,
            max_users=request.max_users,
            hardware_id=request.hardware_id,
            features=list(request.features),
        )
        token = self._signer.sign(payload)
        self._issued[license_id] = _IssuedLicense(
            payload=payload, token=token, issued_at=payload.issued_at
        )
        self._audit_event("license.issue", license_id, {"mode": str(request.mode)})
        return token

    def _validate_issue_profile(self, request: LicenseIssueRequest) -> None:
        if request.mode is LicenseMode.AIRGAP_ENTERPRISE:
            if not request.hardware_id:
                raise LicenseError(
                    "airgap_enterprise license must be bound to a hardware_id (spec §2 p.3)"
                )
        else:  # online_subscription
            if request.hardware_id:
                raise LicenseError(
                    "online_subscription license must not carry a hardware_id (not hardware-bound)"
                )
            if request.expires_at is None:
                raise LicenseError(
                    "online_subscription license must have an expires_at (perpetual is airgap-only)"
                )

    async def revoke_license(self, request: LicenseRevokeRequest) -> None:
        """Marks the license revoked and publishes it to the CRL (`§4.2`)."""
        self._revoked[request.license_id] = RevocationEntry(
            license_id=request.license_id,
            reason=request.reason,
            revoked_at=self._clock(),
        )
        issued = self._issued.get(request.license_id)
        if issued is not None:
            issued.revoked = True
        self._audit_event("license.revoke", request.license_id, {"reason": request.reason})

    async def register_heartbeat(self, hardware_id: str, license_id: UUID) -> None:
        """Receives a heartbeat: fail-closed on unknown id, revocation or hardware mismatch.

        * An unknown / never-issued license id raises :class:`InvalidLicenseError` (a heartbeat must
          reference a real license, `§🔴3`).
        * A revoked license raises :class:`LicenseRevokedError`.
        * A ``hardware_id`` differing from the issued binding raises
          :class:`HardwareMismatchError` (clone signal; the box must re-activate first).
        """
        self._require_issued(license_id)
        if license_id in self._revoked:
            raise LicenseRevokedError(f"License {license_id} is revoked")
        self._assert_hardware(license_id, hardware_id)
        self._heartbeats[license_id] = _HeartbeatRecord(
            hardware_id=hardware_id, last_heartbeat_at=self._clock()
        )

    async def generate_activation_code(
        self, customer_id: UUID, plan: LicensePlan
    ) -> ActivationCode:
        """Generates a one-time ``RAG-ENT-XXXX-YYYY-ZZZZ`` activation code (`§4.1`)."""
        code = self._new_activation_code()
        activation = ActivationCode(
            code=code,
            customer_id=customer_id,
            plan=plan,
            status=ActivationCodeStatus.ISSUED,
            created_at=self._clock(),
        )
        self._activation_codes[code] = activation
        return activation

    async def redeem_activation_code(
        self,
        code: str,
        tenant_id: UUID,
        *,
        mode: LicenseMode = LicenseMode.ONLINE_SUBSCRIPTION,
        max_documents: int = 1000,
        max_users: int = 10,
        expires_at: datetime | None = None,
        hardware_id: str | None = None,
    ) -> str:
        """Exchanges a one-time activation code for a signed license token (`§4.1`).

        The code must exist and still be ``ISSUED``; on success it is marked ``REDEEMED`` (bound to
        ``tenant_id``) and a license is issued for its ``customer_id``/``plan``.
        """
        activation = self._activation_codes.get(code)
        if activation is None:
            raise InvalidLicenseError(f"Unknown activation code: {code!r}")
        if activation.status is not ActivationCodeStatus.ISSUED:
            raise LicenseError(
                f"Activation code {code!r} is not redeemable (status={activation.status})"
            )
        token = await self.issue_license(
            LicenseIssueRequest(
                customer_id=activation.customer_id,
                tenant_id=tenant_id,
                plan=activation.plan,
                mode=mode,
                max_documents=max_documents,
                max_users=max_users,
                expires_at=expires_at,
                hardware_id=hardware_id,
            )
        )
        activation.status = ActivationCodeStatus.REDEEMED
        activation.redeemed_by_tenant_id = tenant_id
        self._activation_codes[code] = activation
        self._audit_event("license.redeem", None, {"code": code, "tenant_id": str(tenant_id)})
        return token

    async def reactivate_license(
        self,
        license_id: UUID,
        new_hardware_id: str,
        reason: str,
        old_hardware_id: str | None = None,
    ) -> str:
        """Re-binds a license to new hardware, re-issuing the token (audited + rate-limited, `§5`).

        Required by `license_server.md` §4.2/§5 and `IMPLEMENTATION_PLAN.md:269`. Rejects unknown or
        revoked licenses, enforces :data:`DEFAULT_MAX_REACTIVATIONS`, records an audited
        :class:`ReactivationRecord` and returns a freshly signed token bound to ``new_hardware_id``.
        """
        issued = self._require_issued(license_id)
        if license_id in self._revoked:
            raise LicenseRevokedError(f"License {license_id} is revoked; cannot re-activate")
        if len(issued.reactivations) >= self._max_reactivations:
            raise LicenseError(
                f"License {license_id} reached the re-activation limit "
                f"({self._max_reactivations}); contact the vendor"
            )
        now = self._clock()
        new_payload = issued.payload.model_copy(update={"hardware_id": new_hardware_id})
        new_token = self._signer.sign(new_payload)
        record = ReactivationRecord(
            license_id=license_id,
            old_hardware_id=old_hardware_id or issued.payload.hardware_id,
            new_hardware_id=new_hardware_id,
            reason=reason,
            reactivated_at=now,
        )
        issued.payload = new_payload
        issued.token = new_token
        issued.reactivations.append(record)
        # A new hardware binding invalidates any stale heartbeat record.
        self._heartbeats.pop(license_id, None)
        self._audit_event(
            "license.reactivate",
            license_id,
            {"old": record.old_hardware_id, "new": new_hardware_id, "reason": reason},
        )
        return new_token

    def reactivations(self, license_id: UUID) -> list[ReactivationRecord]:
        """The audited re-activation history for a license."""
        issued = self._issued.get(license_id)
        return list(issued.reactivations) if issued is not None else []

    # --- heartbeat intake with explicit payload (public endpoint helper) --------
    async def ingest_heartbeat(self, payload: HeartbeatPayload) -> HeartbeatStatus:
        """Full heartbeat intake from :class:`HeartbeatPayload`, returning the grace status.

        Fail-closed: an unknown license id yields :attr:`HeartbeatState.UNKNOWN`/restricted, and a
        hardware-id mismatch yields restricted (`§🔴3`). Only anonymized aggregates are stored; when
        ``telemetry_opt_out`` is set the metric fields are dropped (`core/license_server.md` §4.2).
        """
        if payload.license_id not in self._issued:
            return HeartbeatStatus(status=HeartbeatState.UNKNOWN, grace_until=None, restricted=True)
        if payload.license_id in self._revoked:
            return HeartbeatStatus(status=HeartbeatState.REVOKED, grace_until=None, restricted=True)
        bound = self._issued[payload.license_id].payload.hardware_id
        if bound is not None and bound != payload.hardware_id:
            return HeartbeatStatus(
                status=HeartbeatState.RESTRICTED, grace_until=None, restricted=True
            )
        stored = None if payload.telemetry_opt_out else payload
        self._heartbeats[payload.license_id] = _HeartbeatRecord(
            hardware_id=payload.hardware_id,
            last_heartbeat_at=self._clock(),
            last_payload=stored,
        )
        return self.heartbeat_status(payload.license_id)

    def heartbeat_status(self, license_id: UUID) -> HeartbeatStatus:
        """Computes the current grace/restricted status for a license from its last heartbeat."""
        if license_id not in self._issued:
            return HeartbeatStatus(status=HeartbeatState.UNKNOWN, grace_until=None, restricted=True)
        if license_id in self._revoked:
            return HeartbeatStatus(status=HeartbeatState.REVOKED, grace_until=None, restricted=True)
        record = self._heartbeats.get(license_id)
        if record is None:
            return HeartbeatStatus(
                status=HeartbeatState.RESTRICTED, grace_until=None, restricted=True
            )
        grace_until = record.last_heartbeat_at + self._grace_period
        if self._clock() <= grace_until:
            return HeartbeatStatus(
                status=HeartbeatState.ACTIVE, grace_until=grace_until, restricted=False
            )
        return HeartbeatStatus(
            status=HeartbeatState.RESTRICTED, grace_until=grace_until, restricted=True
        )

    # --- CRL --------------------------------------------------------------------
    def is_revoked(self, license_id: UUID) -> bool:
        """True iff the license id is in the CRL."""
        return license_id in self._revoked

    def crl(self, since: datetime | None = None) -> list[RevocationEntry]:
        """Returns the CRL snapshot, optionally only entries revoked after ``since`` (`§4.2`)."""
        entries = sorted(self._revoked.values(), key=lambda e: e.revoked_at)
        if since is not None:
            entries = [e for e in entries if e.revoked_at > since]
        return entries

    def signed_crl(self, since: datetime | None = None) -> SignedCRL:
        """Returns a SIGNED CRL snapshot the client can verify before applying (`§6`)."""
        entries = self.crl(since)
        issued_at = self._clock()
        crl = SignedCRL(
            revoked_ids=[str(e.license_id) for e in entries],
            issued_at=issued_at,
            signature=b"",
        )
        crl.signature = self._signer.sign_bytes(crl.signing_bytes())
        return crl

    # --- helpers ----------------------------------------------------------------
    def _require_issued(self, license_id: UUID) -> _IssuedLicense:
        issued = self._issued.get(license_id)
        if issued is None:
            raise InvalidLicenseError(f"License {license_id} was never issued (fail-closed)")
        return issued

    def _assert_hardware(self, license_id: UUID, hardware_id: str) -> None:
        bound = self._issued[license_id].payload.hardware_id
        if bound is not None and bound != hardware_id:
            raise HardwareMismatchError(
                f"License {license_id} is bound to a different hardware_id; re-activate first"
            )

    def _audit_event(self, action: str, license_id: UUID | None, extra: dict[str, object]) -> None:
        self._audit.append(
            {
                "action": action,
                "license_id": str(license_id) if license_id else None,
                "at": self._clock().isoformat(),
                **extra,
            }
        )

    @property
    def audit_log(self) -> list[dict[str, object]]:
        """The in-memory audit trail (a production server persists to backoffice_audit_log)."""
        return list(self._audit)

    def _new_activation_code(self) -> str:
        while True:
            groups = [
                "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_GROUP_LEN))
                for _ in range(_CODE_GROUPS)
            ]
            code = "RAG-ENT-" + "-".join(groups)
            if code not in self._activation_codes:
                return code
