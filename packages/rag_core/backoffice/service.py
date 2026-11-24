"""Vendor Backoffice backend service (`core/vendor_backoffice.md` §4, §5).

Business logic for the vendor super-admin contour: customers, orders, activation codes, license
issuance/revocation/renewal/upgrade, the box registry and break-glass impersonation. Every
mutation writes a ``backoffice_audit_log`` entry in the SAME transaction (§2 p.6). The data lives
in the cross-tenant Core tables defined in :mod:`rag_core.backoffice.schema` (outside tenant-RLS).

The private Ed25519 signing key is NEVER held here: signing is delegated to the injected
:class:`~rag_core.licensing.tokens.TokenSigner` port (the signing service / HSM / KMS, §7).
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.orm import Session

from rag_core.admin.auth import (
    AuthError,
    generate_totp_secret,
    get_totp_uri,
    hash_password,
    verify_password,
    verify_totp_code,
)
from rag_core.backoffice.lifecycle import (
    InMemoryTenantBackupBackend,
    LangfuseProvisioner,
    StoragePurger,
    TenantBackupBackend,
    deserialize_row,
    serialize_row,
)
from rag_core.backoffice.schema import (
    activation_codes_table,
    backoffice_audit_log_table,
    backoffice_users_table,
    box_deployments_table,
    customers_table,
    orders_table,
)
from rag_core.contracts.enums import (
    ActivationCodeStatus,
    BackofficeRole,
    CustomerStatus,
    LicenseMode,
    LicensePlan,
)
from rag_core.contracts.models import (
    ActivationCode,
    ActivationCodeCreateRequest,
    BackofficeUserRead,
    BoxDeployment,
    Customer,
    CustomerCreateRequest,
    ImpersonationGrant,
    ImpersonationRequest,
    LicenseExtendRequest,
    LicenseIssueRequest,
    LicensePayload,
    LicenseRevokeRequest,
    LicenseStatus,
    LicenseUpgradeRequest,
    ObservabilityConfig,
    Order,
    Page,
    TenantBackupResult,
    TenantExport,
    TenantOffboardRequest,
    TenantOffboardResult,
    TenantProvisionRequest,
    TenantProvisionResult,
    TenantRestoreResult,
    TwoFactorSetupResponse,
    VendorDashboard,
)
from rag_core.contracts.settings import Settings
from rag_core.db import models as orm
from rag_core.licensing.tokens import TokenSigner

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]

_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

# Tenant-scoped ORM models in FK-safe RESTORE order (parents first). Snapshot/export iterate in
# this order; the hard-delete iterates in reverse (children first) so there are no dangling rows -
# we do NOT rely on DB-level ``ON DELETE CASCADE`` (it is a no-op on SQLite without PRAGMA, and we
# want a deterministic, portable legal-deletion receipt). ``Message`` has no ``tenant_id`` column
# and is scoped through its ``conversation_id`` (handled specially below).
_TENANT_RESTORE_ORDER: tuple[Any, ...] = (
    orm.Tenant,
    orm.License,
    orm.AdminUser,
    orm.User,
    orm.Group,
    orm.Collection,
    orm.Document,
    orm.UserGroup,
    orm.DocumentCollection,
    orm.DocumentAcl,
    orm.ParentChunk,
    orm.ChildChunk,
    orm.Conversation,
    orm.Message,
    orm.StorageCredential,
    orm.AuditLog,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _as_aware(dt: datetime | None) -> datetime | None:
    """Normalises a (possibly naive, e.g. from SQLite) datetime to a UTC-aware one."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class BackofficeError(Exception):
    """A backoffice operation failed (bad request / invalid state)."""


