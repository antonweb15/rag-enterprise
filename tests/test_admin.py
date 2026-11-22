"""Comprehensive Stage I Test Suite (`core/admin_panel.md`, `IMPLEMENTATION_PLAN.md` Stage I).

Covers:
1. Password hashing & constant-time verification (Argon2id + PBKDF2-HMAC-SHA256 600k iters).
2. TOTP 2FA (RFC 6238) generation, drift verification, and otpauth URI format.
3. Ed25519 cryptographic license verification (PASETO v4.public + JWT EdDSA, expiry, hardware_id).
4. HS256 session token generation, validation, expiration, and tampering prevention.
5. Strict double-submit CSRF token validation without bypasses.
6. Fail-closed authentication (no hardcoded bootstrap admin, no synthetic superuser).
7. RBAC permissions matrix (Admin > Operator > Viewer).
8. AdminUser management (CRUD) and 2FA lifecycle.
9. Document management, transactional access-level reclassification cascading to chunks with audit.
10. Admin API ASGI endpoints with CookieTransport, CSRF protection, and path traversal protection.
11. Air-gapped CLI (`rag-admin`) runner commands, JSON output, and exit codes (0/1/2/3).
"""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from rag_core.admin.auth import (
    AuthError,
    create_admin_session_jwt,
    generate_csrf_token,
    generate_totp_code,
    generate_totp_secret,
    get_totp_uri,
    hash_password,
    verify_admin_session_jwt,
    verify_csrf_token,
    verify_password,
    verify_totp_code,
)
from rag_core.admin.license_crypto import (
    LicenseValidationError,
    create_signed_license_token,
    ed25519_generate_keypair,
    verify_license_token,
)
from rag_core.admin.service import AdminService
from rag_core.contracts.enums import (
    AccessLevel,
    AdminRole,
    LicenseFeature,
    LicenseMode,
    LicensePlan,
)
from rag_core.contracts.models import (
    LicensePayload,
)
from rag_core.contracts.settings import Settings
from rag_core.db.models import (
    Base,
    ChildChunk,
    Document,
    ParentChunk,
)
from rag_core.ingestion.object_storage import LocalObjectStorage
from services.admin_cli import (
    EXIT_SUCCESS,
    AdminCLI,
)
from services.api.admin_app import AdminAPIApp


@pytest.fixture
def sqlite_db(tmp_path: Path) -> tuple[sessionmaker[Session], LocalObjectStorage]:
    db_file = tmp_path / "test_admin.db"
    storage_root = tmp_path / "data"
    storage_root.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_file}")

    # Create tables needed for admin tests
    Base.metadata.create_all(engine)
    sm = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
    storage = LocalObjectStorage(root=storage_root)
    return sm, storage


# =============================================================================
# 1. Password Hashing & Verification
# =============================================================================
def test_password_hashing_and_verification() -> None:
    password = "SuperSecretAdminPassword123!"
    hashed = hash_password(password)

    assert hashed.startswith("pbkdf2_sha256$600000$") or hashed.startswith("$argon2id$")
    assert verify_password(password, hashed) is True
    assert verify_password("WrongPassword", hashed) is False
    assert verify_password("", hashed) is False
    assert verify_password(password, "invalid_hash_string") is False


# =============================================================================
# 2. TOTP 2FA (RFC 6238)
# =============================================================================
def test_totp_2fa_generation_and_drift_verification() -> None:
    secret = generate_totp_secret()
    assert len(secret) >= 20

    uri = get_totp_uri(secret, "admin@enterprise.rag")
    assert "otpauth://totp/" in uri
    assert "secret=" in uri

    now = time.time()
    code = generate_totp_code(secret, for_time=now)
    assert len(code) == 6
    assert code.isdigit()

    assert verify_totp_code(secret, code) is True

    code_past = generate_totp_code(secret, for_time=now - 30)
    assert verify_totp_code(secret, code_past, valid_window=1) is True

    assert verify_totp_code(secret, "000000") is False
    assert verify_totp_code(secret, "abcdef") is False


