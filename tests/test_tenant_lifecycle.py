"""Stage N - SaaS tenant lifecycle test suite (IMPLEMENTATION_PLAN.md §N).

Covers the Definition-of-Done test matrix for the tenant lifecycle:
1. Provisioning creates an ISOLATED tenant (data of two provisioned tenants never overlaps).
2. Provisioning is ATOMIC: a failing Langfuse hook rolls back the whole transaction.
3. Offboarding: suspend -> export -> legal hard-delete leaves NO remnants in DB/S3/Langfuse,
   churns the customer and still writes a surviving audit entry.
4. Backup/restore re-establishes full tenant integrity.
5. RBAC: the lifecycle capabilities are held by ``superadmin`` ONLY.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from rag_core.backoffice.auth import (
    BackofficeCapability,
    check_backoffice_capability,
)
from rag_core.backoffice.lifecycle import InMemoryTenantBackupBackend
from rag_core.backoffice.schema import backoffice_audit_log_table, create_backoffice_schema
from rag_core.backoffice.service import BackofficeError, BackofficeService
from rag_core.contracts.enums import BackofficeRole, CustomerStatus
from rag_core.contracts.models import (
    CustomerCreateRequest,
    ObservabilityConfig,
    TenantOffboardRequest,
    TenantProvisionRequest,
)
from rag_core.contracts.settings import Settings
from rag_core.db import models as orm
from rag_core.db.models import Base
from rag_core.licensing.tokens import Ed25519TokenSigner, generate_keypair

SECRET = "b" * 48


class FakeLangfuse:
    """Records provisioning/deprovisioning calls for one-project-per-tenant assertions."""

    def __init__(self, *, fail: bool = False, fail_deprovision: bool = False) -> None:
        self.fail = fail
        self.fail_deprovision = fail_deprovision
        self.provisioned: set[UUID] = set()
        self.deprovisioned: set[UUID] = set()

    def provision(self, tenant_id: UUID, slug: str, name: str) -> ObservabilityConfig:
        if self.fail:
            raise RuntimeError("langfuse unavailable")
        self.provisioned.add(tenant_id)
        return ObservabilityConfig(
            url=f"https://langfuse.local/project/{slug}",
            username=f"obs-{slug}@vendor.local",
            provisioned=True,
        )

    def deprovision(self, tenant_id: UUID) -> None:
        if self.fail_deprovision:
            raise RuntimeError("langfuse deprovision failed")
        self.deprovisioned.add(tenant_id)


class FakeStorage:
    """Records purged per-tenant prefixes (object-storage offboarding)."""

    def __init__(self, *, fail_purge: bool = False) -> None:
        self.fail_purge = fail_purge
        self.purged: list[tuple[UUID, str | None, str | None]] = []

    def purge_tenant_prefix(self, tenant_id: UUID, bucket: str | None, prefix: str | None) -> None:
        if self.fail_purge:
            raise RuntimeError("storage purge failed")
        self.purged.append((tenant_id, bucket, prefix))


@pytest.fixture
def sm(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'life.db'}")
    Base.metadata.create_all(engine)
    create_backoffice_schema(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def signer() -> Ed25519TokenSigner:
    kp = generate_keypair("test-kid")
    return Ed25519TokenSigner(private_seed=kp.private_seed, kid=kp.kid)


@pytest.fixture
def langfuse() -> FakeLangfuse:
    return FakeLangfuse()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def service(
    sm: sessionmaker[Session],
    signer: Ed25519TokenSigner,
    langfuse: FakeLangfuse,
    storage: FakeStorage,
) -> BackofficeService:
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False)
    return BackofficeService(
        session_factory=sm,
        settings=settings,
        signer=signer,
        langfuse_provisioner=langfuse,
        storage_purger=storage,
        backup_backend=InMemoryTenantBackupBackend(),
    )


def _new_customer(service: BackofficeService) -> UUID:
    customer = service.create_customer(
        CustomerCreateRequest(legal_name=f"ACME {uuid4().hex[:6]}", contact_email="c@x.io")
    )
    return customer.id


def _provision(service: BackofficeService, customer_id: UUID, slug: str) -> UUID:
    result = service.provision_tenant(
        TenantProvisionRequest(
            customer_id=customer_id,
            slug=slug,
            name=slug.upper(),
            admin_email=f"admin-{slug}@acme.io",
            admin_password="S3cret-pass-123",
        )
    )
    return result.tenant_id


def _add_document(sm: sessionmaker[Session], tenant_id: UUID, name: str) -> UUID:
    doc_id = uuid4()
    with sm() as session, session.begin():
        session.add(
            orm.Document(
                id=doc_id,
                tenant_id=tenant_id,
                source_type="s3",
                source_uri=f"s3://{tenant_id}/{name}",
                content_hash=uuid4().hex,
                access_level=1,
                status="indexed",
            )
        )
    return doc_id


# =============================================================================
# 1. Provisioning: isolation + atomicity + side effects
# =============================================================================
def test_provision_creates_isolated_tenant(
    service: BackofficeService, sm: sessionmaker[Session], langfuse: FakeLangfuse
) -> None:
    customer_id = _new_customer(service)
    result = service.provision_tenant(
        TenantProvisionRequest(
            customer_id=customer_id,
            slug="acme-eu",
            name="ACME EU",
            admin_email="owner@acme.io",
            admin_password="S3cret-pass-123",
        )
    )
    with sm() as session:
        tenant = session.get(orm.Tenant, result.tenant_id)
        assert tenant is not None and tenant.is_active
        assert tenant.s3_prefix == f"{result.tenant_id}/"  # per-tenant isolation prefix
        lic = session.get(orm.License, result.license_id)
        assert lic is not None and lic.tenant_id == result.tenant_id
        admin = session.get(orm.AdminUser, result.admin_user_id)
        assert admin is not None and admin.email == "owner@acme.io" and admin.role == "admin"
    # Langfuse project provisioned (one per tenant) and recorded in tenant.config.
    assert result.tenant_id in langfuse.provisioned
    assert result.observability is not None and result.observability.provisioned
    # Provisioning activates the owning customer.
    customer = service.get_customer(customer_id)
    assert customer is not None and customer.status is CustomerStatus.ACTIVE


def test_provisioned_tenants_do_not_overlap(
    service: BackofficeService, sm: sessionmaker[Session]
) -> None:
    c1, c2 = _new_customer(service), _new_customer(service)
    t1 = _provision(service, c1, "tenant-a")
    t2 = _provision(service, c2, "tenant-b")
    _add_document(sm, t1, "doc-a")
    _add_document(sm, t2, "doc-b")

    export_a = service.export_tenant(t1)
    export_b = service.export_tenant(t2)
    uris_a = {row["source_uri"] for row in export_a.data["documents"]}
    uris_b = {row["source_uri"] for row in export_b.data["documents"]}
    assert uris_a == {f"s3://{t1}/doc-a"}
    assert uris_b == {f"s3://{t2}/doc-b"}
    assert uris_a.isdisjoint(uris_b)  # no cross-tenant leakage


def test_provision_duplicate_slug_rejected(service: BackofficeService) -> None:
    customer_id = _new_customer(service)
    _provision(service, customer_id, "dupe")
    with pytest.raises(BackofficeError):
        _provision(service, customer_id, "dupe")


def test_provision_is_atomic_on_langfuse_failure(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False)
    bad_service = BackofficeService(
        session_factory=sm,
        settings=settings,
        signer=signer,
        langfuse_provisioner=FakeLangfuse(fail=True),
    )
    customer_id = _new_customer(bad_service)
    with pytest.raises(RuntimeError):
        _provision(bad_service, customer_id, "rollback")
    # Nothing must have been persisted (full rollback).
    with sm() as session:
        assert session.execute(select(func.count()).select_from(orm.Tenant)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(orm.License)).scalar_one() == 0
        assert session.execute(select(func.count()).select_from(orm.AdminUser)).scalar_one() == 0
    # Customer stays TRIAL (was never activated).
    customer = bad_service.get_customer(customer_id)
    assert customer is not None and customer.status is CustomerStatus.TRIAL


# =============================================================================
# 2. Offboarding: suspend -> export -> legal hard-delete (no remnants)
# =============================================================================
def test_offboarding_leaves_no_remnants(
    service: BackofficeService,
    sm: sessionmaker[Session],
    langfuse: FakeLangfuse,
    storage: FakeStorage,
) -> None:
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "to-churn")
    _add_document(sm, tenant_id, "secret.pdf")
    # Add a conversation + message to exercise the conversation/message cascade.
    with sm() as session, session.begin():
        conv_id = uuid4()
        session.add(orm.Conversation(id=conv_id, tenant_id=tenant_id, user_id="u1", channel="web"))
        session.add(orm.Message(id=uuid4(), conversation_id=conv_id, role="user", content="hi"))

    result = service.offboard_tenant(
        TenantOffboardRequest(tenant_id=tenant_id, reason="contract terminated")
    )
    assert result.deleted_documents == 1
    assert result.storage_purged and result.observability_deprovisioned
    assert result.export is not None and result.export.document_count == 1

    with sm() as session:
        # No remnants anywhere in the primary contour.
        assert session.get(orm.Tenant, tenant_id) is None
        for model in (orm.License, orm.AdminUser, orm.Document, orm.Conversation):
            left = session.execute(
                select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
            ).scalar_one()
            assert left == 0, model.__tablename__
        # Messages (scoped via conversation) are gone too.
        assert session.execute(select(func.count()).select_from(orm.Message)).scalar_one() == 0

    # External side effects were purged.
    assert tenant_id in langfuse.deprovisioned
    assert any(p[0] == tenant_id for p in storage.purged)
    # Customer churned.
    customer = service.get_customer(customer_id)
    assert customer is not None and customer.status is CustomerStatus.CHURNED
    # The audit entry SURVIVES in the separate backoffice_audit_log (legal deletion receipt).
    with sm() as session:
        audit = session.execute(
            select(backoffice_audit_log_table).where(
                backoffice_audit_log_table.c.action == "tenant.offboard"
            )
        ).first()
        assert audit is not None


def test_offboard_requires_reason(service: BackofficeService) -> None:
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "no-reason")
    with pytest.raises(BackofficeError):
        service.offboard_tenant(TenantOffboardRequest(tenant_id=tenant_id, reason="  "))


def test_offboard_churns_customer_only_when_last_installation_gone(
    service: BackofficeService,
) -> None:
    """1 customer : N tenants - churn only once the LAST active installation is removed."""
    customer_id = _new_customer(service)
    tenant_a = _provision(service, customer_id, "cust-eu")
    tenant_b = _provision(service, customer_id, "cust-us")

    # Offboard the first of two tenants: the customer must stay ACTIVE.
    service.offboard_tenant(TenantOffboardRequest(tenant_id=tenant_a, reason="downsize"))
    customer = service.get_customer(customer_id)
    assert customer is not None and customer.status is CustomerStatus.ACTIVE

    # Offboard the last tenant: now the customer is churned.
    service.offboard_tenant(TenantOffboardRequest(tenant_id=tenant_b, reason="terminated"))
    customer = service.get_customer(customer_id)
    assert customer is not None and customer.status is CustomerStatus.CHURNED


def test_offboard_purge_runs_after_commit(
    service: BackofficeService, sm: sessionmaker[Session], langfuse: FakeLangfuse
) -> None:
    """The external purge must run only after the DB (incl. the audit receipt) is committed."""
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "commit-first")
    service.offboard_tenant(TenantOffboardRequest(tenant_id=tenant_id, reason="done"))
    # DB committed (tenant gone + audit present) AND external effects ran.
    with sm() as session:
        assert session.get(orm.Tenant, tenant_id) is None
        assert (
            session.execute(
                select(func.count())
                .select_from(backoffice_audit_log_table)
                .where(backoffice_audit_log_table.c.action == "tenant.offboard")
            ).scalar_one()
            == 1
        )
    assert tenant_id in langfuse.deprovisioned


def test_offboard_records_purge_failure_but_keeps_receipt(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    """A failed post-commit purge must be durably recorded, not silently dropped (Stage N 1)."""
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False)
    flaky = BackofficeService(
        session_factory=sm,
        settings=settings,
        signer=signer,
        langfuse_provisioner=FakeLangfuse(),
        storage_purger=FakeStorage(fail_purge=True),
        backup_backend=InMemoryTenantBackupBackend(),
    )
    customer_id = _new_customer(flaky)
    tenant_id = _provision(flaky, customer_id, "purge-fails")

    result = flaky.offboard_tenant(TenantOffboardRequest(tenant_id=tenant_id, reason="gdpr"))

    # The DB deletion is committed (tenant gone) and the purge failure is surfaced on the receipt.
    assert result.storage_purged is False
    assert result.observability_deprovisioned is True
    with sm() as session:
        assert session.get(orm.Tenant, tenant_id) is None
        # The incomplete purge is durably recorded for a retry job / operator.
        purge_failed = session.execute(
            select(backoffice_audit_log_table).where(
                backoffice_audit_log_table.c.action == "tenant.offboard.purge_failed"
            )
        ).first()
        assert purge_failed is not None


def test_suspend_and_resume_tenant(service: BackofficeService, sm: sessionmaker[Session]) -> None:
    """Standalone suspend pauses a tenant without destroying it; resume reactivates it."""
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "pause-me")

    service.suspend_tenant(tenant_id, reason="billing hold")
    with sm() as session:
        tenant = session.get(orm.Tenant, tenant_id)
        assert tenant is not None and tenant.is_active is False
        assert (
            session.execute(
                select(func.count())
                .select_from(backoffice_audit_log_table)
                .where(backoffice_audit_log_table.c.action == "tenant.suspend")
            ).scalar_one()
            == 1
        )

    service.suspend_tenant(tenant_id, suspend=False)
    with sm() as session:
        tenant = session.get(orm.Tenant, tenant_id)
        assert tenant is not None and tenant.is_active is True


# =============================================================================
# 3. Backup / restore integrity
# =============================================================================
def test_backup_restore_roundtrip(service: BackofficeService, sm: sessionmaker[Session]) -> None:
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "backup-me")
    _add_document(sm, tenant_id, "report.docx")

    backup = service.backup_tenant(tenant_id)
    assert backup.document_count == 1

    # Destroy the tenant entirely (simulate disaster) then restore from the backup.
    service.offboard_tenant(
        TenantOffboardRequest(tenant_id=tenant_id, reason="simulate disaster", export_data=False)
    )
    with sm() as session:
        assert session.get(orm.Tenant, tenant_id) is None

    restored = service.restore_tenant(backup.backup_ref)
    assert restored.tenant_id == tenant_id
    assert restored.restored_documents == 1
    with sm() as session:
        tenant = session.get(orm.Tenant, tenant_id)
        assert tenant is not None and tenant.slug == "backup-me"
        docs = (
            session.execute(select(orm.Document).where(orm.Document.tenant_id == tenant_id))
            .scalars()
            .all()
        )
        assert [d.source_uri for d in docs] == [f"s3://{tenant_id}/report.docx"]
        lic_count = session.execute(
            select(func.count()).select_from(orm.License).where(orm.License.tenant_id == tenant_id)
        ).scalar_one()
        assert lic_count == 1  # license restored


def test_restore_unknown_backup_rejected(service: BackofficeService) -> None:
    with pytest.raises(KeyError):
        service.restore_tenant("backup:does-not-exist:0")


def test_restore_refuses_to_clobber_live_tenant_without_overwrite(
    service: BackofficeService,
) -> None:
    """Restoring over a still-existing tenant is refused unless overwrite=True is passed."""
    customer_id = _new_customer(service)
    tenant_id = _provision(service, customer_id, "live-one")
    backup = service.backup_tenant(tenant_id)

    # Tenant still exists -> a plain restore must refuse (no silent data loss).
    with pytest.raises(BackofficeError):
        service.restore_tenant(backup.backup_ref)

    # With overwrite=True it succeeds.
    restored = service.restore_tenant(backup.backup_ref, overwrite=True)
    assert restored.tenant_id == tenant_id


# =============================================================================
# 4. RBAC: lifecycle capabilities are superadmin-only
# =============================================================================
@pytest.mark.parametrize(
    "capability",
    [
        BackofficeCapability.TENANT_PROVISION,
        BackofficeCapability.TENANT_OFFBOARD,
        BackofficeCapability.TENANT_BACKUP,
    ],
)
def test_lifecycle_capabilities_superadmin_only(capability: BackofficeCapability) -> None:
    assert check_backoffice_capability(BackofficeRole.SUPERADMIN, capability)
    for role in (BackofficeRole.SUPPORT, BackofficeRole.FINANCE, BackofficeRole.SALES):
        assert not check_backoffice_capability(role, capability)