class BackofficeService:
    """Core business logic for the vendor backoffice contour."""

    def __init__(
        self,
        session_factory: SessionFactory,
        settings: Settings | None = None,
        signer: TokenSigner | None = None,
        clock: Callable[[], datetime] = _utcnow,
        crl_publisher: Callable[[UUID, str], None] | None = None,
        langfuse_provisioner: LangfuseProvisioner | None = None,
        storage_purger: StoragePurger | None = None,
        backup_backend: TenantBackupBackend | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings or Settings()
        self._signer = signer
        self._clock = clock
        # Optional hook to publish a revocation to the signed CRL consumed by offline boxes
        # (`core/vendor_backoffice.md` §5 "Revoke a license + publish to the CRL"). When wired to
        # the Stage K ``BaseLicenseServer.signed_crl()`` producer, offline validators stop trusting
        # the revoked token at the next CRL refresh.
        self._crl_publisher = crl_publisher
        # Stage N tenant-lifecycle side-effect ports (see rag_core.backoffice.lifecycle).
        # All optional: without them the DB lifecycle still runs, external effects are skipped.
        self._langfuse_provisioner = langfuse_provisioner
        self._storage_purger = storage_purger
        self._backup_backend: TenantBackupBackend = backup_backend or InMemoryTenantBackupBackend()

    # --- audit ---------------------------------------------------------------
    def _record_audit(
        self,
        session: Session,
        action: str,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        impersonated_tenant_id: UUID | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Appends a cross-tenant audit record WITHIN the caller's transaction (§2 p.6)."""
        session.execute(
            insert(backoffice_audit_log_table).values(
                id=uuid4(),
                actor_backoffice_user_id=actor_user_id,
                actor_email=actor_email,
                action=action,
                impersonated_tenant_id=impersonated_tenant_id,
                object_type=object_type,
                object_id=object_id,
                ip_address=ip_address,
                user_agent=user_agent,
                payload=payload or {},
                created_at=self._clock(),
            )
        )

    @staticmethod
    def _page(items: list[Any], total: int, page: int, per_page: int) -> Page[Any]:
        pages = (total + per_page - 1) // per_page if per_page > 0 else 0
        return Page(items=items, total=total, page=page, per_page=per_page, pages=pages)

    @staticmethod
    def _to_customer(row: Any) -> Customer:
        return Customer(
            id=row.id,
            legal_name=row.legal_name,
            display_name=row.display_name,
            inn=row.inn,
            contact_email=row.contact_email,
            contact_phone=row.contact_phone,
            contract_number=row.contract_number,
            status=CustomerStatus(row.status),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_activation_code(row: Any) -> ActivationCode:
        return ActivationCode(
            code=row.code,
            customer_id=row.customer_id,
            order_id=row.order_id,
            plan=LicensePlan(row.plan),
            status=ActivationCodeStatus(row.status),
            redeemed_by_tenant_id=row.redeemed_by_tenant_id,
            expires_at=row.expires_at,
            created_at=row.created_at,
        )

    @staticmethod
    def _to_order(row: Any) -> Order:
        return Order(
            id=row.id,
            customer_id=row.customer_id,
            plan=LicensePlan(row.plan),
            amount_minor=row.amount_minor,
            currency=row.currency,
            status=row.status,
            external_provider=row.external_provider,
            external_ref=row.external_ref,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _to_box(row: Any) -> BoxDeployment:
        return BoxDeployment(
            id=row.id,
            customer_id=row.customer_id,
            tenant_id=row.tenant_id,
            image_version=row.image_version,
            delivered_at=row.delivered_at,
            hardware_id=row.hardware_id,
            last_heartbeat_at=row.last_heartbeat_at,
            grace_period_expires_at=row.grace_period_expires_at,
            restricted_mode=row.restricted_mode,
            telemetry_opt_out=row.telemetry_opt_out,
            observability_forward_enabled=row.observability_forward_enabled,
        )

    @staticmethod
    def _to_backoffice_user(row: Any) -> BackofficeUserRead:
        return BackofficeUserRead(
            id=row.id,
            email=row.email,
            role=BackofficeRole(row.role),
            is_active=row.is_active,
            totp_enabled=row.totp_secret_enc is not None,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @staticmethod
    def _license_to_status(lic: orm.License) -> LicenseStatus:
        now = _utcnow()
        exp = _as_aware(lic.expires_at)
        expired = exp is not None and exp < now
        return LicenseStatus(
            is_valid=not lic.is_revoked and not expired,
            plan=LicensePlan(lic.plan),
            mode=LicenseMode(lic.mode),
            issued_at=lic.issued_at,
            expires_at=lic.expires_at,
            max_documents=lic.max_documents,
            max_users=lic.max_users,
            hardware_id=lic.hardware_id,
            last_heartbeat_at=lic.last_heartbeat_at,
            restricted_mode=lic.is_revoked,
            restricted_reason=lic.revocation_reason,
        )

    # --- backoffice users & authentication (§3, §7) --------------------------
    def create_backoffice_user(
        self,
        email: str,
        password: str,
        role: BackofficeRole = BackofficeRole.SUPPORT,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> BackofficeUserRead:
        now = self._clock()
        user_id = uuid4()
        with self._session_factory() as session, session.begin():
            exists = session.execute(
                select(backoffice_users_table.c.id).where(backoffice_users_table.c.email == email)
            ).first()
            if exists:
                raise BackofficeError(f"Backoffice user '{email}' already exists")
            session.execute(
                insert(backoffice_users_table).values(
                    id=user_id,
                    email=email,
                    hashed_password=hash_password(password),
                    role=str(role),
                    is_active=True,
                    totp_secret_enc=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._record_audit(
                session,
                "backoffice_user.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="backoffice_user",
                object_id=str(user_id),
                payload={"email": email, "role": str(role)},
            )
        return self.get_backoffice_user(user_id)  # type: ignore[return-value]

    def get_backoffice_user(self, user_id: UUID) -> BackofficeUserRead | None:
        with self._session_factory() as session:
            row = session.execute(
                select(backoffice_users_table).where(backoffice_users_table.c.id == user_id)
            ).first()
            return self._to_backoffice_user(row) if row else None

    def list_backoffice_users(self, page: int = 1, per_page: int = 50) -> Page[BackofficeUserRead]:
        with self._session_factory() as session:
            total = session.execute(
                select(func.count()).select_from(backoffice_users_table)
            ).scalar_one()
            rows = session.execute(
                select(backoffice_users_table)
                .order_by(backoffice_users_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            items = [self._to_backoffice_user(r) for r in rows]
            return self._page(items, int(total), page, per_page)

    def authenticate(
        self, email: str, password: str, totp_code: str | None = None
    ) -> tuple[BackofficeUserRead, bool]:
        """Fail-closed login.

        Verifies the password and (mandatory) TOTP and returns ``(user, mfa_verified)``. When 2FA is
        required but the account has no secret yet, we do NOT reject outright: a *limited*
        enrollment session (``mfa_verified=False``) is granted so the user can reach
        ``/auth/2fa/setup`` and enroll, then re-login with a code. A limited session carries no
        business capabilities (guarded endpoints require ``mfa_verified=True``).
        """
        with self._session_factory() as session:
            row = session.execute(
                select(backoffice_users_table).where(backoffice_users_table.c.email == email)
            ).first()
        if row is None or not verify_password(password, row.hashed_password):
            raise AuthError("Invalid credentials", status_code=401)
        if not row.is_active:
            raise AuthError("Backoffice account is inactive", status_code=403)
        secret = row.totp_secret_enc.decode("utf-8") if row.totp_secret_enc else None
        if secret:
            if not totp_code or not verify_totp_code(secret, totp_code):
                raise AuthError("Invalid or missing 2FA code", status_code=401)
            return self._to_backoffice_user(row), True
        if self.settings.backoffice_totp_required:
            # No secret yet: grant a limited enrollment-only session.
            return self._to_backoffice_user(row), False
        return self._to_backoffice_user(row), True

    def setup_totp(self, user_id: UUID) -> TwoFactorSetupResponse:
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(backoffice_users_table).where(backoffice_users_table.c.id == user_id)
            ).first()
            if row is None:
                raise BackofficeError(f"Backoffice user {user_id} not found")
            secret = generate_totp_secret()
            session.execute(
                update(backoffice_users_table)
                .where(backoffice_users_table.c.id == user_id)
                .values(totp_secret_enc=secret.encode("utf-8"), updated_at=self._clock())
            )
            uri = get_totp_uri(secret, email=row.email, issuer="Enterprise RAG Backoffice")
            return TwoFactorSetupResponse(secret=secret, otpauth_uri=uri)

    # --- customers (legal entities, SEPARATE from tenants, §4) ---------------
    def create_customer(
        self,
        request: CustomerCreateRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> Customer:
        now = self._clock()
        customer_id = uuid4()
        with self._session_factory() as session, session.begin():
            session.execute(
                insert(customers_table).values(
                    id=customer_id,
                    legal_name=request.legal_name,
                    display_name=request.display_name,
                    inn=request.inn,
                    contact_email=request.contact_email,
                    contact_phone=request.contact_phone,
                    contract_number=request.contract_number,
                    status=str(request.status),
                    metadata={},
                    created_at=now,
                    updated_at=now,
                )
            )
            self._record_audit(
                session,
                "customer.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="customer",
                object_id=str(customer_id),
                payload={"legal_name": request.legal_name},
            )
        return self.get_customer(customer_id)  # type: ignore[return-value]

    def get_customer(self, customer_id: UUID) -> Customer | None:
        with self._session_factory() as session:
            row = session.execute(
                select(customers_table).where(customers_table.c.id == customer_id)
            ).first()
            return self._to_customer(row) if row else None

    def list_customers(
        self,
        page: int = 1,
        per_page: int = 50,
        status: CustomerStatus | None = None,
        q: str | None = None,
    ) -> Page[Customer]:
        with self._session_factory() as session:
            base = select(customers_table)
            count_q = select(func.count()).select_from(customers_table)
            if status is not None:
                base = base.where(customers_table.c.status == str(status))
                count_q = count_q.where(customers_table.c.status == str(status))
            if q:
                like = f"%{q.strip()}%"
                pattern = or_(
                    customers_table.c.legal_name.ilike(like),
                    customers_table.c.display_name.ilike(like),
                    customers_table.c.contact_email.ilike(like),
                    customers_table.c.inn.ilike(like),
                )
                base = base.where(pattern)
                count_q = count_q.where(pattern)
            total = session.execute(count_q).scalar_one()
            rows = session.execute(
                base.order_by(customers_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            return self._page([self._to_customer(r) for r in rows], int(total), page, per_page)

    def _set_customer_status(
        self, session: Session, customer_id: UUID | None, status: CustomerStatus
    ) -> None:
        if customer_id is None:
            return
        session.execute(
            update(customers_table)
            .where(customers_table.c.id == customer_id)
            .values(status=str(status), updated_at=self._clock())
        )

    # --- orders (§4) ---------------------------------------------------------
    def create_order(
        self,
        customer_id: UUID,
        plan: LicensePlan,
        *,
        amount_minor: int | None = None,
        currency: str = "RUB",
        external_provider: str | None = None,
        external_ref: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> Order:
        now = self._clock()
        order_id = uuid4()
        with self._session_factory() as session, session.begin():
            session.execute(
                insert(orders_table).values(
                    id=order_id,
                    customer_id=customer_id,
                    plan=str(plan),
                    amount_minor=amount_minor,
                    currency=currency,
                    status="pending",
                    external_provider=external_provider,
                    external_ref=external_ref,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._record_audit(
                session,
                "order.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="order",
                object_id=str(order_id),
                payload={"customer_id": str(customer_id), "plan": str(plan)},
            )
        with self._session_factory() as session:
            row = session.execute(select(orders_table).where(orders_table.c.id == order_id)).first()
            return self._to_order(row)

    def list_orders(
        self,
        customer_id: UUID | None = None,
        status: str | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Page[Order]:
        with self._session_factory() as session:
            base = select(orders_table)
            count_q = select(func.count()).select_from(orders_table)
            if customer_id is not None:
                base = base.where(orders_table.c.customer_id == customer_id)
                count_q = count_q.where(orders_table.c.customer_id == customer_id)
            if status is not None:
                base = base.where(orders_table.c.status == status)
                count_q = count_q.where(orders_table.c.status == status)
            total = session.execute(count_q).scalar_one()
            rows = session.execute(
                base.order_by(orders_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            return self._page([self._to_order(r) for r in rows], int(total), page, per_page)

    # --- activation codes (§4) ----------------------------------------------
    def _generate_code(self) -> str:
        groups = ["-".join(secrets.choice(_CODE_ALPHABET) for _ in range(4)) for _ in range(3)]
        return "RAG-ENT-" + "-".join(groups)

    def generate_activation_code(
        self,
        request: ActivationCodeCreateRequest,
        *,
        order_id: UUID | None = None,
        expires_at: datetime | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> ActivationCode:
        now = self._clock()
        code = self._generate_code()
        code_id = uuid4()
        with self._session_factory() as session, session.begin():
            session.execute(
                insert(activation_codes_table).values(
                    id=code_id,
                    code=code,
                    customer_id=request.customer_id,
                    order_id=order_id,
                    plan=str(request.plan),
                    status=str(ActivationCodeStatus.ISSUED),
                    redeemed_by_tenant_id=None,
                    expires_at=expires_at,
                    created_at=now,
                )
            )
            self._record_audit(
                session,
                "activation_code.generate",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="activation_code",
                object_id=code,
                payload={"customer_id": str(request.customer_id), "plan": str(request.plan)},
            )
        with self._session_factory() as session:
            row = session.execute(
                select(activation_codes_table).where(activation_codes_table.c.id == code_id)
            ).first()
            return self._to_activation_code(row)

    def list_activation_codes(
        self,
        customer_id: UUID | None = None,
        status: ActivationCodeStatus | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Page[ActivationCode]:
        with self._session_factory() as session:
            base = select(activation_codes_table)
            count_q = select(func.count()).select_from(activation_codes_table)
            if customer_id is not None:
                base = base.where(activation_codes_table.c.customer_id == customer_id)
                count_q = count_q.where(activation_codes_table.c.customer_id == customer_id)
            if status is not None:
                base = base.where(activation_codes_table.c.status == str(status))
                count_q = count_q.where(activation_codes_table.c.status == str(status))
            total = session.execute(count_q).scalar_one()
            rows = session.execute(
                base.order_by(activation_codes_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            return self._page(
                [self._to_activation_code(r) for r in rows], int(total), page, per_page
            )

    def revoke_activation_code(
        self,
        code: str,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            row = session.execute(
                select(activation_codes_table).where(activation_codes_table.c.code == code)
            ).first()
            if row is None:
                raise BackofficeError(f"Activation code '{code}' not found")
            session.execute(
                update(activation_codes_table)
                .where(activation_codes_table.c.code == code)
                .values(status=str(ActivationCodeStatus.REVOKED))
            )
            self._record_audit(
                session,
                "activation_code.revoke",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="activation_code",
                object_id=code,
            )

    # --- licenses: issue / revoke / renew / upgrade (§4, §7) -----------------
    def _require_signer(self) -> TokenSigner:
        if self._signer is None:
            raise BackofficeError(
                "No license signing service configured (TokenSigner); issuance is disabled"
            )
        return self._signer

    def issue_license(
        self,
        request: LicenseIssueRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> LicenseStatus:
        """Signs a license token (via the signing port) and persists it, activating the customer."""
        signer = self._require_signer()
        now = self._clock()
        license_id = uuid4()
        payload = LicensePayload(
            license_id=str(license_id),
            client_id=str(request.tenant_id),
            plan=request.plan,
            mode=request.mode,
            issued_at=now,
            expires_at=request.expires_at,
            max_documents=request.max_documents,
            max_users=request.max_users,
            hardware_id=request.hardware_id,
            features=list(request.features),
        )
        token = signer.sign(payload)
        with self._session_factory() as session, session.begin():
            lic = orm.License(
                id=license_id,
                tenant_id=request.tenant_id,
                license_key=token,
                plan=str(request.plan),
                mode=str(request.mode),
                issued_at=now,
                expires_at=request.expires_at,
                max_documents=request.max_documents,
                max_users=request.max_users,
                is_revoked=False,
                hardware_id=request.hardware_id,
                features=[str(f) for f in request.features],
                issued_by=actor_user_id,
                customer_id=request.customer_id,
                created_at=now,
            )
            session.add(lic)
            # Issuance reflects in the customer status (DoD): the customer becomes ACTIVE.
            self._set_customer_status(session, request.customer_id, CustomerStatus.ACTIVE)
            self._record_audit(
                session,
                "license.issue",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="license",
                object_id=str(license_id),
                payload={
                    "customer_id": str(request.customer_id),
                    "tenant_id": str(request.tenant_id),
                    "plan": str(request.plan),
                },
            )
            session.flush()
            return self._license_to_status(lic)

    def revoke_license(
        self,
        request: LicenseRevokeRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> LicenseStatus:
        now = self._clock()
        with self._session_factory() as session, session.begin():
            lic = session.get(orm.License, request.license_id)
            if lic is None:
                raise BackofficeError(f"License {request.license_id} not found")
            lic.is_revoked = True
            lic.revoked_at = now
            lic.revocation_reason = request.reason
            # Revocation reflects in the customer status (DoD): suspend when no active license left.
            if lic.customer_id is not None and not self._has_active_license(
                session, lic.customer_id, exclude_id=lic.id
            ):
                self._set_customer_status(session, lic.customer_id, CustomerStatus.SUSPENDED)
            self._record_audit(
                session,
                "license.revoke",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="license",
                object_id=str(request.license_id),
                payload={"reason": request.reason},
            )
            session.flush()
            status = self._license_to_status(lic)
        # Publish to the signed CRL AFTER the transaction commits, so offline boxes converge.
        if self._crl_publisher is not None:
            self._crl_publisher(request.license_id, request.reason)
        return status

    def _has_active_license(
        self, session: Session, customer_id: UUID, exclude_id: UUID | None = None
    ) -> bool:
        now = self._clock()
        rows = session.execute(
            select(orm.License).where(
                orm.License.customer_id == customer_id,
                orm.License.is_revoked.is_(False),
            )
        ).scalars()
        for lic in rows:
            if exclude_id is not None and lic.id == exclude_id:
                continue
            exp = _as_aware(lic.expires_at)
            if exp is None or exp >= now:
                return True
        return False

    def renew_license(
        self,
        license_id: UUID,
        request: LicenseExtendRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> LicenseStatus:
        """Extends the expiry and re-signs the token (the signed token carries the new expiry)."""
        signer = self._require_signer()
        with self._session_factory() as session, session.begin():
            lic = session.get(orm.License, license_id)
            if lic is None:
                raise BackofficeError(f"License {license_id} not found")
            lic.expires_at = request.expires_at
            lic.is_revoked = False
            lic.revoked_at = None
            lic.revocation_reason = None
            lic.license_key = signer.sign(self._payload_from_license(lic))
            if lic.customer_id is not None:
                self._set_customer_status(session, lic.customer_id, CustomerStatus.ACTIVE)
            self._record_audit(
                session,
                "license.renew",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="license",
                object_id=str(license_id),
                payload={"expires_at": request.expires_at.isoformat()},
            )
            session.flush()
            return self._license_to_status(lic)

    def upgrade_license(
        self,
        license_id: UUID,
        request: LicenseUpgradeRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> LicenseStatus:
        signer = self._require_signer()
        with self._session_factory() as session, session.begin():
            lic = session.get(orm.License, license_id)
            if lic is None:
                raise BackofficeError(f"License {license_id} not found")
            lic.plan = str(request.plan)
            if request.max_documents is not None:
                lic.max_documents = request.max_documents
            if request.max_users is not None:
                lic.max_users = request.max_users
            if request.features is not None:
                lic.features = [str(f) for f in request.features]
            lic.license_key = signer.sign(self._payload_from_license(lic))
            self._record_audit(
                session,
                "license.upgrade",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="license",
                object_id=str(license_id),
                payload={"plan": str(request.plan)},
            )
            session.flush()
            return self._license_to_status(lic)

    @staticmethod
    def _payload_from_license(lic: orm.License) -> LicensePayload:
        from rag_core.contracts.enums import LicenseFeature

        return LicensePayload(
            license_id=str(lic.id),
            client_id=str(lic.tenant_id),
            plan=LicensePlan(lic.plan),
            mode=LicenseMode(lic.mode),
            issued_at=_as_aware(lic.issued_at) or _utcnow(),
            expires_at=_as_aware(lic.expires_at),
            max_documents=lic.max_documents,
            max_users=lic.max_users,
            hardware_id=lic.hardware_id,
            features=[LicenseFeature(f) for f in lic.features],
        )

    def list_licenses(
        self, customer_id: UUID | None = None, tenant_id: UUID | None = None
    ) -> list[LicenseStatus]:
        with self._session_factory() as session:
            q = select(orm.License)
            if customer_id is not None:
                q = q.where(orm.License.customer_id == customer_id)
            if tenant_id is not None:
                q = q.where(orm.License.tenant_id == tenant_id)
            rows = session.execute(q.order_by(orm.License.issued_at.desc())).scalars().all()
            return [self._license_to_status(lic) for lic in rows]

    # --- box deployments registry (§4) --------------------------------------
    def register_box(
        self,
        customer_id: UUID,
        *,
        tenant_id: UUID | None = None,
        image_version: str | None = None,
        hardware_id: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> BoxDeployment:
        now = self._clock()
        box_id = uuid4()
        with self._session_factory() as session, session.begin():
            session.execute(
                insert(box_deployments_table).values(
                    id=box_id,
                    customer_id=customer_id,
                    tenant_id=tenant_id,
                    image_version=image_version,
                    delivered_at=now,
                    hardware_id=hardware_id,
                    last_heartbeat_at=None,
                    grace_period_expires_at=None,
                    restricted_mode=False,
                    telemetry_opt_out=True,
                    observability_forward_enabled=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._record_audit(
                session,
                "box.register",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="box_deployment",
                object_id=str(box_id),
                payload={"customer_id": str(customer_id)},
            )
        with self._session_factory() as session:
            row = session.execute(
                select(box_deployments_table).where(box_deployments_table.c.id == box_id)
            ).first()
            return self._to_box(row)

    def list_boxes(
        self,
        customer_id: UUID | None = None,
        stale_hours: int | None = None,
        page: int = 1,
        per_page: int = 50,
    ) -> Page[BoxDeployment]:
        with self._session_factory() as session:
            base = select(box_deployments_table)
            count_q = select(func.count()).select_from(box_deployments_table)
            if customer_id is not None:
                base = base.where(box_deployments_table.c.customer_id == customer_id)
                count_q = count_q.where(box_deployments_table.c.customer_id == customer_id)
            if stale_hours is not None:
                stale_before = self._clock() - timedelta(hours=stale_hours)
                pattern = or_(
                    box_deployments_table.c.last_heartbeat_at.is_(None),
                    box_deployments_table.c.last_heartbeat_at < stale_before,
                )
                base = base.where(pattern)
                count_q = count_q.where(pattern)
            total = session.execute(count_q).scalar_one()
            rows = session.execute(
                base.order_by(box_deployments_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            return self._page([self._to_box(r) for r in rows], int(total), page, per_page)

    # --- break-glass impersonation (ALWAYS audited, §2 p.6, §4) --------------
    def grant_impersonation(
        self,
        backoffice_user_id: UUID,
        target_tenant_id: UUID,
        request: ImpersonationRequest,
        *,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> ImpersonationGrant:
        """Issues a short-lived impersonation grant. The audit record is MANDATORY and atomic."""
        if not request.reason or not request.reason.strip():
            raise BackofficeError("Impersonation requires a non-empty reason (break-glass audit)")
        expires_at = self._clock() + timedelta(seconds=request.ttl_seconds)
        with self._session_factory() as session, session.begin():
            self._record_audit(
                session,
                "impersonation.grant",
                actor_user_id=backoffice_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=target_tenant_id,
                object_type="tenant",
                object_id=str(target_tenant_id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"reason": request.reason, "ttl_seconds": request.ttl_seconds},
            )
        return ImpersonationGrant(
            backoffice_user_id=backoffice_user_id,
            target_tenant_id=target_tenant_id,
            reason=request.reason,
            expires_at=expires_at,
        )

    def end_impersonation(
        self,
        backoffice_user_id: UUID,
        target_tenant_id: UUID,
        *,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Logs the break-glass EXIT separately from the entry (`core/vendor_backoffice.md` §5)."""
        with self._session_factory() as session, session.begin():
            self._record_audit(
                session,
                "impersonation.end",
                actor_user_id=backoffice_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=target_tenant_id,
                object_type="tenant",
                object_id=str(target_tenant_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    def list_customer_tenants(self, customer_id: UUID) -> list[dict[str, Any]]:
        """Lists the tenants/installations a customer owns (via their issued licenses)."""
        with self._session_factory() as session:
            rows = (
                session.execute(
                    select(orm.License.tenant_id).where(orm.License.customer_id == customer_id)
                )
                .scalars()
                .all()
            )
            tenant_ids = {tid for tid in rows if tid is not None}
            if not tenant_ids:
                return []
            tenants = (
                session.execute(select(orm.Tenant).where(orm.Tenant.id.in_(tenant_ids)))
                .scalars()
                .all()
            )
            return [{"id": str(t.id), "slug": t.slug, "name": t.name} for t in tenants]

    def _customer_has_active_installations(self, session: Session, customer_id: UUID) -> bool:
        """True if the customer still owns at least one existing, active tenant installation.

        Used to decide churn on offboarding: with the ``1 customer : N tenants`` model a customer
        is churned only once their LAST active installation is gone (deepseek-criticism Stage N 2).
        """
        tenant_ids = {
            tid
            for tid in session.execute(
                select(orm.License.tenant_id).where(orm.License.customer_id == customer_id)
            )
            .scalars()
            .all()
            if tid is not None
        }
        if not tenant_ids:
            return False
        active = session.execute(
            select(func.count())
            .select_from(orm.Tenant)
            .where(orm.Tenant.id.in_(tenant_ids), orm.Tenant.is_active.is_(True))
        ).scalar_one()
        return int(active) > 0

    def list_audit_log(
        self,
        page: int = 1,
        per_page: int = 50,
        action: str | None = None,
        actor: str | None = None,
        tenant: UUID | None = None,
    ) -> Page[dict[str, Any]]:
        with self._session_factory() as session:
            base = select(backoffice_audit_log_table)
            count_q = select(func.count()).select_from(backoffice_audit_log_table)
            if action is not None:
                base = base.where(backoffice_audit_log_table.c.action == action)
                count_q = count_q.where(backoffice_audit_log_table.c.action == action)
            if actor is not None:
                base = base.where(backoffice_audit_log_table.c.actor_email == actor)
                count_q = count_q.where(backoffice_audit_log_table.c.actor_email == actor)
            if tenant is not None:
                base = base.where(backoffice_audit_log_table.c.impersonated_tenant_id == tenant)
                count_q = count_q.where(
                    backoffice_audit_log_table.c.impersonated_tenant_id == tenant
                )
            total = session.execute(count_q).scalar_one()
            rows = session.execute(
                base.order_by(backoffice_audit_log_table.c.created_at.desc())
                .limit(per_page)
                .offset((page - 1) * per_page)
            ).all()
            items = [dict(r._mapping) for r in rows]
            return self._page(items, int(total), page, per_page)

    # --- cross-tenant dashboard (§5) ----------------------------------------
    def get_dashboard(self, stale_heartbeat_hours: int = 48) -> VendorDashboard:
        now = self._clock()
        stale_before = now - timedelta(hours=stale_heartbeat_hours)
        with self._session_factory() as session:
            customers_total = session.execute(
                select(func.count()).select_from(customers_table)
            ).scalar_one()
            boxes_total = session.execute(
                select(func.count()).select_from(box_deployments_table)
            ).scalar_one()
            codes_pending = session.execute(
                select(func.count())
                .select_from(activation_codes_table)
                .where(activation_codes_table.c.status == str(ActivationCodeStatus.ISSUED))
            ).scalar_one()
            tenants_total = session.execute(
                select(func.count()).select_from(orm.Tenant)
            ).scalar_one()
            licenses_revoked = session.execute(
                select(func.count())
                .select_from(orm.License)
                .where(orm.License.is_revoked.is_(True))
            ).scalar_one()
            active_rows = (
                session.execute(select(orm.License).where(orm.License.is_revoked.is_(False)))
                .scalars()
                .all()
            )
            licenses_active = 0
            for lic in active_rows:
                exp = _as_aware(lic.expires_at)
                if exp is None or exp >= now:
                    licenses_active += 1
            box_rows = session.execute(select(box_deployments_table)).all()
            stale_boxes = 0
            for b in box_rows:
                hb = _as_aware(b.last_heartbeat_at)
                if hb is not None and hb < stale_before:
                    stale_boxes += 1
            return VendorDashboard(
                customers_total=int(customers_total),
                tenants_total=int(tenants_total),
                boxes_total=int(boxes_total),
                stale_boxes=int(stale_boxes),
                licenses_active=int(licenses_active),
                licenses_revoked=int(licenses_revoked),
                activation_codes_pending=int(codes_pending),
            )

    # --- SaaS tenant lifecycle (Stage N, IMPLEMENTATION_PLAN.md §N) ----------
    def provision_tenant(
        self,
        request: TenantProvisionRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> TenantProvisionResult:
        """Provisions an isolated SaaS tenant for an existing customer (commit-then-effects).

        The primary Postgres state (``tenants`` row RLS-isolated by ``tenant_id``, a signed
        license, the bootstrapped tenant admin and the customer->ACTIVE flip) is written in ONE
        DB transaction and COMMITTED first. Only then is the external Langfuse project provisioned
        - a dual-write hazard otherwise (a committed DB vs. an un-rolled-back external project).
        If the Langfuse hook fails, the already-committed tenant is COMPENSATED (hard-deleted) so
        the operation still has all-or-nothing semantics from the caller's point of view.
        """
        signer = self._require_signer()
        now = self._clock()
        tenant_id = uuid4()
        license_id = uuid4()
        admin_user_id = uuid4()
        s3_prefix = f"{tenant_id}/"
        payload = LicensePayload(
            license_id=str(license_id),
            client_id=str(tenant_id),
            plan=request.plan,
            mode=request.mode,
            issued_at=now,
            expires_at=request.expires_at,
            max_documents=request.max_documents,
            max_users=request.max_users,
            hardware_id=None,
            features=list(request.features),
        )
        token = signer.sign(payload)
        with self._session_factory() as session, session.begin():
            customer = session.execute(
                select(customers_table).where(customers_table.c.id == request.customer_id)
            ).first()
            if customer is None:
                raise BackofficeError(f"Customer {request.customer_id} not found")
            slug_taken = session.execute(
                select(orm.Tenant.id).where(orm.Tenant.slug == request.slug)
            ).first()
            if slug_taken is not None:
                raise BackofficeError(f"Tenant slug '{request.slug}' already exists")
            prior_customer_status = CustomerStatus(customer.status)
            session.add(
                orm.Tenant(
                    id=tenant_id,
                    slug=request.slug,
                    name=request.name,
                    is_active=True,
                    storage_type=request.storage_type,
                    s3_bucket=request.s3_bucket,
                    s3_prefix=s3_prefix,
                    allowed_origins=list(request.allowed_origins),
                    config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                orm.License(
                    id=license_id,
                    tenant_id=tenant_id,
                    license_key=token,
                    plan=str(request.plan),
                    mode=str(request.mode),
                    issued_at=now,
                    expires_at=request.expires_at,
                    max_documents=request.max_documents,
                    max_users=request.max_users,
                    is_revoked=False,
                    features=[str(f) for f in request.features],
                    issued_by=actor_user_id,
                    customer_id=request.customer_id,
                    created_at=now,
                )
            )
            session.add(
                orm.AdminUser(
                    id=admin_user_id,
                    tenant_id=tenant_id,
                    email=request.admin_email,
                    hashed_password=hash_password(request.admin_password),
                    role="admin",
                    is_active=True,
                    is_superuser=True,
                    is_verified=True,
                    created_at=now,
                    updated_at=now,
                )
            )
            # Provisioning activates the customer (DoD: lifecycle reflects in customer status).
            self._set_customer_status(session, request.customer_id, CustomerStatus.ACTIVE)
            self._record_audit(
                session,
                "tenant.provision",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=tenant_id,
                object_type="tenant",
                object_id=str(tenant_id),
                payload={
                    "customer_id": str(request.customer_id),
                    "slug": request.slug,
                    "license_id": str(license_id),
                },
            )
        # DB state is now committed. Run the external Langfuse side-effect AFTER commit and
        # compensate (hard-delete the committed tenant) if it fails, so the caller still observes
        # all-or-nothing semantics without leaving an orphaned Langfuse project behind.
        obs: ObservabilityConfig | None = None
        if self._langfuse_provisioner is not None:
            try:
                obs = self._langfuse_provisioner.provision(tenant_id, request.slug, request.name)
            except Exception:
                # Compensation: undo the committed DB state so the caller sees all-or-nothing
                # (hard-delete the tenant AND restore the customer's prior status). The rollback
                # itself is audited, so the audit trail never shows a bare "tenant.provision"
                # whose tenant was later compensated away.
                with self._session_factory() as session, session.begin():
                    self._hard_delete_tenant(session, tenant_id)
                    self._set_customer_status(session, request.customer_id, prior_customer_status)
                    self._record_audit(
                        session,
                        "tenant.provision.compensated",
                        actor_user_id=actor_user_id,
                        actor_email=actor_email,
                        impersonated_tenant_id=tenant_id,
                        object_type="tenant",
                        object_id=str(tenant_id),
                        payload={"customer_id": str(request.customer_id)},
                    )
                raise
            with self._session_factory() as session, session.begin():
                tenant = session.get(orm.Tenant, tenant_id)
                if tenant is not None:
                    tenant.config = {"observability": obs.model_dump(mode="json")}
                    tenant.updated_at = self._clock()
        return TenantProvisionResult(
            tenant_id=tenant_id,
            customer_id=request.customer_id,
            license_id=license_id,
            admin_user_id=admin_user_id,
            slug=request.slug,
            s3_prefix=s3_prefix,
            observability=obs,
        )

    def _snapshot_tenant(
        self, session: Session, tenant_id: UUID
    ) -> dict[str, list[dict[str, Any]]]:
        """Serialises every tenant-scoped row into a JSON-round-trippable snapshot."""
        snapshot: dict[str, list[dict[str, Any]]] = {}
        for model in _TENANT_RESTORE_ORDER:
            rows: list[Any]
            if model is orm.Message:
                conv_ids = (
                    session.execute(
                        select(orm.Conversation.id).where(orm.Conversation.tenant_id == tenant_id)
                    )
                    .scalars()
                    .all()
                )
                rows = (
                    list(
                        session.execute(
                            select(orm.Message).where(orm.Message.conversation_id.in_(conv_ids))
                        )
                        .scalars()
                        .all()
                    )
                    if conv_ids
                    else []
                )
            elif model is orm.Tenant:
                rows = list(
                    session.execute(select(orm.Tenant).where(orm.Tenant.id == tenant_id))
                    .scalars()
                    .all()
                )
            else:
                rows = list(
                    session.execute(select(model).where(model.tenant_id == tenant_id))
                    .scalars()
                    .all()
                )
            snapshot[model.__tablename__] = [serialize_row(r) for r in rows]
        return snapshot

    def _hard_delete_tenant(self, session: Session, tenant_id: UUID) -> None:
        """Deterministically deletes all tenant-scoped rows (children first), then the tenant."""
        conv_subq = select(orm.Conversation.id).where(orm.Conversation.tenant_id == tenant_id)
        for model in reversed(_TENANT_RESTORE_ORDER):
            if model is orm.Message:
                session.execute(
                    delete(orm.Message).where(orm.Message.conversation_id.in_(conv_subq))
                )
            elif model is orm.Tenant:
                session.execute(delete(orm.Tenant).where(orm.Tenant.id == tenant_id))
            else:
                session.execute(delete(model).where(model.tenant_id == tenant_id))

    def _restore_snapshot(
        self, session: Session, snapshot: dict[str, list[dict[str, Any]]]
    ) -> None:
        """Re-inserts a snapshot in FK-safe order (parents first)."""
        for model in _TENANT_RESTORE_ORDER:
            rows = snapshot.get(model.__tablename__, [])
            for row in rows:
                session.add(model(**deserialize_row(row)))
            session.flush()

    def suspend_tenant(
        self,
        tenant_id: UUID,
        *,
        suspend: bool = True,
        reason: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> None:
        """Pauses (or resumes) a tenant WITHOUT terminating it.

        A standalone, independently-audited suspend step (deepseek-criticism Stage N 8): toggles
        ``tenants.is_active`` so no new traffic is served, but keeps all data/objects intact.
        """
        with self._session_factory() as session, session.begin():
            tenant = session.get(orm.Tenant, tenant_id)
            if tenant is None:
                raise BackofficeError(f"Tenant {tenant_id} not found")
            tenant.is_active = not suspend
            tenant.updated_at = self._clock()
            self._record_audit(
                session,
                "tenant.suspend" if suspend else "tenant.resume",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=tenant_id,
                object_type="tenant",
                object_id=str(tenant_id),
                payload={"reason": reason} if reason else None,
            )

    def export_tenant(self, tenant_id: UUID) -> TenantExport:
        """Builds a full data export of a tenant (handed to the client before legal deletion)."""
        with self._session_factory() as session:
            tenant = session.get(orm.Tenant, tenant_id)
            if tenant is None:
                raise BackofficeError(f"Tenant {tenant_id} not found")
            snapshot = self._snapshot_tenant(session, tenant_id)
            return TenantExport(
                tenant_id=tenant_id,
                slug=tenant.slug,
                exported_at=self._clock(),
                data=snapshot,
                document_count=len(snapshot.get(orm.Document.__tablename__, [])),
            )

    def offboard_tenant(
        self,
        request: TenantOffboardRequest,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> TenantOffboardResult:
        """Offboards a tenant: suspend -> export -> legal hard-delete (DB + S3 + Langfuse).

        Leaves NO remnants in the primary contour; the audit entry survives in the SEPARATE
        ``backoffice_audit_log`` table (152-FZ/GDPR deletion receipt, `vendor_backoffice.md` §6).
        """
        if not request.reason or not request.reason.strip():
            raise BackofficeError("Offboarding requires a non-empty reason (legal audit trail)")
        export: TenantExport | None = None
        s3_bucket: str | None = None
        s3_prefix: str | None = None
        with self._session_factory() as session, session.begin():
            tenant = session.get(orm.Tenant, request.tenant_id)
            if tenant is None:
                raise BackofficeError(f"Tenant {request.tenant_id} not found")
            slug = tenant.slug
            s3_bucket = tenant.s3_bucket
            s3_prefix = tenant.s3_prefix
            # 1. Suspend immediately (so no new traffic is served during offboarding).
            tenant.is_active = False
            snapshot = self._snapshot_tenant(session, request.tenant_id)
            deleted_documents = len(snapshot.get(orm.Document.__tablename__, []))
            # 2. Export the data for the client (optional).
            if request.export_data:
                export = TenantExport(
                    tenant_id=request.tenant_id,
                    slug=slug,
                    exported_at=self._clock(),
                    data=snapshot,
                    document_count=deleted_documents,
                )
            customer_id = (
                session.execute(
                    select(orm.License.customer_id).where(
                        orm.License.tenant_id == request.tenant_id
                    )
                )
                .scalars()
                .first()
            )
            # 3. Legal hard-delete with cascade across all tenant-scoped tables.
            self._hard_delete_tenant(session, request.tenant_id)
            # Model is 1 customer : N tenants - churn the customer ONLY when this was their last
            # remaining active installation, otherwise terminating one tenant would wrongly churn
            # a still-paying customer (deepseek-criticism Stage N 2).
            customer_churned = False
            if customer_id is not None and not self._customer_has_active_installations(
                session, customer_id
            ):
                self._set_customer_status(session, customer_id, CustomerStatus.CHURNED)
                customer_churned = True
            self._record_audit(
                session,
                "tenant.offboard",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=request.tenant_id,
                object_type="tenant",
                object_id=str(request.tenant_id),
                payload={
                    "reason": request.reason,
                    "slug": slug,
                    "deleted_documents": deleted_documents,
                    "customer_churned": customer_churned,
                    "exported": request.export_data,
                },
            )
        # DB (incl. the surviving legal-deletion audit receipt) is now committed. Only then do we
        # run the irreversible external purge, so a DB-commit failure can never leave a live tenant
        # pointing at already-deleted objects (deepseek-criticism Stage N 1).
        storage_purged = False
        obs_deprovisioned = False
        purge_failures: list[str] = []
        if self._storage_purger is not None:
            try:
                self._storage_purger.purge_tenant_prefix(request.tenant_id, s3_bucket, s3_prefix)
                storage_purged = True
            except Exception as exc:  # noqa: BLE001
                purge_failures.append(f"storage: {exc}")
        if self._langfuse_provisioner is not None:
            try:
                self._langfuse_provisioner.deprovision(request.tenant_id)
                obs_deprovisioned = True
            except Exception as exc:  # noqa: BLE001
                purge_failures.append(f"langfuse: {exc}")
        if purge_failures:
            # The DB deletion (incl. the surviving receipt) is already committed and irreversible;
            # record the incomplete external purge durably so a retry job / operator can finish it
            # instead of silently leaving S3/Langfuse remnants behind (criticism Stage N 1).
            with self._session_factory() as session, session.begin():
                self._record_audit(
                    session,
                    "tenant.offboard.purge_failed",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    impersonated_tenant_id=request.tenant_id,
                    object_type="tenant",
                    object_id=str(request.tenant_id),
                    payload={"failures": purge_failures},
                )
            logger.error(
                "Tenant %s hard-deleted but external purge incomplete: %s",
                request.tenant_id,
                "; ".join(purge_failures),
            )
        return TenantOffboardResult(
            tenant_id=request.tenant_id,
            export=export,
            deleted_documents=deleted_documents,
            storage_purged=storage_purged,
            observability_deprovisioned=obs_deprovisioned,
        )

    def backup_tenant(
        self,
        tenant_id: UUID,
        *,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> TenantBackupResult:
        """Creates a verifiable backup of a tenant via the injected backup backend.

        The snapshot read, the backend write and the ``tenant.backup`` audit all happen inside a
        single session so a durable backup can never be left without its audit record
        (deepseek-criticism Stage N 10).
        """
        now = self._clock()
        with self._session_factory() as session, session.begin():
            tenant = session.get(orm.Tenant, tenant_id)
            if tenant is None:
                raise BackofficeError(f"Tenant {tenant_id} not found")
            snapshot = self._snapshot_tenant(session, tenant_id)
            document_count = len(snapshot.get(orm.Document.__tablename__, []))
            backup_ref = self._backup_backend.save(tenant_id, snapshot)
            self._record_audit(
                session,
                "tenant.backup",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=tenant_id,
                object_type="tenant",
                object_id=str(tenant_id),
                payload={"backup_ref": backup_ref, "document_count": document_count},
            )
        return TenantBackupResult(
            tenant_id=tenant_id,
            backup_ref=backup_ref,
            created_at=now,
            document_count=document_count,
        )

    def restore_tenant(
        self,
        backup_ref: str,
        *,
        overwrite: bool = False,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> TenantRestoreResult:
        """Restores a tenant from a backup, re-establishing the tenant's DB rows.

        Restoring over a tenant that still exists is REFUSED unless ``overwrite=True`` is passed
        explicitly, so an older backup can never silently destroy newer live data
        (deepseek-criticism Stage N 12). Scope is DB-only: ``source_uri`` strings are restored but
        S3 objects / the Langfuse project are NOT re-created (see ``core/vendor_backoffice.md`` §5).
        """
        snapshot = self._backup_backend.load(backup_ref)
        tenant_rows = snapshot.get(orm.Tenant.__tablename__, [])
        if not tenant_rows:
            raise BackofficeError(f"Backup {backup_ref} contains no tenant row")
        tenant_id = deserialize_row(tenant_rows[0])["id"]
        with self._session_factory() as session, session.begin():
            if session.get(orm.Tenant, tenant_id) is not None:
                if not overwrite:
                    raise BackofficeError(
                        f"Tenant {tenant_id} already exists; pass overwrite=True to replace it"
                    )
                # Explicit overwrite: wipe the existing tenant before re-inserting the backup.
                self._hard_delete_tenant(session, tenant_id)
            self._restore_snapshot(session, snapshot)
            restored_documents = len(snapshot.get(orm.Document.__tablename__, []))
            self._record_audit(
                session,
                "tenant.restore",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                impersonated_tenant_id=tenant_id,
                object_type="tenant",
                object_id=str(tenant_id),
                payload={"backup_ref": backup_ref, "restored_documents": restored_documents},
            )
        return TenantRestoreResult(tenant_id=tenant_id, restored_documents=restored_documents)