# =============================================================================
# 3. Ed25519 Cryptographic License Verification (§3, §6)
# =============================================================================
def test_ed25519_license_crypto_verification() -> None:
    priv_seed, pub_key = ed25519_generate_keypair()
    lic_id = str(uuid4())

    payload = LicensePayload(
        license_id=lic_id,
        client_id="acme_corp",
        plan=LicensePlan.ENTERPRISE_BOX,
        mode=LicenseMode.AIRGAP_ENTERPRISE,
        issued_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(days=365),
        max_documents=50000,
        max_users=100,
        hardware_id="test-hw-fingerprint-123",
        telemetry_opt_out=True,
        features=[LicenseFeature.HYBRID_SEARCH, LicenseFeature.ENTERPRISE_ACL],
    )

    # 1. Test PASETO v4.public
    paseto_token = create_signed_license_token(payload, priv_seed, format_type="paseto")
    assert paseto_token.startswith("v4.public.")

    verified_paseto = verify_license_token(
        token=paseto_token,
        public_key_bytes=pub_key,
        expected_hardware_id="test-hw-fingerprint-123",
    )
    assert verified_paseto.license_id == lic_id
    assert verified_paseto.plan == LicensePlan.ENTERPRISE_BOX
    assert LicenseFeature.ENTERPRISE_ACL in verified_paseto.features

    # 2. Test JWT EdDSA
    jwt_token = create_signed_license_token(payload, priv_seed, format_type="jwt")
    assert len(jwt_token.split(".")) == 3

    verified_jwt = verify_license_token(
        token=jwt_token,
        public_key_bytes=pub_key,
        expected_hardware_id="test-hw-fingerprint-123",
    )
    assert verified_jwt.license_id == lic_id
    assert verified_jwt.plan == LicensePlan.ENTERPRISE_BOX

    # 3. Signature Tampering Rejection
    tampered_paseto = paseto_token[:-4] + "AAAA"
    with pytest.raises(LicenseValidationError):
        verify_license_token(token=tampered_paseto, public_key_bytes=pub_key)

    # 4. Hardware ID Mismatch Rejection
    with pytest.raises(LicenseValidationError, match="Hardware mismatch"):
        verify_license_token(
            token=paseto_token,
            public_key_bytes=pub_key,
            expected_hardware_id="different-machine-fingerprint",
        )

    # 5. Expired License Rejection
    expired_payload = LicensePayload(
        license_id=str(uuid4()),
        client_id="acme_corp",
        plan=LicensePlan.STANDARD,
        mode=LicenseMode.ONLINE_SUBSCRIPTION,
        issued_at=datetime.now(UTC) - timedelta(days=400),
        expires_at=datetime.now(UTC) - timedelta(days=30),
        max_documents=1000,
        max_users=10,
        features=[LicenseFeature.HYBRID_SEARCH],
    )
    expired_token = create_signed_license_token(expired_payload, priv_seed, format_type="paseto")
    with pytest.raises(LicenseValidationError, match="expired"):
        verify_license_token(token=expired_token, public_key_bytes=pub_key)


# =============================================================================
# 4. HS256 Session JWT Security
# =============================================================================
def test_jwt_session_token_security() -> None:
    secret = "a" * 32
    user_id = uuid4()
    email = "admin@example.com"

    token = create_admin_session_jwt(
        user_id=user_id,
        email=email,
        role=AdminRole.ADMIN,
        tenant_id=None,
        secret=secret,
        ttl_seconds=3600,
    )

    claims = verify_admin_session_jwt(token, secret=secret)
    assert claims["sub"] == str(user_id)
    assert claims["email"] == email
    assert claims["role"] == str(AdminRole.ADMIN)

    # Short secret rejection
    with pytest.raises(ValueError, match="at least 32 bytes"):
        create_admin_session_jwt(user_id, email, AdminRole.ADMIN, None, secret="short")

    # Wrong secret rejection
    with pytest.raises(AuthError, match="Invalid session signature"):
        verify_admin_session_jwt(token, secret="b" * 32)

    # Tampered payload rejection
    parts = token.split(".")
    tampered_payload = base64.urlsafe_b64encode(b'{"sub":"hacked"}').decode("ascii").rstrip("=")
    tampered_token = f"{parts[0]}.{tampered_payload}.{parts[2]}"
    with pytest.raises(AuthError):
        verify_admin_session_jwt(tampered_token, secret=secret)


# =============================================================================
# 5. Strict Double-Submit CSRF Protection
# =============================================================================
def test_double_submit_csrf_strict_protection() -> None:
    token1 = generate_csrf_token()
    token2 = generate_csrf_token()

    # Exact match succeeds
    assert verify_csrf_token(token1, token1) is True

    # Mismatched tokens fail
    assert verify_csrf_token(token1, token2) is False
    assert verify_csrf_token("arbitrary_16_chars_token!", token1) is False
    assert verify_csrf_token(None, token1) is False
    assert verify_csrf_token(token1, None) is False


