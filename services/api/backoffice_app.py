"""Vendor Backoffice API application (`core/vendor_backoffice.md` §5, Stage L).

A SEPARATE ASGI contour mounted at ``/api/v1/backoffice/*`` and run on its own host/port
(``backoffice_host``/``backoffice_port``), NEVER mixed with the tenant admin panel and NEVER part
of the ``on_premise_box`` artifact. It reuses the audited security primitives (password hashing,
TOTP 2FA, double-submit CSRF) but has its OWN session cookie, JWT context claim (``ctx=backoffice``)
and capability-based RBAC (:class:`BackofficeCapability`). Every mutating endpoint maps to a single
capability and all break-glass impersonation is mandatorily audited by the service layer.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID

from rag_core.admin.auth import (
    AuthError,
    CSRFError,
    RBACPermissionDeniedError,
    generate_csrf_token,
    verify_csrf_token,
)
from rag_core.backoffice.auth import (
    BackofficeCapability,
    create_backoffice_session_jwt,
    enforce_backoffice_capability,
    verify_backoffice_session_jwt,
)
from rag_core.backoffice.service import BackofficeError, BackofficeService
from rag_core.contracts.enums import ActivationCodeStatus, CustomerStatus, LicensePlan
from rag_core.contracts.models import (
    ActivationCodeCreateRequest,
    BackofficeLoginRequest,
    BackofficeLoginResponse,
    BackofficeUserCreate,
    BackofficeUserRead,
    CustomerCreateRequest,
    ImpersonationRequest,
    LicenseExtendRequest,
    LicenseIssueRequest,
    LicenseRevokeRequest,
    LicenseUpgradeRequest,
    TenantOffboardRequest,
    TenantProvisionRequest,
    TenantRestoreRequest,
)
from rag_core.contracts.settings import Settings
from rag_core.licensing.tokens import TokenSigner

logger = logging.getLogger(__name__)

BACKOFFICE_PREFIX = "/api/v1/backoffice"
BACKOFFICE_COOKIE_NAME = "rag_backoffice_session"


class _RateLimitExceeded(Exception):
    """The per-IP backoffice rate-limit pool has been exhausted (HTTP 429)."""


def _parse_cookies(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    if not cookie_header:
        return cookies
    for item in cookie_header.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


class BackofficeAPIApp:
    """Private ASGI 3.0 application for the vendor (super-admin) contour."""

    def __init__(
        self,
        settings: Settings | None = None,
        service: BackofficeService | None = None,
        signer: TokenSigner | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        if service is not None:
            self.service = service
        else:
            if session_factory is None:
                from rag_core.db.session import build_session_factory

                session_factory = build_session_factory(self.settings.postgres_dsn)
            self.service = BackofficeService(
                session_factory=session_factory, settings=self.settings, signer=signer
            )
        secret = self.settings.jwt_secret
        if not secret or len(secret) < 32:
            secret = secrets.token_hex(32)
        self.jwt_secret = secret
        self._csrf_store: dict[str, str] = {}
        # Dedicated per-IP rate-limit pool for the backoffice contour (§2 p.1 / §8).
        self._rate_window: dict[str, list[float]] = {}

    # --- ASGI entrypoint -----------------------------------------------------
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
        raw_qs = scope.get("query_string", b"") or b""
        query = {k: v[-1] for k, v in parse_qs(raw_qs.decode("latin1")).items()}
        raw_headers = dict(scope.get("headers", []))
        headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in raw_headers.items()}
        client = scope.get("client")
        client_ip = str(client[0]) if client and client[0] else "127.0.0.1"
        user_agent = headers.get("user-agent")
        try:
            self._enforce_rate_limit(client_ip)
            await self._route(method, path, headers, query, receive, send, client_ip, user_agent)
        except AuthError as exc:
            await self._json(
                send, {"error": "Unauthorized", "message": exc.message}, exc.status_code
            )
        except RBACPermissionDeniedError as exc:
            await self._json(send, {"error": "Forbidden", "message": exc.message}, 403)
        except _RateLimitExceeded:
            await self._json(send, {"error": "Too Many Requests"}, 429)
        except (BackofficeError, ValueError) as exc:
            await self._json(send, {"error": "Bad Request", "message": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error in BackofficeAPIApp: %s", exc)
            await self._json(send, {"error": "Internal Server Error", "message": str(exc)}, 500)

    def _enforce_rate_limit(self, client_ip: str) -> None:
        """Simple per-IP fixed-window limiter for the dedicated backoffice pool (§2 p.1)."""
        limit = self.settings.backoffice_rate_limit_per_minute
        if limit <= 0:
            return
        now = time.monotonic()
        window = [t for t in self._rate_window.get(client_ip, []) if now - t < 60.0]
        if len(window) >= limit:
            self._rate_window[client_ip] = window
            raise _RateLimitExceeded()
        window.append(now)
        self._rate_window[client_ip] = window

    # --- authentication & RBAC ----------------------------------------------
    def _authenticate(
        self, headers: dict[str, str], require_mfa: bool = True
    ) -> tuple[BackofficeUserRead, str]:
        cookies = _parse_cookies(headers.get("cookie", ""))
        token = cookies.get(BACKOFFICE_COOKIE_NAME)
        if not token:
            raise AuthError("Missing backoffice session cookie", status_code=401)
        payload = verify_backoffice_session_jwt(token, secret=self.jwt_secret)
        if require_mfa and payload.get("mfa") is False:
            raise AuthError("2FA enrollment required", status_code=403)
        sub = payload.get("sub")
        if not sub:
            raise AuthError("Malformed session token", status_code=401)
        user = self.service.get_backoffice_user(UUID(sub))
        if user is None:
            raise AuthError("Backoffice user no longer exists", status_code=401)
        if not user.is_active:
            raise AuthError("Backoffice account is deactivated", status_code=403)
        return user, token

    def _verify_csrf(self, method: str, headers: dict[str, str], token: str) -> None:
        if method in ("GET", "HEAD", "OPTIONS"):
            return
        if not self.settings.csrf_enabled:
            return
        header_csrf = headers.get("x-csrf-token")
        stored = self._csrf_store.get(token)
        if not verify_csrf_token(header_csrf, stored):
            raise CSRFError("CSRF token missing or invalid")

    def _guard(
        self,
        method: str,
        headers: dict[str, str],
        capability: BackofficeCapability,
    ) -> BackofficeUserRead:
        """Authenticates, enforces CSRF on unsafe methods, and checks the capability."""
        user, token = self._authenticate(headers)
        self._verify_csrf(method, headers, token)
        enforce_backoffice_capability(user.role, capability, is_active=user.is_active)
        return user

    # --- router --------------------------------------------------------------
    async def _route(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, str],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        client_ip: str,
        user_agent: str | None,
    ) -> None:
        if not path.startswith(BACKOFFICE_PREFIX):
            await self._json(send, {"error": "Not Found", "path": path}, 404)
            return
        sub = path[len(BACKOFFICE_PREFIX) :] or "/"

        page = int(query.get("page", "1") or "1")

        # --- auth endpoints (no capability required) ---
        if sub == "/auth/login" and method == "POST":
            await self._login(receive, send)
            return
        if sub == "/auth/logout" and method == "POST":
            await self._logout(headers, send)
            return
        if sub == "/auth/me" and method == "GET":
            me, _ = self._authenticate(headers, require_mfa=False)
            await self._json(send, me.model_dump(mode="json"))
            return
        if sub == "/auth/2fa/setup" and method == "POST":
            me, token = self._authenticate(headers, require_mfa=False)
            self._verify_csrf(method, headers, token)
            totp = self.service.setup_totp(me.id)
            await self._json(send, totp.model_dump(mode="json"))
            return

        # --- dashboard ---
        if sub == "/dashboard" and method == "GET":
            self._guard(method, headers, BackofficeCapability.DASHBOARD_VIEW)
            await self._json(send, self.service.get_dashboard().model_dump(mode="json"))
            return

        # --- backoffice users (superadmin only, §5) ---
        if sub == "/users" and method == "GET":
            self._guard(method, headers, BackofficeCapability.USER_MANAGE)
            await self._json(
                send, self.service.list_backoffice_users(page=page).model_dump(mode="json")
            )
            return
        if sub == "/users" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.USER_MANAGE)
            body = await self._read_json(receive)
            user_req = BackofficeUserCreate.model_validate(body)
            created = self.service.create_backoffice_user(
                user_req.email,
                user_req.password,
                user_req.role,
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._json(send, created.model_dump(mode="json"), 201)
            return

        # --- customers ---
        if sub == "/customers" and method == "GET":
            self._guard(method, headers, BackofficeCapability.CUSTOMER_VIEW)
            cust_status = CustomerStatus(query["status"]) if query.get("status") else None
            await self._json(
                send,
                self.service.list_customers(
                    page=page, status=cust_status, q=query.get("q")
                ).model_dump(mode="json"),
            )
            return
        if sub == "/customers" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.CUSTOMER_WRITE)
            body = await self._read_json(receive)
            customer_req = CustomerCreateRequest.model_validate(body)
            customer = self.service.create_customer(
                customer_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, customer.model_dump(mode="json"), 201)
            return
        if sub.startswith("/customers/") and sub.endswith("/tenants") and method == "GET":
            self._guard(method, headers, BackofficeCapability.CUSTOMER_VIEW)
            tenants = self.service.list_customer_tenants(UUID(sub.split("/")[2]))
            await self._json(send, tenants)
            return
        if sub.startswith("/customers/") and method == "GET":
            self._guard(method, headers, BackofficeCapability.CUSTOMER_VIEW)
            found = self.service.get_customer(UUID(sub.split("/")[2]))
            if found is None:
                await self._json(send, {"error": "Not Found"}, 404)
                return
            await self._json(send, found.model_dump(mode="json"))
            return

        # --- orders ---
        if sub == "/orders" and method == "GET":
            self._guard(method, headers, BackofficeCapability.ORDER_VIEW)
            customer_id = UUID(query["customer_id"]) if query.get("customer_id") else None
            await self._json(
                send,
                self.service.list_orders(
                    customer_id=customer_id, status=query.get("status"), page=page
                ).model_dump(mode="json"),
            )
            return
        if sub == "/orders" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.ORDER_WRITE)
            body = await self._read_json(receive)
            order = self.service.create_order(
                customer_id=UUID(body["customer_id"]),
                plan=LicensePlan(body.get("plan", LicensePlan.STANDARD.value)),
                amount_minor=body.get("amount_minor"),
                currency=body.get("currency", "RUB"),
                external_provider=body.get("external_provider"),
                external_ref=body.get("external_ref"),
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._json(send, order.model_dump(mode="json"), 201)
            return

        # --- activation codes ---
        if sub == "/activation-codes" and method == "GET":
            self._guard(method, headers, BackofficeCapability.CODE_VIEW)
            customer_id = UUID(query["customer_id"]) if query.get("customer_id") else None
            code_status = ActivationCodeStatus(query["status"]) if query.get("status") else None
            await self._json(
                send,
                self.service.list_activation_codes(
                    customer_id=customer_id, status=code_status, page=page
                ).model_dump(mode="json"),
            )
            return
        if sub == "/activation-codes" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.CODE_GENERATE)
            body = await self._read_json(receive)
            code_req = ActivationCodeCreateRequest.model_validate(body)
            code = self.service.generate_activation_code(
                code_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, code.model_dump(mode="json"), 201)
            return
        if sub.startswith("/activation-codes/") and sub.endswith("/revoke") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.CODE_GENERATE)
            code_value = sub.split("/")[2]
            self.service.revoke_activation_code(
                code_value, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, {"status": "ok"})
            return

        # --- licenses (paths per core/vendor_backoffice.md §5) ---
        if sub == "/licenses" and method == "GET":
            self._guard(method, headers, BackofficeCapability.LICENSE_VIEW)
            statuses = self.service.list_licenses()
            await self._json(send, [s.model_dump(mode="json") for s in statuses])
            return
        if sub == "/licenses/issue" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.LICENSE_ISSUE)
            body = await self._read_json(receive)
            issue_req = LicenseIssueRequest.model_validate(body)
            issued = self.service.issue_license(
                issue_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, issued.model_dump(mode="json"), 201)
            return
        if sub == "/licenses/revoke" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.LICENSE_REVOKE)
            body = await self._read_json(receive)
            revoke_req = LicenseRevokeRequest.model_validate(body)
            self.service.revoke_license(revoke_req, actor_user_id=user.id, actor_email=user.email)
            await self._no_content(send)
            return
        if sub.startswith("/licenses/") and sub.endswith("/extend") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.LICENSE_RENEW)
            license_id = UUID(sub.split("/")[2])
            body = await self._read_json(receive)
            extend_req = LicenseExtendRequest.model_validate(body)
            extended = self.service.renew_license(
                license_id, extend_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, extended.model_dump(mode="json"))
            return
        if sub.startswith("/licenses/") and sub.endswith("/upgrade") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.LICENSE_UPGRADE)
            license_id = UUID(sub.split("/")[2])
            body = await self._read_json(receive)
            upgrade_req = LicenseUpgradeRequest.model_validate(body)
            upgraded = self.service.upgrade_license(
                license_id, upgrade_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, upgraded.model_dump(mode="json"))
            return

        # --- box deployments (§5 /box-deployments) ---
        if sub == "/box-deployments" and method == "GET":
            self._guard(method, headers, BackofficeCapability.BOX_VIEW)
            customer_id = UUID(query["customer_id"]) if query.get("customer_id") else None
            stale_hours = int(query["stale_hours"]) if query.get("stale_hours") else None
            await self._json(
                send,
                self.service.list_boxes(
                    customer_id=customer_id, stale_hours=stale_hours, page=page
                ).model_dump(mode="json"),
            )
            return
        if sub == "/box-deployments" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.BOX_WRITE)
            body = await self._read_json(receive)
            box = self.service.register_box(
                customer_id=UUID(body["customer_id"]),
                tenant_id=UUID(body["tenant_id"]) if body.get("tenant_id") else None,
                image_version=body.get("image_version"),
                hardware_id=body.get("hardware_id"),
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._json(send, box.model_dump(mode="json"), 201)
            return

        # --- break-glass impersonation (§5 /tenants/{id}/impersonate; entry + exit audited) ---
        if sub.startswith("/tenants/") and sub.endswith("/impersonate") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.IMPERSONATE)
            target_tenant_id = UUID(sub.split("/")[2])
            body = await self._read_json(receive)
            imp_req = ImpersonationRequest.model_validate(body)
            grant = self.service.grant_impersonation(
                backoffice_user_id=user.id,
                target_tenant_id=target_tenant_id,
                request=imp_req,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._json(send, grant.model_dump(mode="json"), 201)
            return
        if sub.startswith("/tenants/") and sub.endswith("/impersonate") and method == "DELETE":
            user = self._guard(method, headers, BackofficeCapability.IMPERSONATE)
            target_tenant_id = UUID(sub.split("/")[2])
            self.service.end_impersonation(
                backoffice_user_id=user.id,
                target_tenant_id=target_tenant_id,
                actor_email=user.email,
                ip_address=client_ip,
                user_agent=user_agent,
            )
            await self._no_content(send)
            return

        # --- SaaS tenant lifecycle (Stage N: provisioning / offboarding / backup-restore) ---
        if sub == "/tenants/provision" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.TENANT_PROVISION)
            body = await self._read_json(receive)
            prov_req = TenantProvisionRequest.model_validate(body)
            result = self.service.provision_tenant(
                prov_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, result.model_dump(mode="json"), 201)
            return
        if sub == "/tenants/restore" and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.TENANT_BACKUP)
            body = await self._read_json(receive)
            restore_req = TenantRestoreRequest.model_validate(body)
            restored = self.service.restore_tenant(
                restore_req.backup_ref,
                overwrite=restore_req.overwrite,
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._json(send, restored.model_dump(mode="json"), 201)
            return
        if sub.startswith("/tenants/") and sub.endswith("/suspend") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.TENANT_OFFBOARD)
            tenant_id = UUID(sub.split("/")[2])
            body = await self._read_json(receive)
            self.service.suspend_tenant(
                tenant_id,
                suspend=bool(body.get("suspend", True)),
                reason=body.get("reason"),
                actor_user_id=user.id,
                actor_email=user.email,
            )
            await self._no_content(send)
            return
        if sub.startswith("/tenants/") and sub.endswith("/offboard") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.TENANT_OFFBOARD)
            tenant_id = UUID(sub.split("/")[2])
            body = await self._read_json(receive)
            off_req = TenantOffboardRequest.model_validate({**body, "tenant_id": str(tenant_id)})
            off_result = self.service.offboard_tenant(
                off_req, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, off_result.model_dump(mode="json"))
            return
        if sub.startswith("/tenants/") and sub.endswith("/export") and method == "GET":
            self._guard(method, headers, BackofficeCapability.TENANT_OFFBOARD)
            tenant_id = UUID(sub.split("/")[2])
            export = self.service.export_tenant(tenant_id)
            await self._json(send, export.model_dump(mode="json"))
            return
        if sub.startswith("/tenants/") and sub.endswith("/backup") and method == "POST":
            user = self._guard(method, headers, BackofficeCapability.TENANT_BACKUP)
            tenant_id = UUID(sub.split("/")[2])
            backup = self.service.backup_tenant(
                tenant_id, actor_user_id=user.id, actor_email=user.email
            )
            await self._json(send, backup.model_dump(mode="json"), 201)
            return

        # --- audit log (§5 /audit-log?actor=&action=&tenant=&page=) ---
        if sub == "/audit-log" and method == "GET":
            self._guard(method, headers, BackofficeCapability.AUDIT_VIEW)
            tenant_q = query.get("tenant")
            audit = self.service.list_audit_log(
                page=page,
                action=query.get("action"),
                actor=query.get("actor"),
                tenant=UUID(tenant_q) if tenant_q else None,
            )
            await self._json(send, audit.model_dump(mode="json"))
            return

        await self._json(send, {"error": "Not Found", "path": path}, 404)

    # --- auth handlers -------------------------------------------------------
    async def _login(
        self, receive: Callable[[], Any], send: Callable[[dict[str, Any]], Any]
    ) -> None:
        body = await self._read_json(receive)
        req = BackofficeLoginRequest.model_validate(body)
        user, mfa_verified = self.service.authenticate(req.email, req.password, req.totp_code)
        token = create_backoffice_session_jwt(
            user_id=user.id,
            email=user.email,
            role=user.role,
            secret=self.jwt_secret,
            ttl_seconds=self.settings.session_ttl_seconds,
            mfa_verified=mfa_verified,
        )
        csrf_token = generate_csrf_token()
        if len(self._csrf_store) > 10000:
            self._csrf_store.clear()
        self._csrf_store[token] = csrf_token
        cookie_val = (
            f"{BACKOFFICE_COOKIE_NAME}={token}; "
            f"Path={BACKOFFICE_PREFIX}; "
            f"Max-Age={self.settings.session_ttl_seconds}; "
            f"HttpOnly; "
            f"SameSite={self.settings.session_cookie_samesite.capitalize()}"
        )
        if self.settings.session_cookie_secure:
            cookie_val += "; Secure"
        resp = BackofficeLoginResponse(user=user, csrf_token=csrf_token)
        await self._json(
            send,
            resp.model_dump(mode="json"),
            200,
            extra_headers=[(b"set-cookie", cookie_val.encode("ascii"))],
        )

    async def _logout(self, headers: dict[str, str], send: Callable[[dict[str, Any]], Any]) -> None:
        cookies = _parse_cookies(headers.get("cookie", ""))
        token = cookies.get(BACKOFFICE_COOKIE_NAME)
        if token and token in self._csrf_store:
            del self._csrf_store[token]
        cookie_val = (
            f"{BACKOFFICE_COOKIE_NAME}=; Path={BACKOFFICE_PREFIX}; Max-Age=0; HttpOnly; "
            f"SameSite={self.settings.session_cookie_samesite.capitalize()}"
        )
        await self._json(
            send,
            {"status": "ok", "message": "Logged out"},
            200,
            extra_headers=[(b"set-cookie", cookie_val.encode("ascii"))],
        )

    # --- helpers -------------------------------------------------------------
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

    async def _json(
        self,
        send: Callable[[dict[str, Any]], Any],
        payload: Any,
        status: int = 200,
        extra_headers: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        hdrs: list[tuple[bytes, bytes]] = list(extra_headers or [])
        hdrs.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
        )
        await send({"type": "http.response.start", "status": status, "headers": hdrs})
        await send({"type": "http.response.body", "body": body})

    async def _no_content(self, send: Callable[[dict[str, Any]], Any]) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [(b"content-length", b"0")],
            }
        )
        await send({"type": "http.response.body", "body": b""})
