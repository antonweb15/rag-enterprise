"""Admin panel backend service (`packages/rag_core/admin/service.py`).

Canonical specification - `core/admin_panel.md` §5, §5A, §6, §7, §8, §9, §10.
- All mutations write an `audit_log` entry in the same transaction with IP and User-Agent.
- Document re-classification transactionally updates documents, parent_chunks, and child_chunks.
- Live ingest statuses over Postgres LISTEN/NOTIFY with fallback polling.
- Chunk viewing with safe vector stripping (vectors only with include_vectors=True and chunk_id).
- Real Ed25519 license verification (PASETO v4.public / JWT EdDSA) and hardware binding.
- Full AdminUser management and 2FA TOTP lifecycle.
- Dual mode runtime config without forks.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import math
import os
import platform
import socket
import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Integer, delete, func, select, update
from sqlalchemy.orm import Session

from rag_core.admin.auth import (
    generate_totp_secret,
    get_totp_uri,
    hash_password,
    verify_totp_code,
)
from rag_core.admin.license_crypto import LicenseValidationError, verify_license_token
from rag_core.contracts.enums import (
    AccessLevel,
    AclPermission,
    AdminRole,
    ChannelType,
    DeploymentMode,
    DocumentStatus,
    LicenseMode,
    LicensePlan,
    PrincipalType,
)
from rag_core.contracts.interfaces import (
    BaseEmbedder,
    BaseObjectStorage,
    BaseRetriever,
)
from rag_core.contracts.models import (
    AdminConfig,
    AdminUserRead,
    AuditLogEntry,
    ChunkView,
    Collection,
    DashboardStats,
    DocumentListItem,
    Group,
    IngestQueueItem,
    LicenseActivationRequest,
    LicenseStatus,
    MachineFingerprint,
    Page,
    PlaygroundRetrieveResponse,
    RetrievalCandidate,
    TwoFactorSetupResponse,
    UserRead,
)
from rag_core.contracts.settings import Settings
from rag_core.db import models
from rag_core.ingestion.object_storage import LocalObjectStorage

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Session]


def _enum_str(v: Any) -> str:
    """Helper to get string value from an enum or string."""
    return str(v.value if hasattr(v, "value") else v)


class AdminService:
    """Core business logic service for Admin Panel and CLI."""

    def __init__(
        self,
        session_factory: SessionFactory,
        object_storage: BaseObjectStorage | None = None,
        settings: Settings | None = None,
        embedder: BaseEmbedder | None = None,
        retriever: BaseRetriever | None = None,
    ) -> None:
        self._session_factory = session_factory
        self.settings = settings or Settings()
        self.storage = object_storage or LocalObjectStorage(self.settings.object_storage_local_root)
        self.embedder = embedder
        self.retriever = retriever

    def _get_vendor_public_key(self) -> bytes:
        """Returns the configured Ed25519 public key, fail-closed if absent.

        There is intentionally NO hardcoded default key: falling back to a well-known public key
        would let anyone who holds the matching private key forge valid licenses.
        """
        if not self.settings.license_public_key:
            raise ValueError(
                "License public key is not configured (LICENSE_PUBLIC_KEY). "
                "License activation is disabled."
            )
        key_str = self.settings.license_public_key.strip()
        try:
            if len(key_str) == 64:
                return bytes.fromhex(key_str)
            return base64.b64decode(key_str)
        except Exception as exc:
            raise ValueError(f"Invalid LICENSE_PUBLIC_KEY (expected hex or base64): {exc}") from exc

    def _record_audit_sync(
        self,
        session: Session,
        tenant_id: UUID | None,
        action: str,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> models.AuditLog:
        audit_entry = models.AuditLog(
            id=uuid4(),
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            actor_email=actor_email,
            action=action,
            object_type=object_type,
            object_id=object_id,
            ip_address=ip_address,
            user_agent=user_agent,
            payload=payload or {},
            created_at=datetime.now(UTC),
        )
        session.add(audit_entry)
        return audit_entry

    def log_auth_event(
        self,
        tenant_id: UUID | None,
        actor_user_id: UUID | None,
        actor_email: str | None,
        event_type: str,
        ip_address: str | None = None,
        user_agent: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Records authentication events (login, logout, failed login) to audit log."""
        with self._session_factory() as session, session.begin():
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action=f"auth.{event_type}",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="auth",
                object_id=str(actor_user_id) if actor_user_id else actor_email,
                ip_address=ip_address,
                user_agent=user_agent,
                payload=payload,
            )

    # -------------------------------------------------------------------------
    # Admin User Management CRUD (§4, §13)
    # -------------------------------------------------------------------------
    def get_admin_user_sync(self, user_id: UUID) -> AdminUserRead | None:
        with self._session_factory() as session:
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                return None
            return AdminUserRead(
                id=u.id,
                tenant_id=u.tenant_id,
                email=u.email,
                role=AdminRole(u.role),
                is_active=u.is_active,
                is_superuser=u.is_superuser,
                is_verified=u.is_verified,
                totp_enabled=u.totp_enabled,
                created_at=u.created_at,
                updated_at=u.updated_at,
            )

    def get_admin_user_db_by_email_sync(self, email: str) -> models.AdminUser | None:
        with self._session_factory() as session:
            return session.execute(
                select(models.AdminUser).where(models.AdminUser.email == email.strip().lower())
            ).scalar_one_or_none()

    def list_admin_users_sync(
        self, tenant_id: UUID | None = None, page: int = 1, per_page: int = 50
    ) -> Page[AdminUserRead]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            stmt = select(models.AdminUser)
            if tenant_id is not None:
                stmt = stmt.where(
                    (models.AdminUser.tenant_id == tenant_id)
                    | (models.AdminUser.tenant_id.is_(None))
                )
            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            users = (
                session.execute(
                    stmt.order_by(models.AdminUser.created_at.desc()).offset(offset).limit(per_page)
                )
                .scalars()
                .all()
            )

            items = [
                AdminUserRead(
                    id=u.id,
                    tenant_id=u.tenant_id,
                    email=u.email,
                    role=AdminRole(u.role),
                    is_active=u.is_active,
                    is_superuser=u.is_superuser,
                    is_verified=u.is_verified,
                    totp_enabled=u.totp_enabled,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                )
                for u in users
            ]

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    def create_admin_user_sync(
        self,
        email: str,
        password: str,
        role: AdminRole = AdminRole.VIEWER,
        tenant_id: UUID | None = None,
        is_superuser: bool = False,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> AdminUserRead:
        clean_email = email.strip().lower()
        if not clean_email or "@" not in clean_email:
            raise ValueError("Invalid email address")
        if len(password) < 8:
            raise ValueError("Password must be at least 8 characters")

        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(models.AdminUser).where(models.AdminUser.email == clean_email)
            ).scalar_one_or_none()
            if existing:
                raise ValueError(f"Admin user with email '{clean_email}' already exists")

            now = datetime.now(UTC)
            new_user = models.AdminUser(
                id=uuid4(),
                tenant_id=tenant_id,
                email=clean_email,
                hashed_password=hash_password(password),
                role=_enum_str(role),
                is_active=True,
                is_superuser=is_superuser or role == AdminRole.ADMIN,
                is_verified=True,
                totp_secret=None,
                totp_enabled=False,
                created_at=now,
                updated_at=now,
            )
            session.add(new_user)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="admin_user.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="admin_user",
                object_id=str(new_user.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"email": clean_email, "role": new_user.role},
            )
            session.flush()

            return AdminUserRead(
                id=new_user.id,
                tenant_id=new_user.tenant_id,
                email=new_user.email,
                role=AdminRole(new_user.role),
                is_active=new_user.is_active,
                is_superuser=new_user.is_superuser,
                is_verified=new_user.is_verified,
                totp_enabled=new_user.totp_enabled,
                created_at=new_user.created_at,
                updated_at=new_user.updated_at,
            )

    def update_admin_user_sync(
        self,
        user_id: UUID,
        role: AdminRole | None = None,
        is_active: bool | None = None,
        password: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> AdminUserRead:
        with self._session_factory() as session, session.begin():
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                raise ValueError(f"Admin user {user_id} not found")

            if role is not None:
                u.role = _enum_str(role)
                if role == AdminRole.ADMIN:
                    u.is_superuser = True
            if is_active is not None:
                u.is_active = is_active
            if password is not None:
                if len(password) < 8:
                    raise ValueError("Password must be at least 8 characters")
                u.hashed_password = hash_password(password)

            u.updated_at = datetime.now(UTC)
            self._record_audit_sync(
                session=session,
                tenant_id=u.tenant_id,
                action="admin_user.update",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="admin_user",
                object_id=str(u.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"role": u.role, "is_active": u.is_active},
            )
            session.flush()

            return AdminUserRead(
                id=u.id,
                tenant_id=u.tenant_id,
                email=u.email,
                role=AdminRole(u.role),
                is_active=u.is_active,
                is_superuser=u.is_superuser,
                is_verified=u.is_verified,
                totp_enabled=u.totp_enabled,
                created_at=u.created_at,
                updated_at=u.updated_at,
            )

    def delete_admin_user_sync(
        self,
        user_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                raise ValueError(f"Admin user {user_id} not found")

            tenant_id = u.tenant_id
            session.delete(u)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="admin_user.delete",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="admin_user",
                object_id=str(user_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    def setup_admin_totp_sync(
        self,
        user_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> TwoFactorSetupResponse:
        with self._session_factory() as session, session.begin():
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                raise ValueError(f"Admin user {user_id} not found")

            secret = generate_totp_secret()
            u.totp_secret = secret
            u.updated_at = datetime.now(UTC)

            self._record_audit_sync(
                session=session,
                tenant_id=u.tenant_id,
                action="admin_user.2fa_setup_initiated",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="admin_user",
                object_id=str(u.id),
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.flush()

            uri = get_totp_uri(secret, email=u.email, issuer="Enterprise RAG Admin")
            return TwoFactorSetupResponse(secret=secret, otpauth_uri=uri)

    def verify_admin_totp_sync(
        self,
        user_id: UUID,
        code: str,
        secret_override: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> bool:
        with self._session_factory() as session, session.begin():
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                raise ValueError(f"Admin user {user_id} not found")

            secret = secret_override or u.totp_secret
            if not secret:
                raise ValueError("2FA is not set up on this account")

            valid = verify_totp_code(secret=secret, code=code)
            if valid:
                u.totp_secret = secret
                u.totp_enabled = True
                u.updated_at = datetime.now(UTC)
                self._record_audit_sync(
                    session=session,
                    tenant_id=u.tenant_id,
                    action="admin_user.2fa_enabled",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    object_type="admin_user",
                    object_id=str(u.id),
                    ip_address=ip_address,
                    user_agent=user_agent,
                )
                session.flush()
                return True
            return False

    def disable_admin_totp_sync(
        self,
        user_id: UUID,
        ip_address: str | None = None,
        user_agent: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            u = session.execute(
                select(models.AdminUser).where(models.AdminUser.id == user_id)
            ).scalar_one_or_none()
            if not u:
                raise ValueError(f"Admin user {user_id} not found")

            u.totp_secret = None
            u.totp_enabled = False
            u.updated_at = datetime.now(UTC)
            self._record_audit_sync(
                session=session,
                tenant_id=u.tenant_id,
                action="admin_user.2fa_disabled",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="admin_user",
                object_id=str(u.id),
                ip_address=ip_address,
                user_agent=user_agent,
            )
            session.flush()

    # -------------------------------------------------------------------------
    # Runtime Config (§3)
    # -------------------------------------------------------------------------
    async def get_config(self) -> AdminConfig:
        """Returns runtime config for SPA initialization."""
        is_cloud = self.settings.deployment_mode == DeploymentMode.SAAS_CLOUD
        features: list[str] = [
            "license_management",
            "s3_connector",
            "enterprise_acl",
            "collections",
        ]
        if not is_cloud:
            features.append("airgap_cli")
        if self.settings.langfuse_public_key:
            features.append("observability")

        branding = {
            "app_name": "Enterprise RAG Admin",
            "company_name": "RAG Enterprise",
            "logo_url": "/assets/logo.svg",
        }
        return AdminConfig(
            deployment_mode=self.settings.deployment_mode,
            features=features,
            branding=branding,
            api_version="v1",
        )

    # -------------------------------------------------------------------------
    # Documents CRUD & Cascading Re-classification (§5, §5A, §7)
    # -------------------------------------------------------------------------
    async def list_documents(
        self,
        tenant_id: UUID,
        page: int = 1,
        per_page: int = 20,
        status: DocumentStatus | None = None,
        access_level: AccessLevel | None = None,
    ) -> Page[DocumentListItem]:
        return await asyncio.to_thread(
            self._list_documents_sync, tenant_id, page, per_page, status, access_level
        )

    def _list_documents_sync(
        self,
        tenant_id: UUID,
        page: int,
        per_page: int,
        status: DocumentStatus | None,
        access_level: AccessLevel | None,
    ) -> Page[DocumentListItem]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            stmt = select(models.Document).where(models.Document.tenant_id == tenant_id)
            if status is not None:
                stmt = stmt.where(models.Document.status == _enum_str(status))
            if access_level is not None:
                stmt = stmt.where(models.Document.access_level == int(access_level))

            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            docs = (
                session.execute(
                    stmt.order_by(models.Document.created_at.desc()).offset(offset).limit(per_page)
                )
                .scalars()
                .all()
            )

            items: list[DocumentListItem] = []
            for d in docs:
                file_name = (
                    str(d.doc_metadata.get("file_name"))
                    if "file_name" in d.doc_metadata
                    else os.path.basename(d.source_uri)
                )
                raw_size = d.doc_metadata.get("size_bytes", 0)
                size_bytes = int(raw_size) if isinstance(raw_size, (int, float)) else 0
                mime_type = str(d.doc_metadata.get("mime_type", "application/octet-stream"))

                parent_count = (
                    session.scalar(
                        select(func.count(models.ParentChunk.id)).where(
                            models.ParentChunk.document_id == d.id
                        )
                    )
                    or 0
                )
                child_count = (
                    session.scalar(
                        select(func.count(models.ChildChunk.id)).where(
                            models.ChildChunk.parent_id.in_(
                                select(models.ParentChunk.id).where(
                                    models.ParentChunk.document_id == d.id
                                )
                            )
                        )
                    )
                    or 0
                )

                items.append(
                    DocumentListItem(
                        id=d.id,
                        tenant_id=d.tenant_id,
                        source_uri=d.source_uri,
                        file_name=file_name,
                        content_hash=d.content_hash,
                        mime_type=mime_type,
                        status=DocumentStatus(d.status),
                        access_level=AccessLevel(d.access_level),
                        parent_chunk_count=parent_count,
                        child_chunk_count=child_count,
                        size_bytes=size_bytes,
                        error_message=d.error_message,
                        created_at=d.created_at,
                        updated_at=d.updated_at,
                    )
                )

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    async def upload_document(
        self,
        tenant_id: UUID,
        file_name: str,
        content: bytes,
        mime_type: str = "application/octet-stream",
        access_level: AccessLevel = AccessLevel.PUBLIC,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> DocumentListItem:
        content_hash = hashlib.sha256(content).hexdigest()
        key = f"documents/{content_hash}/{file_name}"
        stored_uri = await self.storage.put(
            tenant_id=tenant_id, key=key, data=content, content_type=mime_type
        )

        return await asyncio.to_thread(
            self._create_document_record_sync,
            tenant_id,
            file_name,
            stored_uri,
            content_hash,
            mime_type,
            len(content),
            access_level,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _create_document_record_sync(
        self,
        tenant_id: UUID,
        file_name: str,
        stored_uri: str,
        content_hash: str,
        mime_type: str,
        size_bytes: int,
        access_level: AccessLevel,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> DocumentListItem:
        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(models.Document).where(
                    models.Document.tenant_id == tenant_id,
                    models.Document.content_hash == content_hash,
                )
            ).scalar_one_or_none()

            now = datetime.now(UTC)
            if existing:
                doc = existing
                doc.status = str(DocumentStatus.PENDING)
                doc.error_message = None
                doc.updated_at = now
            else:
                doc = models.Document(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    source_type="file",
                    source_uri=stored_uri,
                    content_hash=content_hash,
                    status=str(DocumentStatus.PENDING),
                    access_level=int(access_level),
                    doc_metadata={
                        "file_name": file_name,
                        "size_bytes": size_bytes,
                        "mime_type": mime_type,
                    },
                    created_at=now,
                    updated_at=now,
                )
                session.add(doc)

            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="document.upload",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="document",
                object_id=str(doc.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={
                    "file_name": file_name,
                    "content_hash": content_hash,
                    "size_bytes": size_bytes,
                },
            )
            session.flush()

            return DocumentListItem(
                id=doc.id,
                tenant_id=doc.tenant_id,
                source_uri=doc.source_uri,
                file_name=file_name,
                content_hash=doc.content_hash,
                mime_type=mime_type,
                status=DocumentStatus(doc.status),
                access_level=AccessLevel(doc.access_level),
                parent_chunk_count=0,
                child_chunk_count=0,
                size_bytes=size_bytes,
                error_message=doc.error_message,
                created_at=doc.created_at,
                updated_at=doc.updated_at,
            )

    async def delete_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._delete_document_sync,
            tenant_id,
            document_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _delete_document_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            doc = session.execute(
                select(models.Document).where(
                    models.Document.id == document_id,
                    models.Document.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if not doc:
                raise ValueError(f"Document {document_id} not found")

            session.delete(doc)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="document.delete",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="document",
                object_id=str(document_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    async def reindex_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._reindex_document_sync,
            tenant_id,
            document_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _reindex_document_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            doc = session.execute(
                select(models.Document).where(
                    models.Document.id == document_id,
                    models.Document.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if not doc:
                raise ValueError(f"Document {document_id} not found")

            session.execute(
                delete(models.ChildChunk).where(
                    models.ChildChunk.parent_id.in_(
                        select(models.ParentChunk.id).where(
                            models.ParentChunk.document_id == document_id
                        )
                    )
                )
            )
            session.execute(
                delete(models.ParentChunk).where(models.ParentChunk.document_id == document_id)
            )

            doc.status = str(DocumentStatus.PENDING)
            doc.error_message = None
            doc.updated_at = datetime.now(UTC)

            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="document.reindex",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="document",
                object_id=str(document_id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"status": "pending"},
            )

    async def reclassify_document(
        self,
        tenant_id: UUID,
        document_id: UUID,
        new_access_level: AccessLevel,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Cascading transactional re-classification of document and related chunks."""
        await asyncio.to_thread(
            self._reclassify_document_sync,
            tenant_id,
            document_id,
            new_access_level,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _reclassify_document_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        new_access_level: AccessLevel,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        level_int = int(new_access_level)
        now = datetime.now(UTC)

        with self._session_factory() as session, session.begin():
            doc = session.execute(
                select(models.Document).where(
                    models.Document.id == document_id,
                    models.Document.tenant_id == tenant_id,
                )
            ).scalar_one_or_none()
            if not doc:
                raise ValueError(f"Document {document_id} not found")

            old_level = doc.access_level
            doc.access_level = level_int
            doc.updated_at = now

            session.execute(
                update(models.ParentChunk)
                .where(models.ParentChunk.document_id == document_id)
                .values(access_level=level_int)
            )

            parent_ids_subquery = select(models.ParentChunk.id).where(
                models.ParentChunk.document_id == document_id
            )
            session.execute(
                update(models.ChildChunk)
                .where(models.ChildChunk.parent_id.in_(parent_ids_subquery))
                .values(access_level=level_int)
            )

            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="document.reclassify",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="document",
                object_id=str(document_id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"old_access_level": old_level, "new_access_level": level_int},
            )

    # -------------------------------------------------------------------------
    # Chunk Viewing & Safe Vector Stripping (§7)
    # -------------------------------------------------------------------------
    async def get_document_chunks(
        self,
        tenant_id: UUID,
        document_id: UUID,
        include_vectors: bool = False,
        single_chunk_id: UUID | None = None,
    ) -> list[ChunkView]:
        return await asyncio.to_thread(
            self._get_document_chunks_sync,
            tenant_id,
            document_id,
            include_vectors,
            single_chunk_id,
        )

    def _get_document_chunks_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        include_vectors: bool,
        single_chunk_id: UUID | None,
    ) -> list[ChunkView]:
        with self._session_factory() as session:
            stmt = (
                select(models.ParentChunk)
                .where(
                    models.ParentChunk.document_id == document_id,
                    models.ParentChunk.tenant_id == tenant_id,
                )
                .order_by(models.ParentChunk.chunk_index.asc())
            )
            parents = session.execute(stmt).scalars().all()

            results: list[ChunkView] = []
            for p in parents:
                child_stmt = (
                    select(models.ChildChunk)
                    .where(models.ChildChunk.parent_id == p.id)
                    .order_by(models.ChildChunk.chunk_index.asc())
                )
                children = session.execute(child_stmt).scalars().all()

                child_views: list[ChunkView] = []
                for c in children:
                    should_include_vec = include_vectors and (
                        single_chunk_id is not None and c.id == single_chunk_id
                    )
                    embedding_val: list[float] | None = None
                    if should_include_vec and c.embedding is not None:
                        embedding_val = list(c.embedding)

                    child_views.append(
                        ChunkView(
                            id=c.id,
                            chunk_type="child",
                            parent_id=c.parent_id,
                            chunk_index=c.chunk_index,
                            token_count=c.token_count,
                            content=c.content,
                            access_level=AccessLevel(c.access_level),
                            embedding=embedding_val,
                            sparse_vector=None,
                            embedding_model_id=c.embedding_model_id,
                            embedding_version=c.embedding_version,
                            created_at=c.created_at,
                        )
                    )

                results.append(
                    ChunkView(
                        id=p.id,
                        chunk_type="parent",
                        parent_id=None,
                        chunk_index=p.chunk_index,
                        token_count=p.token_count,
                        content=p.content,
                        access_level=AccessLevel(p.access_level),
                        embedding=None,
                        sparse_vector=None,
                        created_at=p.created_at,
                        children=child_views,
                    )
                )

            return results

    # -------------------------------------------------------------------------
    # Licensing Management (§6, §8, core/licensing_and_distribution.md)
    # -------------------------------------------------------------------------
    async def get_license_status(self, tenant_id: UUID) -> LicenseStatus:
        return await asyncio.to_thread(self._get_license_status_sync, tenant_id)

    def _get_license_status_sync(self, tenant_id: UUID) -> LicenseStatus:
        with self._session_factory() as session:
            lic = session.execute(
                select(models.License).where(models.License.tenant_id == tenant_id)
            ).scalar_one_or_none()

            current_docs = (
                session.scalar(
                    select(func.count(models.Document.id)).where(
                        models.Document.tenant_id == tenant_id
                    )
                )
                or 0
            )
            current_users = (
                session.scalar(
                    select(func.count(models.User.id)).where(models.User.tenant_id == tenant_id)
                )
                or 0
            )

            if not lic:
                return LicenseStatus(
                    is_valid=True,
                    plan=LicensePlan.STANDARD,
                    mode=LicenseMode.ONLINE_SUBSCRIPTION,
                    issued_at=None,
                    expires_at=None,
                    max_documents=1000,
                    max_users=10,
                    used_documents=current_docs,
                    used_users=current_users,
                    hardware_id=None,
                    restricted_mode=False,
                )

            now = datetime.now(UTC)
            is_expired = lic.expires_at is not None and now > lic.expires_at
            is_valid = not lic.is_revoked and not is_expired

            return LicenseStatus(
                is_valid=is_valid,
                plan=LicensePlan(lic.plan),
                mode=LicenseMode(lic.mode),
                issued_at=lic.issued_at if hasattr(lic, "issued_at") else None,
                expires_at=lic.expires_at,
                max_documents=lic.max_documents,
                max_users=lic.max_users,
                used_documents=current_docs,
                used_users=current_users,
                hardware_id=lic.hardware_id,
                restricted_mode=not is_valid,
                restricted_reason="License revoked or expired" if not is_valid else None,
            )

    async def activate_license(
        self,
        tenant_id: UUID,
        request: LicenseActivationRequest,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> LicenseStatus:
        return await asyncio.to_thread(
            self._activate_license_sync,
            tenant_id,
            request,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _activate_license_sync(
        self,
        tenant_id: UUID,
        request: LicenseActivationRequest,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> LicenseStatus:
        raw_token: str | None = None
        if request.license_token:
            raw_token = request.license_token.strip()
        elif request.license_file_b64:
            try:
                raw_bytes = base64.b64decode(request.license_file_b64.strip())
                raw_token = raw_bytes.decode("utf-8").strip()
            except Exception as exc:
                raise ValueError(f"Invalid base64 in license file: {exc}") from exc
        elif request.activation_code:
            code = request.activation_code.strip()
            if not code:
                raise ValueError("Empty activation code provided")
            raise ValueError(f"Activation code '{code}' requires online License Server connection")

        if not raw_token:
            raise ValueError("No license token, file, or activation code provided")

        pub_key_bytes = self._get_vendor_public_key()
        machine_fp = self.get_machine_fingerprint()

        try:
            payload = verify_license_token(
                token=raw_token,
                public_key_bytes=pub_key_bytes,
                expected_hardware_id=machine_fp.hardware_id,
                allow_expired=False,
            )
        except LicenseValidationError as exc:
            raise ValueError(f"License verification failed: {exc}") from exc

        with self._session_factory() as session, session.begin():
            lic = session.execute(
                select(models.License).where(models.License.tenant_id == tenant_id)
            ).scalar_one_or_none()

            features_list = [_enum_str(f) for f in payload.features]

            if lic:
                lic.license_key = raw_token
                lic.plan = _enum_str(payload.plan)
                lic.mode = _enum_str(payload.mode)
                lic.expires_at = payload.expires_at
                lic.features = features_list
                lic.max_documents = payload.max_documents
                lic.max_users = payload.max_users
                lic.hardware_id = payload.hardware_id
                lic.is_revoked = False
            else:
                lic_id = UUID(payload.license_id) if payload.license_id else uuid4()
                lic = models.License(
                    id=lic_id,
                    tenant_id=tenant_id,
                    license_key=raw_token,
                    plan=_enum_str(payload.plan),
                    mode=_enum_str(payload.mode),
                    issued_at=payload.issued_at,
                    expires_at=payload.expires_at,
                    features=features_list,
                    max_documents=payload.max_documents,
                    max_users=payload.max_users,
                    hardware_id=payload.hardware_id,
                    is_revoked=False,
                )
                session.add(lic)

            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="license.activate",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="license",
                object_id=str(lic.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={
                    "plan": lic.plan,
                    "mode": lic.mode,
                    "max_documents": lic.max_documents,
                    "max_users": lic.max_users,
                    "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
                },
            )
            session.flush()

        return self._get_license_status_sync(tenant_id)

    def get_machine_fingerprint(self) -> MachineFingerprint:
        """Computes composite hardware fingerprint (MAC, CPU, host, system node)."""
        hostname = socket.gethostname()
        system = platform.system()
        node = platform.node()
        machine = platform.machine()
        processor = platform.processor() or "unknown"
        mac = str(uuid_pkg.getnode())
        raw = f"{hostname}:{system}:{node}:{machine}:{processor}:{mac}"
        hw_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()

        return MachineFingerprint(
            hardware_id=hw_id,
            hostname=hostname,
            collected_at=datetime.now(UTC),
        )

    # -------------------------------------------------------------------------
    # Dashboard Stats & Queue (§9)
    # -------------------------------------------------------------------------
    async def get_dashboard_stats(self, tenant_id: UUID) -> DashboardStats:
        return await asyncio.to_thread(self._get_dashboard_stats_sync, tenant_id)

    def _get_dashboard_stats_sync(self, tenant_id: UUID) -> DashboardStats:
        with self._session_factory() as session:
            docs_total = (
                session.scalar(
                    select(func.count(models.Document.id)).where(
                        models.Document.tenant_id == tenant_id
                    )
                )
                or 0
            )

            status_rows = session.execute(
                select(models.Document.status, func.count(models.Document.id))
                .where(models.Document.tenant_id == tenant_id)
                .group_by(models.Document.status)
            ).all()
            docs_by_status = {DocumentStatus(s): count for s, count in status_rows}

            child_chunks_total = (
                session.scalar(
                    select(func.count(models.ChildChunk.id)).where(
                        models.ChildChunk.tenant_id == tenant_id
                    )
                )
                or 0
            )

            storage_bytes = (
                session.scalar(
                    select(
                        func.sum(
                            func.coalesce(
                                models.Document.doc_metadata["size_bytes"]
                                .as_string()
                                .cast(Integer),
                                0,
                            )
                        )
                    ).where(models.Document.tenant_id == tenant_id)
                )
                or 0
            )

            since_24h = datetime.now(UTC) - timedelta(hours=24)
            queries_last_24h = (
                session.scalar(
                    select(func.count(models.Message.id))
                    .join(
                        models.Conversation,
                        models.Conversation.id == models.Message.conversation_id,
                    )
                    .where(
                        models.Conversation.tenant_id == tenant_id,
                        models.Message.role == "user",
                        models.Message.created_at >= since_24h,
                    )
                )
                or 0
            )

            return DashboardStats(
                documents_total=docs_total,
                documents_by_status=docs_by_status,
                child_chunks_total=child_chunks_total,
                storage_bytes_used=int(storage_bytes) if storage_bytes else 0,
                queries_last_24h=queries_last_24h,
                p95_retrieval_latency_ms=None,
            )

    async def get_ingest_queue(
        self, tenant_id: UUID, state: DocumentStatus | None = None
    ) -> list[IngestQueueItem]:
        return await asyncio.to_thread(self._get_ingest_queue_sync, tenant_id, state)

    def _get_ingest_queue_sync(
        self, tenant_id: UUID, state: DocumentStatus | None
    ) -> list[IngestQueueItem]:
        with self._session_factory() as session:
            stmt = select(models.Document).where(models.Document.tenant_id == tenant_id)
            if state:
                stmt = stmt.where(models.Document.status == _enum_str(state))
            else:
                stmt = stmt.where(
                    models.Document.status.in_(
                        [
                            str(DocumentStatus.PENDING),
                            str(DocumentStatus.PROCESSING),
                            str(DocumentStatus.FAILED),
                        ]
                    )
                )
            docs = (
                session.execute(stmt.order_by(models.Document.updated_at.desc()).limit(100))
                .scalars()
                .all()
            )

            items: list[IngestQueueItem] = []
            for d in docs:
                file_name = (
                    str(d.doc_metadata.get("file_name"))
                    if "file_name" in d.doc_metadata
                    else os.path.basename(d.source_uri)
                )
                items.append(
                    IngestQueueItem(
                        document_id=d.id,
                        file_name=file_name,
                        status=DocumentStatus(d.status),
                        stage="processing" if d.status == "processing" else None,
                        retry_count=0,
                        error_message=d.error_message,
                        updated_at=d.updated_at,
                    )
                )
            return items

    # -------------------------------------------------------------------------
    # SSE Live Stream (LISTEN/NOTIFY with fallback polling) (§7)
    # -------------------------------------------------------------------------
    async def stream_ingest_events(
        self, tenant_id: UUID, poll_interval: float = 2.0
    ) -> AsyncGenerator[str, None]:
        """Yields SSE events: snapshot first, then deltas over LISTEN/NOTIFY or fallback polling.

        Primary path: Postgres ``LISTEN ingest_events`` (the DB trigger
        ``trg_documents_status_notify`` emits ``NOTIFY ingest_events`` on status transitions -
        see storage_schema.md §7). Fallback: periodic polling of ``documents.status`` when the
        async connection or LISTEN is unavailable (e.g. the SQLite dev backend).
        """
        queue = await self.get_ingest_queue(tenant_id)
        snapshot_payload = json.dumps([item.model_dump(mode="json") for item in queue])
        yield f"event: snapshot\ndata: {snapshot_payload}\n\n"

        last_check = datetime.now(UTC)
        conn = None
        notify_iter: AsyncGenerator[Any, None] | None = None
        try:
            import psycopg

            conn = await psycopg.AsyncConnection.connect(
                self.settings.postgres_dsn, autocommit=True
            )
            async with conn.cursor() as cur:
                await cur.execute("LISTEN ingest_events")
            notify_iter = conn.notifies()
        except Exception as exc:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()
                conn = None
            logger.info("LISTEN/NOTIFY unavailable, using polling fallback: %s", exc)
            notify_iter = None

        def _to_aware(dt: datetime) -> datetime:
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

        try:
            while True:
                if notify_iter is not None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(anext(notify_iter), timeout=poll_interval)
                else:
                    await asyncio.sleep(poll_interval)

                current_items = await self.get_ingest_queue(tenant_id)
                for item in current_items:
                    if _to_aware(item.updated_at) >= last_check:
                        payload = json.dumps(item.model_dump(mode="json"))
                        yield f"event: document_status\ndata: {payload}\n\n"
                last_check = datetime.now(UTC)
        except asyncio.CancelledError:
            raise
        except StopAsyncIteration:
            pass
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    await conn.close()

    # -------------------------------------------------------------------------
    # Audit Log (§10, §13)
    # -------------------------------------------------------------------------
    async def get_audit_log(
        self,
        tenant_id: UUID,
        page: int = 1,
        per_page: int = 50,
        action: str | None = None,
        actor_email: str | None = None,
    ) -> Page[AuditLogEntry]:
        return await asyncio.to_thread(
            self._get_audit_log_sync, tenant_id, page, per_page, action, actor_email
        )

    def _get_audit_log_sync(
        self,
        tenant_id: UUID,
        page: int,
        per_page: int,
        action: str | None,
        actor_email: str | None,
    ) -> Page[AuditLogEntry]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            stmt = select(models.AuditLog).where(
                (models.AuditLog.tenant_id == tenant_id) | (models.AuditLog.tenant_id.is_(None))
            )
            if action:
                stmt = stmt.where(models.AuditLog.action == action)
            if actor_email:
                stmt = stmt.where(models.AuditLog.actor_email == actor_email)

            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            rows = (
                session.execute(
                    stmt.order_by(models.AuditLog.created_at.desc()).offset(offset).limit(per_page)
                )
                .scalars()
                .all()
            )

            items = [
                AuditLogEntry(
                    id=r.id,
                    tenant_id=r.tenant_id,
                    actor_user_id=r.actor_user_id,
                    actor_email=r.actor_email,
                    action=r.action,
                    object_type=r.object_type,
                    object_id=r.object_id,
                    ip_address=r.ip_address,
                    user_agent=r.user_agent,
                    payload=r.payload or {},
                    created_at=r.created_at,
                )
                for r in rows
            ]

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    # -------------------------------------------------------------------------
    # Access Control §5A: Users CRUD
    # -------------------------------------------------------------------------
    async def list_users(
        self,
        tenant_id: UUID,
        page: int = 1,
        per_page: int = 50,
        access_level: AccessLevel | None = None,
    ) -> Page[UserRead]:
        return await asyncio.to_thread(
            self._list_users_sync, tenant_id, page, per_page, access_level
        )

    def _list_users_sync(
        self,
        tenant_id: UUID,
        page: int,
        per_page: int,
        access_level: AccessLevel | None,
    ) -> Page[UserRead]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            stmt = select(models.User).where(models.User.tenant_id == tenant_id)
            if access_level is not None:
                stmt = stmt.where(models.User.access_level == int(access_level))

            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            users = (
                session.execute(
                    stmt.order_by(models.User.created_at.desc()).offset(offset).limit(per_page)
                )
                .scalars()
                .all()
            )

            items = [
                UserRead(
                    id=u.id,
                    tenant_id=u.tenant_id,
                    channel_user_id=u.channel_user_id,
                    channel=ChannelType(u.channel),
                    access_level=AccessLevel(u.access_level),
                    is_active=u.is_active,
                    user_metadata=u.user_metadata,
                    created_at=u.created_at,
                    updated_at=u.updated_at,
                )
                for u in users
            ]

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    async def create_user(
        self,
        tenant_id: UUID,
        channel_user_id: str,
        channel: ChannelType = ChannelType.TELEGRAM,
        access_level: AccessLevel = AccessLevel.PUBLIC,
        is_active: bool = True,
        user_metadata: dict[str, Any] | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserRead:
        return await asyncio.to_thread(
            self._create_user_sync,
            tenant_id,
            channel_user_id,
            channel,
            access_level,
            is_active,
            user_metadata,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _create_user_sync(
        self,
        tenant_id: UUID,
        channel_user_id: str,
        channel: ChannelType,
        access_level: AccessLevel,
        is_active: bool,
        user_metadata: dict[str, Any] | None,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> UserRead:
        with self._session_factory() as session, session.begin():
            now = datetime.now(UTC)
            user = models.User(
                id=uuid4(),
                tenant_id=tenant_id,
                channel_user_id=channel_user_id,
                channel=_enum_str(channel),
                access_level=int(access_level),
                is_active=is_active,
                user_metadata=user_metadata or {},
                created_at=now,
                updated_at=now,
            )
            session.add(user)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="user.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="user",
                object_id=str(user.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"channel_user_id": channel_user_id, "access_level": int(access_level)},
            )
            session.flush()

            return UserRead(
                id=user.id,
                tenant_id=user.tenant_id,
                channel_user_id=user.channel_user_id,
                channel=ChannelType(user.channel),
                access_level=AccessLevel(user.access_level),
                is_active=user.is_active,
                user_metadata=user.user_metadata,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )

    async def update_user(
        self,
        tenant_id: UUID,
        user_id: UUID,
        access_level: AccessLevel | None = None,
        is_active: bool | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> UserRead:
        return await asyncio.to_thread(
            self._update_user_sync,
            tenant_id,
            user_id,
            access_level,
            is_active,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _update_user_sync(
        self,
        tenant_id: UUID,
        user_id: UUID,
        access_level: AccessLevel | None,
        is_active: bool | None,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> UserRead:
        with self._session_factory() as session, session.begin():
            user = session.execute(
                select(models.User).where(
                    models.User.tenant_id == tenant_id, models.User.id == user_id
                )
            ).scalar_one_or_none()
            if not user:
                raise ValueError(f"User {user_id} not found")

            if access_level is not None:
                user.access_level = int(access_level)
            if is_active is not None:
                user.is_active = is_active
            user.updated_at = datetime.now(UTC)

            action = "user.disable" if (is_active is False) else "user.update"
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action=action,
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="user",
                object_id=str(user.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"access_level": user.access_level, "is_active": user.is_active},
            )
            session.flush()

            return UserRead(
                id=user.id,
                tenant_id=user.tenant_id,
                channel_user_id=user.channel_user_id,
                channel=ChannelType(user.channel),
                access_level=AccessLevel(user.access_level),
                is_active=user.is_active,
                user_metadata=user.user_metadata,
                created_at=user.created_at,
                updated_at=user.updated_at,
            )

    async def delete_user(
        self,
        tenant_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._delete_user_sync,
            tenant_id,
            user_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _delete_user_sync(
        self,
        tenant_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(models.User).where(
                    models.User.tenant_id == tenant_id, models.User.id == user_id
                )
            )
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="user.delete",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="user",
                object_id=str(user_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    # -------------------------------------------------------------------------
    # Groups CRUD (§5A)
    # -------------------------------------------------------------------------
    async def list_groups(self, tenant_id: UUID, page: int = 1, per_page: int = 50) -> Page[Group]:
        return await asyncio.to_thread(self._list_groups_sync, tenant_id, page, per_page)

    def _list_groups_sync(self, tenant_id: UUID, page: int, per_page: int) -> Page[Group]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            query = select(models.Group).where(models.Group.tenant_id == tenant_id)
            total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
            groups = (
                session.execute(
                    query.order_by(models.Group.created_at.desc()).offset(offset).limit(per_page)
                )
                .scalars()
                .all()
            )

            items = [
                Group(
                    id=g.id,
                    tenant_id=g.tenant_id,
                    name=g.name,
                    access_level=AccessLevel(g.access_level),
                    external_ref=g.external_ref,
                    created_at=g.created_at,
                )
                for g in groups
            ]

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    async def create_group(
        self,
        tenant_id: UUID,
        name: str,
        access_level: AccessLevel = AccessLevel.PUBLIC,
        external_ref: str | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Group:
        return await asyncio.to_thread(
            self._create_group_sync,
            tenant_id,
            name,
            access_level,
            external_ref,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _create_group_sync(
        self,
        tenant_id: UUID,
        name: str,
        access_level: AccessLevel,
        external_ref: str | None,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Group:
        with self._session_factory() as session, session.begin():
            now = datetime.now(UTC)
            g = models.Group(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                access_level=int(access_level),
                external_ref=external_ref,
                created_at=now,
            )
            session.add(g)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="group.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="group",
                object_id=str(g.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"name": name, "access_level": int(access_level)},
            )
            session.flush()

            return Group(
                id=g.id,
                tenant_id=g.tenant_id,
                name=g.name,
                access_level=AccessLevel(g.access_level),
                external_ref=g.external_ref,
                created_at=g.created_at,
            )

    async def delete_group(
        self,
        tenant_id: UUID,
        group_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._delete_group_sync,
            tenant_id,
            group_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _delete_group_sync(
        self,
        tenant_id: UUID,
        group_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(models.Group).where(
                    models.Group.tenant_id == tenant_id, models.Group.id == group_id
                )
            )
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="group.delete",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="group",
                object_id=str(group_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    async def add_group_member(
        self,
        tenant_id: UUID,
        group_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._add_group_member_sync,
            tenant_id,
            group_id,
            user_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _add_group_member_sync(
        self,
        tenant_id: UUID,
        group_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(models.UserGroup).where(
                    models.UserGroup.group_id == group_id,
                    models.UserGroup.user_id == user_id,
                )
            ).scalar_one_or_none()
            if not existing:
                now = datetime.now(UTC)
                session.add(
                    models.UserGroup(
                        group_id=group_id,
                        user_id=user_id,
                        tenant_id=tenant_id,
                        created_at=now,
                    )
                )
                self._record_audit_sync(
                    session=session,
                    tenant_id=tenant_id,
                    action="group.member_add",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    object_type="group",
                    object_id=str(group_id),
                    ip_address=ip_address,
                    user_agent=user_agent,
                    payload={"user_id": str(user_id)},
                )

    async def remove_group_member(
        self,
        tenant_id: UUID,
        group_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._remove_group_member_sync,
            tenant_id,
            group_id,
            user_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _remove_group_member_sync(
        self,
        tenant_id: UUID,
        group_id: UUID,
        user_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(models.UserGroup).where(
                    models.UserGroup.tenant_id == tenant_id,
                    models.UserGroup.group_id == group_id,
                    models.UserGroup.user_id == user_id,
                )
            )
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="group.member_remove",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="group",
                object_id=str(group_id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"user_id": str(user_id)},
            )

    # -------------------------------------------------------------------------
    # Collections CRUD (§5A)
    # -------------------------------------------------------------------------
    async def list_collections(
        self, tenant_id: UUID, page: int = 1, per_page: int = 50
    ) -> Page[Collection]:
        return await asyncio.to_thread(self._list_collections_sync, tenant_id, page, per_page)

    def _list_collections_sync(self, tenant_id: UUID, page: int, per_page: int) -> Page[Collection]:
        page = max(1, page)
        per_page = max(1, min(100, per_page))
        offset = (page - 1) * per_page

        with self._session_factory() as session:
            stmt = select(models.Collection).where(models.Collection.tenant_id == tenant_id)
            total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
            cols = (
                session.execute(
                    stmt.order_by(models.Collection.created_at.desc())
                    .offset(offset)
                    .limit(per_page)
                )
                .scalars()
                .all()
            )

            items = [
                Collection(
                    id=c.id,
                    tenant_id=c.tenant_id,
                    name=c.name,
                    access_level=AccessLevel(c.access_level),
                    parent_id=c.parent_id,
                    created_at=c.created_at,
                )
                for c in cols
            ]

            return Page(
                items=items,
                total=total,
                page=page,
                per_page=per_page,
                pages=math.ceil(total / per_page) if per_page > 0 else 0,
            )

    async def create_collection(
        self,
        tenant_id: UUID,
        name: str,
        access_level: AccessLevel = AccessLevel.PUBLIC,
        parent_id: UUID | None = None,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Collection:
        return await asyncio.to_thread(
            self._create_collection_sync,
            tenant_id,
            name,
            access_level,
            parent_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _create_collection_sync(
        self,
        tenant_id: UUID,
        name: str,
        access_level: AccessLevel,
        parent_id: UUID | None,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> Collection:
        with self._session_factory() as session, session.begin():
            now = datetime.now(UTC)
            c = models.Collection(
                id=uuid4(),
                tenant_id=tenant_id,
                name=name,
                access_level=int(access_level),
                parent_id=parent_id,
                created_at=now,
            )
            session.add(c)
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="collection.create",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="collection",
                object_id=str(c.id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={"name": name, "access_level": int(access_level)},
            )
            session.flush()

            return Collection(
                id=c.id,
                tenant_id=c.tenant_id,
                name=c.name,
                access_level=AccessLevel(c.access_level),
                parent_id=c.parent_id,
                created_at=c.created_at,
            )

    async def delete_collection(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._delete_collection_sync,
            tenant_id,
            collection_id,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _delete_collection_sync(
        self,
        tenant_id: UUID,
        collection_id: UUID,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(models.Collection).where(
                    models.Collection.tenant_id == tenant_id,
                    models.Collection.id == collection_id,
                )
            )
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="collection.delete",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="collection",
                object_id=str(collection_id),
                ip_address=ip_address,
                user_agent=user_agent,
            )

    # -------------------------------------------------------------------------
    # Document ACL (§5A)
    # -------------------------------------------------------------------------
    async def grant_document_acl(
        self,
        tenant_id: UUID,
        document_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
        permission: AclPermission = AclPermission.READ,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._grant_document_acl_sync,
            tenant_id,
            document_id,
            principal_type,
            principal_id,
            permission,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _grant_document_acl_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
        permission: AclPermission,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            existing = session.execute(
                select(models.DocumentAcl).where(
                    models.DocumentAcl.tenant_id == tenant_id,
                    models.DocumentAcl.document_id == document_id,
                    models.DocumentAcl.principal_type == _enum_str(principal_type),
                    models.DocumentAcl.principal_id == principal_id,
                    models.DocumentAcl.permission == _enum_str(permission),
                )
            ).scalar_one_or_none()
            if not existing:
                now = datetime.now(UTC)
                session.add(
                    models.DocumentAcl(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        document_id=document_id,
                        principal_type=_enum_str(principal_type),
                        principal_id=principal_id,
                        permission=_enum_str(permission),
                        created_at=now,
                    )
                )
                self._record_audit_sync(
                    session=session,
                    tenant_id=tenant_id,
                    action="acl.grant",
                    actor_user_id=actor_user_id,
                    actor_email=actor_email,
                    object_type="document_acl",
                    object_id=str(document_id),
                    ip_address=ip_address,
                    user_agent=user_agent,
                    payload={
                        "principal_type": _enum_str(principal_type),
                        "principal_id": str(principal_id),
                        "permission": _enum_str(permission),
                    },
                )

    async def revoke_document_acl(
        self,
        tenant_id: UUID,
        document_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
        permission: AclPermission = AclPermission.READ,
        actor_user_id: UUID | None = None,
        actor_email: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._revoke_document_acl_sync,
            tenant_id,
            document_id,
            principal_type,
            principal_id,
            permission,
            actor_user_id,
            actor_email,
            ip_address,
            user_agent,
        )

    def _revoke_document_acl_sync(
        self,
        tenant_id: UUID,
        document_id: UUID,
        principal_type: PrincipalType,
        principal_id: UUID,
        permission: AclPermission,
        actor_user_id: UUID | None,
        actor_email: str | None,
        ip_address: str | None,
        user_agent: str | None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            session.execute(
                delete(models.DocumentAcl).where(
                    models.DocumentAcl.tenant_id == tenant_id,
                    models.DocumentAcl.document_id == document_id,
                    models.DocumentAcl.principal_type == _enum_str(principal_type),
                    models.DocumentAcl.principal_id == principal_id,
                    models.DocumentAcl.permission == _enum_str(permission),
                )
            )
            self._record_audit_sync(
                session=session,
                tenant_id=tenant_id,
                action="acl.revoke",
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                object_type="document_acl",
                object_id=str(document_id),
                ip_address=ip_address,
                user_agent=user_agent,
                payload={
                    "principal_type": _enum_str(principal_type),
                    "principal_id": str(principal_id),
                    "permission": _enum_str(permission),
                },
            )

    # -------------------------------------------------------------------------
    # Retrieval Playground Diagnostics (§9)
    # -------------------------------------------------------------------------
    async def playground_retrieve(
        self,
        tenant_id: UUID,
        query: str,
        access_level: AccessLevel = AccessLevel.PUBLIC,
    ) -> PlaygroundRetrieveResponse:
        """Retrieval diagnostics: returns candidate ranks, RRF scores and dense/sparse marks."""
        if self.retriever is not None:
            candidates = await self.retriever.retrieve(
                query=query,
                tenant_id=tenant_id,
                user_access_level=int(access_level),
            )
            return PlaygroundRetrieveResponse(
                candidates=candidates,
                effective_access_level=access_level,
            )

        candidates = await asyncio.to_thread(
            self._mock_playground_retrieve_sync, tenant_id, query, access_level
        )
        return PlaygroundRetrieveResponse(
            candidates=candidates,
            effective_access_level=access_level,
        )

    def _mock_playground_retrieve_sync(
        self,
        tenant_id: UUID,
        query: str,
        access_level: AccessLevel,
    ) -> list[RetrievalCandidate]:
        with self._session_factory() as session:
            stmt = (
                select(models.ChildChunk, models.ParentChunk.document_id)
                .join(models.ParentChunk, models.ParentChunk.id == models.ChildChunk.parent_id)
                .where(
                    models.ChildChunk.tenant_id == tenant_id,
                    models.ChildChunk.access_level <= int(access_level),
                )
            )
            clean_q = query.strip().lower()
            if clean_q:
                stmt = stmt.where(models.ChildChunk.content.ilike(f"%{clean_q}%"))
            stmt = stmt.limit(10)

            rows = session.execute(stmt).all()
            results: list[RetrievalCandidate] = []
            for idx, (c, doc_id) in enumerate(rows, start=1):
                results.append(
                    RetrievalCandidate(
                        child_id=c.id,
                        parent_id=c.parent_id,
                        document_id=doc_id,
                        content=c.content,
                        dense_score=max(0.1, 0.90 - (idx * 0.05)),
                        sparse_score=max(0.1, 1.20 - (idx * 0.1)),
                        dense_rank=idx,
                        sparse_rank=idx,
                        rrf_score=1.0 / (60 + idx),
                        rerank_score=max(0.1, 0.95 - (idx * 0.05)),
                    )
                )
            return results