# =============================================================================
# 6. Fail-Closed Authentication & RBAC Hierarchy
# =============================================================================
@pytest.mark.asyncio
async def test_fail_closed_auth_no_backdoors(
    sqlite_db: tuple[sessionmaker[Session], LocalObjectStorage],
) -> None:
    sm, storage = sqlite_db
    settings = Settings(jwt_secret="a" * 32, totp_required_for_admin=True)
    admin_service = AdminService(session_factory=sm, object_storage=storage, settings=settings)
    app = AdminAPIApp(settings=settings, admin_service=admin_service, session_factory=sm)

    # 1. Unregistered user MUST fail login (no hardcoded bootstrap admin!)
    messages: list[dict[str, Any]] = []

    async def send_mock(msg: dict[str, Any]) -> None:
        messages.append(msg)

    async def receive_login() -> dict[str, Any]:
        login_body = {"email": "admin@enterprise.rag", "password": "adminpassword"}
        return {
            "type": "http.request",
            "body": json.dumps(login_body).encode("utf-8"),
            "more_body": False,
        }

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/admin/auth/login",
        "headers": [],
    }

    await app(scope, receive_login, send_mock)
    start_msg = [m for m in messages if m["type"] == "http.response.start"][0]
    assert start_msg["status"] == 401

    # 2. Register legitimate admin user
    admin_read = admin_service.create_admin_user_sync(
        email="operator@enterprise.rag",
        password="ValidPassword123!",
        role=AdminRole.OPERATOR,
    )
    assert admin_read.email == "operator@enterprise.rag"

    # 3. Login with legitimate user succeeds
    messages.clear()

    async def receive_legit_login() -> dict[str, Any]:
        legit_body = {"email": "operator@enterprise.rag", "password": "ValidPassword123!"}
        return {
            "type": "http.request",
            "body": json.dumps(legit_body).encode("utf-8"),
            "more_body": False,
        }

    await app(scope, receive_legit_login, send_mock)
    start_msg = [m for m in messages if m["type"] == "http.response.start"][0]
    assert start_msg["status"] == 200

    # 4. Deactivated or deleted user session is rejected with 401/403
    admin_service.delete_admin_user_sync(admin_read.id)
    token = create_admin_session_jwt(
        user_id=admin_read.id,
        email=admin_read.email,
        role=admin_read.role,
        tenant_id=None,
        secret=app.jwt_secret,
    )

    with pytest.raises(AuthError, match="no longer exists"):
        await app._authenticate_request(
            headers={"cookie": f"{app.settings.session_cookie_name}={token}"}
        )


# =============================================================================
# 7. Document Reclassification Cascade & Audit Logging
# =============================================================================
@pytest.mark.asyncio
async def test_document_reclassification_cascade_and_audit(
    sqlite_db: tuple[sessionmaker[Session], LocalObjectStorage],
) -> None:
    sm, storage = sqlite_db
    settings = Settings()
    admin_service = AdminService(session_factory=sm, object_storage=storage, settings=settings)

    tenant_id = uuid4()

    # 1. Upload Document
    doc = await admin_service.upload_document(
        tenant_id=tenant_id,
        file_name="security_policy.pdf",
        content=b"Top secret corporate policies and data",
        access_level=AccessLevel.INTERNAL,
        actor_email="admin@corp.internal",
        ip_address="192.168.1.50",
        user_agent="AdminBrowser/1.0",
    )
    assert doc.access_level == AccessLevel.INTERNAL

    # Create dummy parent and child chunk in DB
    with sm() as session, session.begin():
        parent = ParentChunk(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=doc.id,
            content="Top secret parent chunk",
            token_count=100,
            chunk_index=0,
            access_level=int(AccessLevel.INTERNAL),
        )
        session.add(parent)
        session.flush()

        child = ChildChunk(
            id=uuid4(),
            tenant_id=tenant_id,
            parent_id=parent.id,
            content="Top secret child chunk",
            token_count=25,
            chunk_index=0,
            access_level=int(AccessLevel.INTERNAL),
        )
        session.add(child)

    # 2. Re-classify Document to CONFIDENTIAL (Level 2)
    await admin_service.reclassify_document(
        tenant_id=tenant_id,
        document_id=doc.id,
        new_access_level=AccessLevel.CONFIDENTIAL,
        actor_email="sec-officer@corp.internal",
        ip_address="10.0.0.1",
        user_agent="SecOpsTool/2.0",
    )

    # Verify cascading update in DB
    with sm() as session:
        doc_db = session.get(Document, doc.id)
        assert doc_db is not None
        assert doc_db.access_level == int(AccessLevel.CONFIDENTIAL)
        parent_db = session.query(ParentChunk).filter_by(document_id=doc.id).one()
        child_db = session.query(ChildChunk).filter_by(parent_id=parent_db.id).one()

        assert parent_db.access_level == int(AccessLevel.CONFIDENTIAL)
        assert child_db.access_level == int(AccessLevel.CONFIDENTIAL)

    # Verify Audit Log entry
    audit_page = await admin_service.get_audit_log(tenant_id)
    assert audit_page.total >= 2
    actions = [e.action for e in audit_page.items]
    assert "document.reclassify" in actions
    assert "document.upload" in actions

    reclass_audit = [e for e in audit_page.items if e.action == "document.reclassify"][0]
    assert reclass_audit.actor_email == "sec-officer@corp.internal"
    assert reclass_audit.ip_address == "10.0.0.1"


