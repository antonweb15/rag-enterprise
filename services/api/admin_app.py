"""Admin API application (`services/api/admin_app.py`).

Canonical specification - `core/admin_panel.md` §3, §4, §5, §5A, §10, `core/eval_and_ops.md` §3.5.
Private operator contour running on 127.0.0.1:8001 / 8080.

Features:
- fastapi-users / CookieTransport auth (HS256 per-install secret, HttpOnly + Secure).
- Double-submit CSRF protection (X-CSRF-Token header on unsafe HTTP methods).
- Mandatory TOTP 2FA for AdminRole.ADMIN and optional for other roles.
- Role-based Access Control (RBAC: Admin > Operator > Viewer).
- Private rate limiting per authenticated user / IP (`admin_rate_limit_per_minute`).
- Live ingest statuses over SSE (`LISTEN/NOTIFY` fallback polling).
- Static SPA file serving for Box deployment mode with path traversal protection.
- Complete AdminUser management endpoints and audit logging with IP/UA.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import mimetypes
import re
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from rag_core.admin.auth import (
    AuthError,
    CSRFError,
    RBACPermissionDeniedError,
    create_admin_session_jwt,
    enforce_role,
    generate_csrf_token,
    verify_admin_session_jwt,
    verify_password,
    verify_totp_code,
)
from rag_core.admin.service import AdminService
from rag_core.channels.rate_limit import InMemoryRateLimiter
from rag_core.contracts.enums import (
    AccessLevel,
    AdminRole,
    DocumentStatus,
)
from rag_core.contracts.interfaces import BaseObjectStorage
from rag_core.contracts.models import (
    AdminLoginRequest,
    AdminLoginResponse,
    AdminUserCreate,
    AdminUserRead,
    AdminUserUpdate,
    CollectionCreateRequest,
    DocumentAclCreateRequest,
    DocumentReclassifyRequest,
    GroupCreateRequest,
    GroupMemberAddRequest,
    LicenseActivationRequest,
    PlaygroundRetrieveRequest,
    TwoFactorVerifyRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from rag_core.contracts.settings import Settings
from rag_core.db.session import SessionFactory, build_session_factory

logger = logging.getLogger(__name__)


def _parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not cookie_header:
        return cookies
    for item in cookie_header.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


class AdminAPIApp:
    """Private ASGI 3.0 Application for the Admin contour."""

    def __init__(
        self,
        settings: Settings | None = None,
        admin_service: AdminService | None = None,
        session_factory: SessionFactory | None = None,
        object_storage: BaseObjectStorage | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.session_factory = session_factory or build_session_factory(self.settings.postgres_dsn)
        self.service = admin_service or AdminService(
            session_factory=self.session_factory,
            object_storage=object_storage,
            settings=self.settings,
        )
        self.rate_limiter = InMemoryRateLimiter(
            max_requests_per_minute=self.settings.admin_rate_limit_per_minute
        )

        secret = self.settings.jwt_secret
        if not secret or len(secret) < 32:
            secret = secrets.token_hex(32)
        self.jwt_secret = secret

        # Bounded CSRF token store (max 10k sessions, session_token -> csrf_token)
        self._csrf_store: dict[str, str] = {}

    def _get_client_ip(self, scope: dict[str, Any], headers: dict[str, str]) -> str:
        # For security: socket client IP by default, unless explicitly trusted proxy configured
        client = scope.get("client")
        if client and isinstance(client, (list, tuple)) and client[0]:
            return str(client[0])
        forwarded = headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return "127.0.0.1"

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        if scope["type"] != "http":
            return

        method = scope["method"].upper()
        path = scope["path"]
        raw_headers = dict(scope.get("headers", []))
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in raw_headers.items()}
        client_ip = self._get_client_ip(scope, headers)
        user_agent = headers.get("user-agent")

        # Rate limiting check (exclude static assets and health)
        if not path.startswith("/assets/") and path not in ("/favicon.ico", "/api/v1/admin/config"):
            cookies = _parse_cookies(headers.get("cookie", ""))
            session_token = cookies.get(self.settings.session_cookie_name)
            rate_key = f"token:{session_token[:16]}" if session_token else f"ip:{client_ip}"
            allowed = await self.rate_limiter.is_allowed(rate_key)
            if not allowed:
                await self._send_json(
                    send,
                    {"error": "Too Many Requests", "message": "Admin rate limit exceeded"},
                    status=429,
                )
                return

        try:
            await self._route_request(
                scope, receive, send, method, path, headers, client_ip, user_agent
            )
        except AuthError as exc:
            await self._send_json(
                send, {"error": "Unauthorized", "message": exc.message}, status=exc.status_code
            )
        except RBACPermissionDeniedError as exc:
            await self._send_json(send, {"error": "Forbidden", "message": exc.message}, status=403)
        except ValueError as exc:
            await self._send_json(send, {"error": "Bad Request", "message": str(exc)}, status=400)
        except Exception as exc:
            logger.exception("Unhandled error in AdminAPIApp: %s", exc)
            await self._send_json(
                send,
                {"error": "Internal Server Error", "message": str(exc)},
                status=500,
            )

    # -------------------------------------------------------------------------
    # Authentication & Session verification (§4, fail-closed)
    # -------------------------------------------------------------------------
    async def _authenticate_request(
        self,
        headers: dict[str, str],
        required_role: AdminRole | None = None,
        require_mfa: bool = True,
    ) -> tuple[AdminUserRead, str]:
        cookies = _parse_cookies(headers.get("cookie", ""))
        session_token = cookies.get(self.settings.session_cookie_name)
        if not session_token:
            raise AuthError("Missing session cookie", status_code=401)

        payload = verify_admin_session_jwt(session_token, secret=self.jwt_secret)
        if require_mfa and payload.get("mfa") is False:
            raise AuthError(
                "2FA enrollment required before accessing admin functions", status_code=403
            )

        user_id_str = payload.get("sub")
        if not user_id_str:
            raise AuthError("Malformed session token", status_code=401)

        try:
            user_id = UUID(user_id_str)
        except ValueError as exc:
            raise AuthError("Invalid user ID in session", status_code=401) from exc

        # Strict DB lookup (fail-closed, never synthesize superuser)
        user = self.service.get_admin_user_sync(user_id)
        if user is None:
            raise AuthError("Admin user no longer exists or was deleted", status_code=401)
        if not user.is_active:
            raise AuthError("Admin account is deactivated", status_code=403)

        if required_role is not None:
            enforce_role(user, required_role)

        return user, session_token

    def _verify_csrf(self, method: str, headers: dict[str, str], session_token: str) -> None:
        """Enforces double-submit CSRF for state-changing HTTP methods."""
        if method in ("GET", "HEAD", "OPTIONS"):
            return

        header_csrf = headers.get("x-csrf-token")
        if not header_csrf:
            raise CSRFError("Missing X-CSRF-Token header")

        session_csrf = self._csrf_store.get(session_token)
        if not session_csrf or not hmac.compare_digest(header_csrf, session_csrf):
            raise CSRFError("Invalid CSRF token")

    # -------------------------------------------------------------------------
    # Routing dispatcher
    # -------------------------------------------------------------------------
    async def _route_request(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        method: str,
        path: str,
        headers: dict[str, str],
        client_ip: str,
        user_agent: str | None,
    ) -> None:
        # Public runtime config (§3)
        if method == "GET" and path == "/api/v1/admin/config":
            config = await self.service.get_config()
            await self._send_json(send, config.model_dump(mode="json"))
            return

        # Auth endpoints (§4)
        if method == "POST" and path == "/api/v1/admin/auth/login":
            await self._handle_login(receive, send, headers, client_ip, user_agent)
            return

        if method == "POST" and path == "/api/v1/admin/auth/logout":
            await self._handle_logout(send, headers, client_ip, user_agent)
            return

        if method == "GET" and path == "/api/v1/admin/auth/me":
            user, _ = await self._authenticate_request(headers, require_mfa=False)
            await self._send_json(send, user.model_dump(mode="json"))
            return

        if method == "POST" and path == "/api/v1/admin/auth/2fa/setup":
            user, token = await self._authenticate_request(headers, require_mfa=False)
            self._verify_csrf(method, headers, token)
            setup_res = self.service.setup_admin_totp_sync(
                user.id,
                ip_address=client_ip,
                user_agent=user_agent,
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._send_json(send, setup_res.model_dump(mode="json"))
            return

        if method == "POST" and path == "/api/v1/admin/auth/2fa/verify":
            user, token = await self._authenticate_request(headers, require_mfa=False)
            self._verify_csrf(method, headers, token)
            body = await self._read_json(receive)
            req = TwoFactorVerifyRequest.model_validate(body)
            success = self.service.verify_admin_totp_sync(
                user.id,
                code=req.code,
                ip_address=client_ip,
                user_agent=user_agent,
                actor_user_id=user.id,
                actor_email=user.email,
            )
            if not success:
                raise ValueError("Invalid 2FA verification code")
            await self._send_json(send, {"status": "ok", "message": "2FA successfully enabled"})
            return

        if method == "POST" and path == "/api/v1/admin/auth/2fa/disable":
            user, token = await self._authenticate_request(headers, require_mfa=False)
            self._verify_csrf(method, headers, token)
            self.service.disable_admin_totp_sync(
                user.id,
                ip_address=client_ip,
                user_agent=user_agent,
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._send_json(send, {"status": "ok", "message": "2FA disabled"})
            return

        # Admin Users CRUD (§4, §13) - ADMIN role required
        if path == "/api/v1/admin/admin-users":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            if method == "GET":
                query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                page = int(query_params.get("page", ["1"])[0])
                per_page = int(query_params.get("per_page", ["50"])[0])
                page_res = self.service.list_admin_users_sync(
                    tenant_id=user.tenant_id, page=page, per_page=per_page
                )
                await self._send_json(send, page_res.model_dump(mode="json"))
                return
            elif method == "POST":
                body = await self._read_json(receive)
                req_admin = AdminUserCreate.model_validate(body)
                created = self.service.create_admin_user_sync(
                    email=req_admin.email,
                    password=req_admin.password,
                    role=req_admin.role,
                    tenant_id=user.tenant_id,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    actor_user_id=user.id,
                    actor_email=user.email,
                )
                await self._send_json(send, created.model_dump(mode="json"), status=201)
                return

        m_admin_user = re.match(r"^/api/v1/admin/admin-users/([0-9a-fA-F-]+)$", path)
        if m_admin_user:
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            target_admin_id = UUID(m_admin_user.group(1))
            if method == "GET":
                target_user = self.service.get_admin_user_sync(target_admin_id)
                if not target_user:
                    await self._send_json(send, {"error": "Admin user not found"}, status=404)
                    return
                await self._send_json(send, target_user.model_dump(mode="json"))
                return
            elif method == "PATCH":
                body = await self._read_json(receive)
                req_up = AdminUserUpdate.model_validate(body)
                updated = self.service.update_admin_user_sync(
                    user_id=target_admin_id,
                    role=req_up.role,
                    is_active=req_up.is_active,
                    password=req_up.password,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    actor_user_id=user.id,
                    actor_email=user.email,
                )
                await self._send_json(send, updated.model_dump(mode="json"))
                return
            elif method == "DELETE":
                self.service.delete_admin_user_sync(
                    user_id=target_admin_id,
                    ip_address=client_ip,
                    user_agent=user_agent,
                    actor_user_id=user.id,
                    actor_email=user.email,
                )
                await self._send_json(
                    send, {"status": "ok", "message": "Admin user deleted"}, status=200
                )
                return

        # Dashboard & Queue endpoints (§9) - VIEWER role minimum
        if method == "GET" and path == "/api/v1/admin/dashboard":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            tenant_id = user.tenant_id or uuid4()
            stats = await self.service.get_dashboard_stats(tenant_id)
            await self._send_json(send, stats.model_dump(mode="json"))
            return

        if method == "GET" and path == "/api/v1/admin/queue":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            tenant_id = user.tenant_id or uuid4()
            query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
            state_str = query_params.get("state", [None])[0]
            state = DocumentStatus(state_str) if state_str else None
            queue_items = await self.service.get_ingest_queue(tenant_id, state=state)
            await self._send_json(send, [i.model_dump(mode="json") for i in queue_items])
            return

        if method == "GET" and path == "/api/v1/admin/events/ingest":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            tenant_id = user.tenant_id or uuid4()
            await self._handle_sse_stream(send, tenant_id)
            return

        # License management (§6, §8)
        if method == "GET" and path == "/api/v1/admin/license":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            tenant_id = user.tenant_id or uuid4()
            status = await self.service.get_license_status(tenant_id)
            await self._send_json(send, status.model_dump(mode="json"))
            return

        if method == "POST" and path == "/api/v1/admin/license/activate":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            body = await self._read_json(receive)
            req_lic = LicenseActivationRequest.model_validate(body)
            tenant_id = user.tenant_id or uuid4()
            new_status = await self.service.activate_license(
                tenant_id,
                req_lic,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, new_status.model_dump(mode="json"))
            return

        if method == "GET" and path == "/api/v1/admin/license/fingerprint":
            await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            fp = self.service.get_machine_fingerprint()
            await self._send_json(send, fp.model_dump(mode="json"))
            return

        # Documents CRUD (§5, §7)
        if path == "/api/v1/admin/documents":
            if method == "GET":
                user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
                tenant_id = user.tenant_id or uuid4()
                query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                page = int(query_params.get("page", ["1"])[0])
                per_page = int(query_params.get("per_page", ["20"])[0])
                status_str = query_params.get("status", [None])[0]
                status_filter = DocumentStatus(status_str) if status_str else None
                level_str = query_params.get("access_level", [None])[0]
                level_filter = AccessLevel(int(level_str)) if level_str is not None else None

                docs_page = await self.service.list_documents(
                    tenant_id=tenant_id,
                    page=page,
                    per_page=per_page,
                    status=status_filter,
                    access_level=level_filter,
                )
                await self._send_json(send, docs_page.model_dump(mode="json"))
                return

            if method == "POST":
                user, token = await self._authenticate_request(
                    headers, required_role=AdminRole.OPERATOR
                )
                self._verify_csrf(method, headers, token)
                tenant_id = user.tenant_id or uuid4()
                await self._handle_document_upload(
                    receive, send, tenant_id, user, client_ip, user_agent
                )
                return

        m_doc = re.match(r"^/api/v1/admin/documents/([0-9a-fA-F-]+)$", path)
        if m_doc:
            doc_id = UUID(m_doc.group(1))
            if method == "DELETE":
                user, token = await self._authenticate_request(
                    headers, required_role=AdminRole.ADMIN
                )
                self._verify_csrf(method, headers, token)
                tenant_id = user.tenant_id or uuid4()
                await self.service.delete_document(
                    tenant_id,
                    doc_id,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, {"status": "ok", "message": "Document deleted"})
                return

        m_reindex = re.match(r"^/api/v1/admin/documents/([0-9a-fA-F-]+)/reindex$", path)
        if m_reindex and method == "POST":
            user, token = await self._authenticate_request(
                headers, required_role=AdminRole.OPERATOR
            )
            self._verify_csrf(method, headers, token)
            doc_id = UUID(m_reindex.group(1))
            tenant_id = user.tenant_id or uuid4()
            await self.service.reindex_document(
                tenant_id,
                doc_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Reindex scheduled"})
            return

        m_reclassify = re.match(r"^/api/v1/admin/documents/([0-9a-fA-F-]+)/reclassify$", path)
        if m_reclassify and method == "POST":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            doc_id = UUID(m_reclassify.group(1))
            tenant_id = user.tenant_id or uuid4()
            body = await self._read_json(receive)
            req_rec = DocumentReclassifyRequest.model_validate(body)
            await self.service.reclassify_document(
                tenant_id=tenant_id,
                document_id=doc_id,
                new_access_level=req_rec.access_level,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Document reclassified"})
            return

        m_chunks = re.match(r"^/api/v1/admin/documents/([0-9a-fA-F-]+)/chunks$", path)
        if m_chunks and method == "GET":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            doc_id = UUID(m_chunks.group(1))
            tenant_id = user.tenant_id or uuid4()
            query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
            include_vec = query_params.get("include_vectors", ["false"])[0].lower() == "true"
            chunk_id_str = query_params.get("chunk_id", [None])[0]
            single_chunk_id = UUID(chunk_id_str) if chunk_id_str else None

            chunks = await self.service.get_document_chunks(
                tenant_id=tenant_id,
                document_id=doc_id,
                include_vectors=include_vec,
                single_chunk_id=single_chunk_id,
            )
            await self._send_json(send, [c.model_dump(mode="json") for c in chunks])
            return

        # Audit Log (§10) - ADMIN role required
        if method == "GET" and path == "/api/v1/admin/audit":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            tenant_id = user.tenant_id or uuid4()
            query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
            page = int(query_params.get("page", ["1"])[0])
            per_page = int(query_params.get("per_page", ["50"])[0])
            action = query_params.get("action", [None])[0]
            actor = query_params.get("actor_email", [None])[0]

            audit_page = await self.service.get_audit_log(
                tenant_id=tenant_id,
                page=page,
                per_page=per_page,
                action=action,
                actor_email=actor,
            )
            await self._send_json(send, audit_page.model_dump(mode="json"))
            return

        # Diagnostics / Playground (§9)
        if method == "POST" and path == "/api/v1/admin/playground/retrieve":
            user, _ = await self._authenticate_request(headers, required_role=AdminRole.VIEWER)
            tenant_id = user.tenant_id or uuid4()
            body = await self._read_json(receive)
            req_play = PlaygroundRetrieveRequest.model_validate(body)
            play_res = await self.service.playground_retrieve(
                tenant_id=tenant_id,
                query=req_play.query,
                access_level=req_play.access_level,
            )
            await self._send_json(send, play_res.model_dump(mode="json"))
            return

        # Access Control §5A: Users / Groups / Collections / ACL (ADMIN only)
        if path == "/api/v1/admin/users":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            tenant_id = user.tenant_id or uuid4()
            if method == "GET":
                query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                page = int(query_params.get("page", ["1"])[0])
                per_page = int(query_params.get("per_page", ["50"])[0])
                users_page = await self.service.list_users(
                    tenant_id=tenant_id, page=page, per_page=per_page
                )
                await self._send_json(send, users_page.model_dump(mode="json"))
                return
            if method == "POST":
                self._verify_csrf(method, headers, token)
                body = await self._read_json(receive)
                req_u = UserCreateRequest.model_validate(body)
                created_u = await self.service.create_user(
                    tenant_id=tenant_id,
                    channel_user_id=req_u.channel_user_id,
                    channel=req_u.channel,
                    access_level=req_u.access_level,
                    is_active=req_u.is_active,
                    user_metadata=req_u.user_metadata,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, created_u.model_dump(mode="json"), status=201)
                return

        m_user = re.match(r"^/api/v1/admin/users/([0-9a-fA-F-]+)$", path)
        if m_user:
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            target_uid = UUID(m_user.group(1))
            tenant_id = user.tenant_id or uuid4()
            if method == "PATCH":
                body = await self._read_json(receive)
                req_up_u = UserUpdateRequest.model_validate(body)
                up_u = await self.service.update_user(
                    tenant_id=tenant_id,
                    user_id=target_uid,
                    access_level=req_up_u.access_level,
                    is_active=req_up_u.is_active,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, up_u.model_dump(mode="json"))
                return
            if method == "DELETE":
                await self.service.delete_user(
                    tenant_id=tenant_id,
                    user_id=target_uid,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, {"status": "ok", "message": "User deleted"})
                return

        # Groups CRUD
        if path == "/api/v1/admin/groups":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            tenant_id = user.tenant_id or uuid4()
            if method == "GET":
                query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                page = int(query_params.get("page", ["1"])[0])
                per_page = int(query_params.get("per_page", ["50"])[0])
                grp_page = await self.service.list_groups(tenant_id, page=page, per_page=per_page)
                await self._send_json(send, grp_page.model_dump(mode="json"))
                return
            if method == "POST":
                self._verify_csrf(method, headers, token)
                body = await self._read_json(receive)
                req_g = GroupCreateRequest.model_validate(body)
                created_g = await self.service.create_group(
                    tenant_id=tenant_id,
                    name=req_g.name,
                    access_level=req_g.access_level,
                    external_ref=req_g.external_ref,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, created_g.model_dump(mode="json"), status=201)
                return

        m_grp = re.match(r"^/api/v1/admin/groups/([0-9a-fA-F-]+)$", path)
        if m_grp and method == "DELETE":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            grp_id = UUID(m_grp.group(1))
            tenant_id = user.tenant_id or uuid4()
            await self.service.delete_group(
                tenant_id,
                grp_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Group deleted"})
            return

        m_grp_mem = re.match(r"^/api/v1/admin/groups/([0-9a-fA-F-]+)/members$", path)
        if m_grp_mem and method == "POST":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            grp_id = UUID(m_grp_mem.group(1))
            tenant_id = user.tenant_id or uuid4()
            body = await self._read_json(receive)
            req_mem = GroupMemberAddRequest.model_validate(body)
            await self.service.add_group_member(
                tenant_id,
                grp_id,
                req_mem.user_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Member added to group"})
            return

        m_grp_mem_del = re.match(
            r"^/api/v1/admin/groups/([0-9a-fA-F-]+)/members/([0-9a-fA-F-]+)$", path
        )
        if m_grp_mem_del and method == "DELETE":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            grp_id = UUID(m_grp_mem_del.group(1))
            member_user_id = UUID(m_grp_mem_del.group(2))
            tenant_id = user.tenant_id or uuid4()
            await self.service.remove_group_member(
                tenant_id,
                grp_id,
                member_user_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Member removed from group"})
            return

        # Collections CRUD
        if path == "/api/v1/admin/collections":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            tenant_id = user.tenant_id or uuid4()
            if method == "GET":
                query_params = parse_qs(scope.get("query_string", b"").decode("utf-8"))
                page = int(query_params.get("page", ["1"])[0])
                per_page = int(query_params.get("per_page", ["50"])[0])
                col_page = await self.service.list_collections(
                    tenant_id, page=page, per_page=per_page
                )
                await self._send_json(send, col_page.model_dump(mode="json"))
                return
            if method == "POST":
                self._verify_csrf(method, headers, token)
                body = await self._read_json(receive)
                req_c = CollectionCreateRequest.model_validate(body)
                created_c = await self.service.create_collection(
                    tenant_id=tenant_id,
                    name=req_c.name,
                    access_level=req_c.access_level,
                    parent_id=req_c.parent_id,
                    actor_user_id=user.id,
                    actor_email=user.email,
                    ip_address=client_ip,
                    user_agent=user_agent,
                )
                await self._send_json(send, created_c.model_dump(mode="json"), status=201)
                return

        m_col = re.match(r"^/api/v1/admin/collections/([0-9a-fA-F-]+)$", path)
        if m_col and method == "DELETE":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            col_id = UUID(m_col.group(1))
            tenant_id = user.tenant_id or uuid4()
            await self.service.delete_collection(
                tenant_id,
                col_id,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "Collection deleted"})
            return

        # Document ACL
        m_doc_acl = re.match(r"^/api/v1/admin/documents/([0-9a-fA-F-]+)/acl$", path)
        if m_doc_acl and method == "POST":
            user, token = await self._authenticate_request(headers, required_role=AdminRole.ADMIN)
            self._verify_csrf(method, headers, token)
            doc_id = UUID(m_doc_acl.group(1))
            tenant_id = user.tenant_id or uuid4()
            body = await self._read_json(receive)
            req_acl = DocumentAclCreateRequest.model_validate(body)
            await self.service.grant_document_acl(
                tenant_id=tenant_id,
                document_id=doc_id,
                principal_type=req_acl.principal_type,
                principal_id=req_acl.principal_id,
                permission=req_acl.permission,
                actor_user_id=user.id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._send_json(send, {"status": "ok", "message": "ACL granted"})
            return

        # Static SPA file distribution (Box mode, with path traversal prevention)
        if method == "GET":
            await self._handle_static_spa(path, send)
            return

        await self._send_json(send, {"error": "Not Found", "path": path}, status=404)

    # -------------------------------------------------------------------------
    # Authentication handlers
    # -------------------------------------------------------------------------
    async def _handle_login(
        self,
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        headers: dict[str, str],
        client_ip: str,
        user_agent: str | None,
    ) -> None:
        body = await self._read_json(receive)
        req = AdminLoginRequest.model_validate(body)

        # Look up user in DB
        db_user = self.service.get_admin_user_db_by_email_sync(req.email)
        if not db_user:
            self.service.log_auth_event(
                tenant_id=None,
                actor_user_id=None,
                actor_email=req.email,
                event_type="login_failed",
                ip_address=client_ip,
                user_agent=user_agent,
                payload={"reason": "user_not_found"},
            )
            raise AuthError("Invalid email or password", status_code=401)

        if not db_user.is_active:
            raise AuthError("Account is deactivated", status_code=403)

        if not verify_password(req.password, db_user.hashed_password):
            self.service.log_auth_event(
                tenant_id=db_user.tenant_id,
                actor_user_id=db_user.id,
                actor_email=db_user.email,
                event_type="login_failed",
                ip_address=client_ip,
                user_agent=user_agent,
                payload={"reason": "password_mismatch"},
            )
            raise AuthError("Invalid email or password", status_code=401)

        # Enforce 2FA TOTP check
        is_admin_role = db_user.role == AdminRole.ADMIN.value
        totp_req_admin = is_admin_role and self.settings.totp_required_for_admin

        mfa_verified = True
        if db_user.totp_enabled:
            if not req.totp_code or not db_user.totp_secret:
                raise AuthError("2FA TOTP code required", status_code=401)
            if not verify_totp_code(db_user.totp_secret, req.totp_code):
                self.service.log_auth_event(
                    tenant_id=db_user.tenant_id,
                    actor_user_id=db_user.id,
                    actor_email=db_user.email,
                    event_type="login_failed",
                    ip_address=client_ip,
                    user_agent=user_agent,
                    payload={"reason": "invalid_totp"},
                )
                raise AuthError("Invalid 2FA TOTP code", status_code=401)
        elif totp_req_admin:
            # Mandatory 2FA for the admin role is not yet enrolled: issue a LIMITED session
            # (mfa=False) that can only reach /auth/me and /auth/2fa/* until enrollment completes.
            mfa_verified = False

        user_read = AdminUserRead(
            id=db_user.id,
            tenant_id=db_user.tenant_id,
            email=db_user.email,
            role=AdminRole(db_user.role),
            is_active=db_user.is_active,
            is_superuser=db_user.is_superuser,
            is_verified=db_user.is_verified,
            totp_enabled=db_user.totp_enabled,
            created_at=db_user.created_at,
            updated_at=db_user.updated_at,
        )

        # Create session JWT
        token = create_admin_session_jwt(
            user_id=user_read.id,
            email=user_read.email,
            role=user_read.role,
            tenant_id=user_read.tenant_id,
            secret=self.jwt_secret,
            ttl_seconds=self.settings.session_ttl_seconds,
            mfa_verified=mfa_verified,
        )

        csrf_token = generate_csrf_token()
        # Bound CSRF store
        if len(self._csrf_store) > 10000:
            self._csrf_store.clear()
        self._csrf_store[token] = csrf_token

        self.service.log_auth_event(
            tenant_id=db_user.tenant_id,
            actor_user_id=db_user.id,
            actor_email=db_user.email,
            event_type="login",
            ip_address=client_ip,
            user_agent=user_agent,
        )

        cookie_name = self.settings.session_cookie_name
        cookie_val = (
            f"{cookie_name}={token}; "
            f"Path=/api/v1; "
            f"Max-Age={self.settings.session_ttl_seconds}; "
            f"HttpOnly; "
            f"SameSite={self.settings.session_cookie_samesite.capitalize()}"
        )
        if self.settings.session_cookie_secure:
            cookie_val += "; Secure"

        response_obj = AdminLoginResponse(user=user_read, csrf_token=csrf_token)
        payload_bytes = json.dumps(response_obj.model_dump(mode="json")).encode("utf-8")

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(payload_bytes)).encode("ascii")],
                    [b"set-cookie", cookie_val.encode("ascii")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload_bytes})

    async def _handle_logout(
        self,
        send: Callable[[dict[str, Any]], Any],
        headers: dict[str, str],
        client_ip: str,
        user_agent: str | None,
    ) -> None:
        cookies = _parse_cookies(headers.get("cookie", ""))
        session_token = cookies.get(self.settings.session_cookie_name)
        if session_token:
            if session_token in self._csrf_store:
                del self._csrf_store[session_token]
            try:
                payload = verify_admin_session_jwt(session_token, secret=self.jwt_secret)
                uid_str = payload.get("sub")
                if uid_str:
                    self.service.log_auth_event(
                        tenant_id=UUID(payload["tenant_id"]) if payload.get("tenant_id") else None,
                        actor_user_id=UUID(uid_str),
                        actor_email=payload.get("email"),
                        event_type="logout",
                        ip_address=client_ip,
                        user_agent=user_agent,
                    )
            except Exception:
                pass

        cookie_val = (
            f"{self.settings.session_cookie_name}=; Path=/api/v1; Max-Age=0; HttpOnly; "
            f"SameSite={self.settings.session_cookie_samesite.capitalize()}"
        )
        res_bytes = b'{"status":"ok","message":"Logged out"}'
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"application/json"],
                    [b"content-length", str(len(res_bytes)).encode("ascii")],
                    [b"set-cookie", cookie_val.encode("ascii")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": res_bytes})

    # -------------------------------------------------------------------------
    # Ingest document upload
    # -------------------------------------------------------------------------
    async def _handle_document_upload(
        self,
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        tenant_id: UUID,
        user: AdminUserRead,
        client_ip: str,
        user_agent: str | None,
    ) -> None:
        body = await self._read_json(receive)
        file_name = body.get("file_name", "unnamed.bin")
        content_b64 = body.get("content_b64", "")
        access_level_int = int(body.get("access_level", 0))
        mime_type = body.get("mime_type", "application/octet-stream")

        content = base64.b64decode(content_b64)
        doc_item = await self.service.upload_document(
            tenant_id=tenant_id,
            file_name=file_name,
            content=content,
            mime_type=mime_type,
            access_level=AccessLevel(access_level_int),
            actor_user_id=user.id,
            actor_email=user.email,
            ip_address=client_ip,
            user_agent=user_agent,
        )
        await self._send_json(send, doc_item.model_dump(mode="json"), status=201)

    # -------------------------------------------------------------------------
    # SSE Live Ingest Stream
    # -------------------------------------------------------------------------
    async def _handle_sse_stream(
        self, send: Callable[[dict[str, Any]], Any], tenant_id: UUID
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", b"text/event-stream"],
                    [b"cache-control", b"no-cache"],
                    [b"connection", b"keep-alive"],
                    [b"x-accel-buffering", b"no"],
                ],
            }
        )

        async for chunk in self.service.stream_ingest_events(tenant_id, poll_interval=1.5):
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk.encode("utf-8"),
                    "more_body": True,
                }
            )

    # -------------------------------------------------------------------------
    # Static SPA file distribution with Path Traversal Protection
    # -------------------------------------------------------------------------
    async def _handle_static_spa(self, path: str, send: Callable[[dict[str, Any]], Any]) -> None:
        dist_dir = Path("apps/admin/dist").resolve()
        if not dist_dir.exists():
            await self._send_json(
                send,
                {"error": "Admin SPA dist folder not found", "path": path},
                status=404,
            )
            return

        clean_path = path.lstrip("/")
        if clean_path.startswith("admin/"):
            clean_path = clean_path[len("admin/") :]

        target_file = (dist_dir / clean_path).resolve()

        # Strict containment check against path traversal
        if not str(target_file).startswith(str(dist_dir)):
            await self._send_json(
                send, {"error": "Forbidden", "message": "Access denied"}, status=403
            )
            return

        if not target_file.exists() or target_file.is_dir():
            target_file = (dist_dir / "index.html").resolve()

        if not target_file.exists():
            await self._send_json(send, {"error": "index.html not found"}, status=404)
            return

        mime_type, _ = mimetypes.guess_type(str(target_file))
        mime_type = mime_type or "application/octet-stream"

        content = target_file.read_bytes()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    [b"content-type", mime_type.encode("ascii")],
                    [b"content-length", str(len(content)).encode("ascii")],
                ],
            }
        )
        await send({"type": "http.response.body", "body": content})

    # -------------------------------------------------------------------------
    # Helper utilities
    # -------------------------------------------------------------------------
    async def _read_json(self, receive: Callable[[], Any]) -> dict[str, Any]:
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        if not body:
            return {}
        try:
            return json.loads(body.decode("utf-8"))  # type: ignore[no-any-return]
        except Exception as exc:
            raise ValueError(f"Malformed JSON body: {exc}") from exc

    async def _send_json(
        self,
        send: Callable[[dict[str, Any]], Any],
        payload: Any,
        status: int = 200,
        headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        hdrs = headers or []
        hdrs.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
        )
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})


# The module-level ASGI instance for `uvicorn services.api.admin_app:app`
# (core/eval_and_ops.md §3.2): the private Admin contour runs on 127.0.0.1:8001,
# mirroring the widget contour's `app = WidgetAPIApp()`.
app = AdminAPIApp()
