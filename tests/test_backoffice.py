"""Stage L — Vendor Backoffice test suite (`core/vendor_backoffice.md`, IMPLEMENTATION_PLAN.md §L).

Covers:
1. Backoffice RBAC capability matrix (superadmin > sales/finance/support) and role isolation.
2. Contour isolation: a backoffice session JWT is NEVER accepted by the tenant admin contour
   (and vice versa) thanks to the ``ctx`` claim.
3. License issuance/revocation reflected in the customer status (ACTIVE <-> SUSPENDED).
4. Break-glass impersonation is ALWAYS audited (mandatory, atomic) and refuses an empty reason.
5. Backoffice ASGI endpoints: login/CSRF, capability enforcement (403), isolation from admin cookie.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from rag_core.admin.auth import (
    AuthError,
    RBACPermissionDeniedError,
    verify_admin_session_jwt,
)
from rag_core.backoffice.auth import (
    BackofficeCapability,
    check_backoffice_capability,
    create_backoffice_session_jwt,
    enforce_backoffice_capability,
    role_capabilities,
    verify_backoffice_session_jwt,
)
from rag_core.backoffice.schema import backoffice_audit_log_table, create_backoffice_schema
from rag_core.backoffice.service import BackofficeError, BackofficeService
from rag_core.contracts.enums import (
    ActivationCodeStatus,
    BackofficeRole,
    CustomerStatus,
    LicenseMode,
    LicensePlan,
)
from rag_core.contracts.models import (
    ActivationCodeCreateRequest,
    CustomerCreateRequest,
    ImpersonationRequest,
    LicenseIssueRequest,
    LicenseRevokeRequest,
)
from rag_core.contracts.settings import Settings
from rag_core.db.models import Base, Tenant
from rag_core.licensing.tokens import Ed25519TokenSigner, generate_keypair
from services.api.backoffice_app import BACKOFFICE_COOKIE_NAME, BackofficeAPIApp

SECRET = "b" * 48


@pytest.fixture
def sm(tmp_path: Path) -> sessionmaker[Session]:
    engine = create_engine(f"sqlite:///{tmp_path / 'bo.db'}")
    Base.metadata.create_all(engine)
    create_backoffice_schema(engine)
    return sessionmaker(bind=engine, class_=Session, expire_on_commit=False)


@pytest.fixture
def signer() -> Ed25519TokenSigner:
    kp = generate_keypair("test-kid")
    return Ed25519TokenSigner(private_seed=kp.private_seed, kid=kp.kid)


@pytest.fixture
def service(sm: sessionmaker[Session], signer: Ed25519TokenSigner) -> BackofficeService:
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False)
    return BackofficeService(session_factory=sm, settings=settings, signer=signer)


def _make_tenant(sm: sessionmaker[Session]) -> Tenant:
    tenant = Tenant(id=uuid4(), slug=f"t-{uuid4().hex[:8]}", name="Acme")
    with sm() as session, session.begin():
        session.add(tenant)
    return tenant


# =============================================================================
# 1. RBAC capability matrix & role isolation
# =============================================================================
def test_backoffice_rbac_matrix() -> None:
    # superadmin holds every capability
    assert role_capabilities(BackofficeRole.SUPERADMIN) == frozenset(BackofficeCapability)

    # sales can generate codes & write customers but MUST NOT issue licenses
    assert check_backoffice_capability(BackofficeRole.SALES, BackofficeCapability.CODE_GENERATE)
    assert check_backoffice_capability(BackofficeRole.SALES, BackofficeCapability.CUSTOMER_WRITE)
    assert not check_backoffice_capability(BackofficeRole.SALES, BackofficeCapability.LICENSE_ISSUE)

    # finance can renew/upgrade but not revoke or generate codes
    assert check_backoffice_capability(BackofficeRole.FINANCE, BackofficeCapability.LICENSE_RENEW)
    assert not check_backoffice_capability(
        BackofficeRole.FINANCE, BackofficeCapability.LICENSE_REVOKE
    )
    assert not check_backoffice_capability(
        BackofficeRole.FINANCE, BackofficeCapability.CODE_GENERATE
    )

    # support is read-only + impersonation; cannot write customers
    assert check_backoffice_capability(BackofficeRole.SUPPORT, BackofficeCapability.IMPERSONATE)
    assert not check_backoffice_capability(
        BackofficeRole.SUPPORT, BackofficeCapability.CUSTOMER_WRITE
    )

    # Only superadmin manages backoffice users
    assert check_backoffice_capability(BackofficeRole.SUPERADMIN, BackofficeCapability.USER_MANAGE)
    for role in (BackofficeRole.SALES, BackofficeRole.FINANCE, BackofficeRole.SUPPORT):
        assert not check_backoffice_capability(role, BackofficeCapability.USER_MANAGE)


def test_enforce_capability_raises_for_inactive_and_missing() -> None:
    with pytest.raises(RBACPermissionDeniedError):
        enforce_backoffice_capability(BackofficeRole.SALES, BackofficeCapability.LICENSE_ISSUE)
    with pytest.raises(RBACPermissionDeniedError):
        enforce_backoffice_capability(
            BackofficeRole.SUPERADMIN, BackofficeCapability.DASHBOARD_VIEW, is_active=False
        )


# =============================================================================
# 2. Contour isolation (backoffice JWT must NOT be a valid admin session)
# =============================================================================
def test_session_contour_isolation() -> None:
    bo_token = create_backoffice_session_jwt(
        user_id=uuid4(), email="vendor@rag", role=BackofficeRole.SUPERADMIN, secret=SECRET
    )
    # a backoffice token is accepted only by the backoffice verifier
    payload = verify_backoffice_session_jwt(bo_token, secret=SECRET)
    assert payload["ctx"] == "backoffice"
    # ...and REJECTED by the tenant admin verifier (different context)
    with pytest.raises(AuthError):
        verify_admin_session_jwt(bo_token, secret=SECRET)


# =============================================================================
# 3. License issuance/revocation reflected in the customer status
# =============================================================================
def test_issue_and_revoke_license_reflects_customer_status(
    service: BackofficeService, sm: sessionmaker[Session]
) -> None:
    tenant = _make_tenant(sm)
    customer = service.create_customer(
        CustomerCreateRequest(legal_name="Acme LLC", contact_email="buyer@acme.test")
    )
    assert customer.status == CustomerStatus.TRIAL

    status = service.issue_license(
        LicenseIssueRequest(
            customer_id=customer.id,
            tenant_id=tenant.id,
            plan=LicensePlan.PRO,
            mode=LicenseMode.ONLINE_SUBSCRIPTION,
            expires_at=datetime.now(UTC) + timedelta(days=365),
        ),
        actor_email="vendor@rag",
    )
    assert status.is_valid is True
    assert status.plan == LicensePlan.PRO
    # issuance -> customer ACTIVE
    assert service.get_customer(customer.id).status == CustomerStatus.ACTIVE  # type: ignore[union-attr]

    licenses = service.list_licenses(customer_id=customer.id)
    assert len(licenses) == 1

    # Revoke the only license -> customer SUSPENDED
    license_id = _only_license_id(sm)
    revoked = service.revoke_license(
        LicenseRevokeRequest(license_id=license_id, reason="non-payment"),
        actor_email="vendor@rag",
    )
    assert revoked.is_valid is False
    assert service.get_customer(customer.id).status == CustomerStatus.SUSPENDED  # type: ignore[union-attr]

    # Both the issue and the revoke are audited
    actions = _audit_actions(sm)
    assert "license.issue" in actions
    assert "license.revoke" in actions


def _only_license_id(sm: sessionmaker[Session]) -> Any:
    from rag_core.db.models import License

    with sm() as session:
        return session.execute(select(License.id)).scalar_one()


def _audit_actions(sm: sessionmaker[Session]) -> list[str]:
    with sm() as session:
        rows = session.execute(select(backoffice_audit_log_table.c.action)).all()
        return [r[0] for r in rows]


# =============================================================================
# 4. Break-glass impersonation is ALWAYS audited
# =============================================================================
def test_impersonation_is_mandatorily_audited(
    service: BackofficeService, sm: sessionmaker[Session]
) -> None:
    actor_id = uuid4()
    target_tenant = uuid4()
    grant = service.grant_impersonation(
        backoffice_user_id=actor_id,
        target_tenant_id=target_tenant,
        request=ImpersonationRequest(reason="customer escalation #42", ttl_seconds=600),
        actor_email="support@rag",
        ip_address="10.0.0.5",
    )
    assert grant.target_tenant_id == target_tenant
    assert grant.expires_at > datetime.now(UTC)

    with sm() as session:
        row = session.execute(
            select(backoffice_audit_log_table).where(
                backoffice_audit_log_table.c.action == "impersonation.grant"
            )
        ).first()
    assert row is not None
    assert row.impersonated_tenant_id == target_tenant
    assert row.actor_backoffice_user_id == actor_id
    assert row.payload["reason"] == "customer escalation #42"


def test_impersonation_requires_reason(service: BackofficeService) -> None:
    with pytest.raises(BackofficeError):
        service.grant_impersonation(
            backoffice_user_id=uuid4(),
            target_tenant_id=uuid4(),
            request=ImpersonationRequest(reason="   ", ttl_seconds=600),
        )


# =============================================================================
# 5. Activation codes
# =============================================================================
def test_activation_code_lifecycle(service: BackofficeService) -> None:
    customer = service.create_customer(
        CustomerCreateRequest(legal_name="Beta", contact_email="b@beta.test")
    )
    code = service.generate_activation_code(
        ActivationCodeCreateRequest(customer_id=customer.id, plan=LicensePlan.ENTERPRISE_BOX)
    )
    assert code.code.startswith("RAG-ENT-")
    assert code.plan == LicensePlan.ENTERPRISE_BOX

    service.revoke_activation_code(code.code, actor_email="sales@rag")
    codes = service.list_activation_codes(customer_id=customer.id)
    assert codes.items[0].status.value == "revoked"


def test_list_filters_wire_spec_v5_query_params(service: BackofficeService) -> None:
    """§5 filters: customers ?status=&q=, orders/codes ?customer_id=&status=, boxes ?stale_hours."""
    alpha = service.create_customer(
        CustomerCreateRequest(
            legal_name="Alpha Corp", contact_email="a@alpha.test", status=CustomerStatus.ACTIVE
        )
    )
    beta = service.create_customer(
        CustomerCreateRequest(legal_name="Beta LLC", contact_email="b@beta.test")
    )

    # customers: status filter + case-insensitive q search
    assert [c.id for c in service.list_customers(status=CustomerStatus.ACTIVE).items] == [alpha.id]
    assert [c.id for c in service.list_customers(q="ALPHA").items] == [alpha.id]

    # orders: customer_id + status filters
    service.create_order(customer_id=alpha.id, plan=LicensePlan.PRO)
    service.create_order(customer_id=beta.id, plan=LicensePlan.STANDARD)
    assert len(service.list_orders(customer_id=beta.id).items) == 1
    assert len(service.list_orders(status="pending").items) == 2

    # activation codes: customer_id + status filters
    code = service.generate_activation_code(
        ActivationCodeCreateRequest(customer_id=alpha.id, plan=LicensePlan.PRO)
    )
    service.generate_activation_code(
        ActivationCodeCreateRequest(customer_id=beta.id, plan=LicensePlan.STANDARD)
    )
    assert len(service.list_activation_codes(customer_id=alpha.id).items) == 1
    assert len(service.list_activation_codes(status=ActivationCodeStatus.ISSUED).items) == 2
    service.revoke_activation_code(code.code)
    assert len(service.list_activation_codes(status=ActivationCodeStatus.REVOKED).items) == 1

    # boxes: stale_hours captures installations that have never heartbeated (or timed out)
    service.register_box(customer_id=alpha.id)
    assert len(service.list_boxes(stale_hours=48).items) == 1
    assert len(service.list_boxes(stale_hours=0).items) == 1


# =============================================================================
# 6. ASGI endpoints: login, capability enforcement, isolation
# =============================================================================
async def _call(
    app: BackofficeAPIApp,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, Any, list[Any]]:
    messages: list[dict[str, Any]] = []

    async def send(msg: dict[str, Any]) -> None:
        messages.append(msg)

    payload = json.dumps(body or {}).encode("utf-8")
    sent = {"done": False}

    async def receive() -> dict[str, Any]:
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [],
        "client": ("127.0.0.1", 9999),
    }
    await app(scope, receive, send)
    start = [m for m in messages if m["type"] == "http.response.start"][0]
    body_msg = [m for m in messages if m["type"] == "http.response.body"][0]
    try:
        parsed = json.loads(body_msg["body"].decode("utf-8"))
    except Exception:
        parsed = None
    return start["status"], parsed, start["headers"]


@pytest.mark.asyncio
async def test_backoffice_login_and_capability_enforcement(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False, csrf_enabled=True)
    service = BackofficeService(session_factory=sm, settings=settings, signer=signer)
    app = BackofficeAPIApp(settings=settings, service=service)

    # Seed a SALES backoffice user
    service.create_backoffice_user("sales@rag", "StrongPass123!", BackofficeRole.SALES)

    # Login
    status, resp, resp_headers = await _call(
        app,
        "POST",
        "/api/v1/backoffice/auth/login",
        {"email": "sales@rag", "password": "StrongPass123!"},
    )
    assert status == 200
    csrf = resp["csrf_token"]
    set_cookie = next(v.decode() for (k, v) in resp_headers if k == b"set-cookie")
    token = set_cookie.split(";")[0].split("=", 1)[1]
    cookie_header = (b"cookie", f"{BACKOFFICE_COOKIE_NAME}={token}".encode())
    csrf_header = (b"x-csrf-token", csrf.encode())

    # SALES can view the dashboard
    status, _, _ = await _call(app, "GET", "/api/v1/backoffice/dashboard", headers=[cookie_header])
    assert status == 200

    # SALES CANNOT issue a license -> 403
    status, _, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/licenses/issue",
        {"customer_id": str(uuid4()), "tenant_id": str(uuid4())},
        headers=[cookie_header, csrf_header],
    )
    assert status == 403

    # SALES can create a customer (capability granted)
    status, cust, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/customers",
        {"legal_name": "Gamma", "contact_email": "g@gamma.test"},
        headers=[cookie_header, csrf_header],
    )
    assert status == 201
    assert cust["legal_name"] == "Gamma"


@pytest.mark.asyncio
async def test_backoffice_requires_auth_and_csrf(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False, csrf_enabled=True)
    service = BackofficeService(session_factory=sm, settings=settings, signer=signer)
    app = BackofficeAPIApp(settings=settings, service=service)

    # No cookie -> 401
    status, _, _ = await _call(app, "GET", "/api/v1/backoffice/dashboard")
    assert status == 401

    # A tenant-admin-style token (wrong ctx) is rejected too
    from rag_core.admin.auth import create_admin_session_jwt
    from rag_core.contracts.enums import AdminRole

    admin_token = create_admin_session_jwt(
        user_id=uuid4(), email="a@rag", role=AdminRole.ADMIN, tenant_id=None, secret=SECRET
    )
    status, _, _ = await _call(
        app,
        "GET",
        "/api/v1/backoffice/dashboard",
        headers=[(b"cookie", f"{BACKOFFICE_COOKIE_NAME}={admin_token}".encode())],
    )
    assert status == 401

    # Logged-in superadmin without CSRF header -> 403 on POST
    service.create_backoffice_user("root@rag", "StrongPass123!", BackofficeRole.SUPERADMIN)
    _, resp, resp_headers = await _call(
        app,
        "POST",
        "/api/v1/backoffice/auth/login",
        {"email": "root@rag", "password": "StrongPass123!"},
    )
    set_cookie = next(v.decode() for (k, v) in resp_headers if k == b"set-cookie")
    token = set_cookie.split(";")[0].split("=", 1)[1]
    status, _, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/customers",
        {"legal_name": "NoCsrf", "contact_email": "n@n.test"},
        headers=[(b"cookie", f"{BACKOFFICE_COOKIE_NAME}={token}".encode())],
    )
    assert status == 403


@pytest.mark.asyncio
async def test_backoffice_endpoint_surface_matches_spec_v5(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    """The §5 endpoint surface: /licenses/revoke -> 204, /audit-log, /users, impersonation exit."""
    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=False, csrf_enabled=True)
    service = BackofficeService(session_factory=sm, settings=settings, signer=signer)
    app = BackofficeAPIApp(settings=settings, service=service)
    tenant = _make_tenant(sm)
    service.create_backoffice_user("root@rag", "StrongPass123!", BackofficeRole.SUPERADMIN)

    _, resp, resp_headers = await _call(
        app,
        "POST",
        "/api/v1/backoffice/auth/login",
        {"email": "root@rag", "password": "StrongPass123!"},
    )
    csrf = resp["csrf_token"]
    set_cookie = next(v.decode() for (k, v) in resp_headers if k == b"set-cookie")
    token = set_cookie.split(";")[0].split("=", 1)[1]
    cookie = (b"cookie", f"{BACKOFFICE_COOKIE_NAME}={token}".encode())
    csrf_header = (b"x-csrf-token", csrf.encode())

    # Create a customer + issue a license through the §5 paths
    _, cust, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/customers",
        {"legal_name": "Spec", "contact_email": "s@spec.test"},
        headers=[cookie, csrf_header],
    )
    status, issued, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/licenses/issue",
        {
            "customer_id": cust["id"],
            "tenant_id": str(tenant.id),
            "plan": "pro",
            "mode": "online_subscription",
            "expires_at": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        headers=[cookie, csrf_header],
    )
    assert status == 201
    license_id = _only_license_id(sm)

    # §5: POST /licenses/revoke (body LicenseRevokeRequest) -> 204
    status, _, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/licenses/revoke",
        {"license_id": str(license_id), "reason": "non-payment"},
        headers=[cookie, csrf_header],
    )
    assert status == 204

    # §5: GET /box-deployments (renamed from /boxes)
    status, _, _ = await _call(app, "GET", "/api/v1/backoffice/box-deployments", headers=[cookie])
    assert status == 200

    # §5: GET /customers/{id}/tenants
    status, tenants, _ = await _call(
        app, "GET", f"/api/v1/backoffice/customers/{cust['id']}/tenants", headers=[cookie]
    )
    assert status == 200
    assert any(t["id"] == str(tenant.id) for t in tenants)

    # §5: break-glass entry + exit are logged separately
    status, _, _ = await _call(
        app,
        "POST",
        f"/api/v1/backoffice/tenants/{tenant.id}/impersonate",
        {"reason": "support #7", "ttl_seconds": 300},
        headers=[cookie, csrf_header],
    )
    assert status == 201
    status, _, _ = await _call(
        app,
        "DELETE",
        f"/api/v1/backoffice/tenants/{tenant.id}/impersonate",
        headers=[cookie, csrf_header],
    )
    assert status == 204

    # §5: GET /audit-log and POST /users (superadmin only)
    status, audit, _ = await _call(app, "GET", "/api/v1/backoffice/audit-log", headers=[cookie])
    assert status == 200
    actions = {item["action"] for item in audit["items"]}
    assert {"license.revoke", "impersonation.grant", "impersonation.end"} <= actions

    status, created, _ = await _call(
        app,
        "POST",
        "/api/v1/backoffice/users",
        {"email": "support@rag", "password": "StrongPass123!", "role": "support"},
        headers=[cookie, csrf_header],
    )
    assert status == 201
    assert created["email"] == "support@rag"


@pytest.mark.asyncio
async def test_totp_enrollment_is_reachable_when_required(
    sm: sessionmaker[Session], signer: Ed25519TokenSigner
) -> None:
    """A fresh account can enroll 2FA via a limited session even when TOTP is mandatory (§5)."""
    from rag_core.admin.auth import generate_totp_code

    settings = Settings(jwt_secret=SECRET, backoffice_totp_required=True, csrf_enabled=True)
    service = BackofficeService(session_factory=sm, settings=settings, signer=signer)
    app = BackofficeAPIApp(settings=settings, service=service)
    service.create_backoffice_user("new@rag", "StrongPass123!", BackofficeRole.SUPERADMIN)

    # Login with no secret yet -> limited (enrollment) session is granted, not rejected
    status, resp, resp_headers = await _call(
        app,
        "POST",
        "/api/v1/backoffice/auth/login",
        {"email": "new@rag", "password": "StrongPass123!"},
    )
    assert status == 200
    csrf = resp["csrf_token"]
    set_cookie = next(v.decode() for (k, v) in resp_headers if k == b"set-cookie")
    token = set_cookie.split(";")[0].split("=", 1)[1]
    cookie = (b"cookie", f"{BACKOFFICE_COOKIE_NAME}={token}".encode())
    csrf_header = (b"x-csrf-token", csrf.encode())

    # A guarded endpoint is blocked by the limited (mfa=False) session
    status, _, _ = await _call(app, "GET", "/api/v1/backoffice/dashboard", headers=[cookie])
    assert status == 403

    # ...but the enrollment endpoint is reachable
    status, setup, _ = await _call(
        app, "POST", "/api/v1/backoffice/auth/2fa/setup", headers=[cookie, csrf_header]
    )
    assert status == 200
    secret = setup["secret"]

    # Re-login WITH a valid code -> full session, guarded endpoint now works
    code = generate_totp_code(secret)
    _, _, resp_headers = await _call(
        app,
        "POST",
        "/api/v1/backoffice/auth/login",
        {"email": "new@rag", "password": "StrongPass123!", "totp_code": code},
    )
    set_cookie = next(v.decode() for (k, v) in resp_headers if k == b"set-cookie")
    token2 = set_cookie.split(";")[0].split("=", 1)[1]
    status, _, _ = await _call(
        app,
        "GET",
        "/api/v1/backoffice/dashboard",
        headers=[(b"cookie", f"{BACKOFFICE_COOKIE_NAME}={token2}".encode())],
    )
    assert status == 200