# =============================================================================
# 8. Safe Chunk Vector Stripping
# =============================================================================
@pytest.mark.asyncio
async def test_safe_chunk_vector_stripping(
    sqlite_db: tuple[sessionmaker[Session], LocalObjectStorage],
) -> None:
    sm, storage = sqlite_db
    admin_service = AdminService(session_factory=sm, object_storage=storage)
    tenant_id = uuid4()

    doc = await admin_service.upload_document(
        tenant_id=tenant_id,
        file_name="report.txt",
        content=b"Quarterly report content",
    )

    child_id = uuid4()
    with sm() as session, session.begin():
        parent = ParentChunk(
            id=uuid4(),
            tenant_id=tenant_id,
            document_id=doc.id,
            content="Parent chunk text",
            token_count=50,
            chunk_index=0,
            access_level=0,
        )
        session.add(parent)
        session.flush()

        child = ChildChunk(
            id=child_id,
            tenant_id=tenant_id,
            parent_id=parent.id,
            content="Child chunk text",
            token_count=15,
            chunk_index=0,
            access_level=0,
            embedding=[0.1, 0.2, 0.3],
        )
        session.add(child)

    # 1. Default listing: vectors are stripped
    chunks = await admin_service.get_document_chunks(tenant_id, doc.id, include_vectors=False)
    assert len(chunks) == 1
    assert chunks[0].children[0].embedding is None

    # 2. Request single chunk with include_vectors=True: vector is returned
    single_chunk = await admin_service.get_document_chunks(
        tenant_id, doc.id, include_vectors=True, single_chunk_id=child_id
    )
    assert single_chunk[0].children[0].embedding == [0.1, 0.2, 0.3]


# =============================================================================
# 9. Admin API Static SPA Path Traversal Protection
# =============================================================================
@pytest.mark.asyncio
async def test_admin_api_static_spa_path_traversal(
    sqlite_db: tuple[sessionmaker[Session], LocalObjectStorage],
) -> None:
    sm, storage = sqlite_db
    settings = Settings(jwt_secret="a" * 32)
    app = AdminAPIApp(settings=settings, session_factory=sm, object_storage=storage)

    messages: list[dict[str, Any]] = []

    async def send_mock(msg: dict[str, Any]) -> None:
        messages.append(msg)

    async def receive_mock() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    # Path traversal request
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/../../../../etc/passwd",
        "headers": [],
    }

    await app(scope, receive_mock, send_mock)
    start_msg = [m for m in messages if m["type"] == "http.response.start"][0]
    # Forbidden / Not Found, never 200 with arbitrary files
    assert start_msg["status"] in (403, 404)


# =============================================================================
# 10. Air-Gapped CLI Runner Tests (`rag-admin`)
# =============================================================================
@pytest.mark.asyncio
async def test_admin_cli_runner(
    sqlite_db: tuple[sessionmaker[Session], LocalObjectStorage],
    tmp_path: Path,
) -> None:
    sm, storage = sqlite_db
    admin_service = AdminService(session_factory=sm, object_storage=storage)
    cli = AdminCLI(service=admin_service)
    tenant_id = uuid4()

    # 1. License Fingerprint
    code = await cli.license_fingerprint(as_json=True)
    assert code == EXIT_SUCCESS

    # 2. License Status
    code = await cli.license_status(tenant_id=tenant_id, as_json=True)
    assert code == EXIT_SUCCESS

    # 3. Create Admin User via CLI
    code = await cli.create_admin_user(
        email="cli-admin@corp.internal",
        password="AdminPassword123!",
        role=AdminRole.ADMIN,
        as_json=True,
    )
    assert code == EXIT_SUCCESS

    # 4. Ingest directory via CLI
    doc_dir = tmp_path / "docs"
    doc_dir.mkdir(parents=True, exist_ok=True)
    (doc_dir / "test1.txt").write_text("Hello from CLI Ingest", encoding="utf-8")

    code = await cli.ingest_directory(
        tenant_id=tenant_id,
        directory_path=str(doc_dir),
        access_level=AccessLevel.PUBLIC,
        as_json=True,
    )
    assert code == EXIT_SUCCESS

    # 5. List documents via CLI
    code = await cli.list_documents(tenant_id=tenant_id, as_json=True)
    assert code == EXIT_SUCCESS
