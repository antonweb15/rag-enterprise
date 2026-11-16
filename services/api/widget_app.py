"""Widget API application (`services/api/widget_app.py`).

The public, anonymous, high-load contour (`core/eval_and_ops.md` §3.2, `core/channels.md` §5).
Runs on a separate process/port (0.0.0.0:8002) for independent scaling and DDoS isolation.

Endpoints:
- `POST /api/v1/widget/session`: Two-step session handshake with Origin check;
- `GET /api/v1/widget/config`: Public branding and channel capabilities;
- `POST /api/v1/chat/stream`: SSE chat streaming with server-side rate-limiting;
- `GET /widget/v1.js`: Widget embed bundle serving;
- `GET /healthz`: Public liveness healthcheck.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from uuid import UUID, uuid4

from rag_core.channels.branding import sanitize_widget_branding
from rag_core.channels.capabilities import get_channel_capabilities
from rag_core.channels.rate_limit import InMemoryRateLimiter, RateLimitExceeded
from rag_core.channels.rest_sse import RestSSEChannel, format_sse_event
from rag_core.channels.session import (
    SessionSecurityError,
    build_widget_user_context,
    create_session_jwt,
    validate_origin,
    verify_session_jwt,
)
from rag_core.contracts.enums import AccessLevel, ChannelType
from rag_core.contracts.interfaces import BaseTenantResolver
from rag_core.contracts.models import (
    TenantContext,
    WebWidgetSessionRequest,
    WebWidgetSessionResponse,
    WidgetBranding,
)
from rag_core.contracts.settings import Settings
from rag_core.generation.pipeline import GenerationPipeline

logger = logging.getLogger(__name__)


class WidgetAPIApp:
    """Public ASGI Application for the Web Widget contour."""

    def __init__(
        self,
        settings: Settings | None = None,
        generation_pipeline: GenerationPipeline | None = None,
        tenants: dict[str, TenantContext] | None = None,
        brandings: dict[str, WidgetBranding] | None = None,
        rate_limiter: InMemoryRateLimiter | None = None,
        tenant_resolver: BaseTenantResolver | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.pipeline = generation_pipeline
        self.tenant_resolver = tenant_resolver
        # Fail-closed: no tenant is served until a registry or a BaseTenantResolver is wired.
        # The public contour must never fall back to a baked-in default tenant.
        self.tenants: dict[str, TenantContext] = tenants if tenants is not None else {}
        self.brandings: dict[str, WidgetBranding] = brandings if brandings is not None else {}
        self.rate_limiter = rate_limiter or InMemoryRateLimiter(
            max_requests_per_minute=self.settings.widget_rate_limit_per_minute
        )
        # Fail-closed secret: Use configured JWT secret, or generate a cryptographically
        # secure random ephemeral secret on initialization if none was configured
        self.jwt_secret = self.settings.jwt_secret or secrets.token_urlsafe(32)
        self.sse_channel = RestSSEChannel()

    async def _resolve_tenant(self, slug: str) -> TenantContext | None:
        """Resolves tenant from in-memory registry or BaseTenantResolver."""
        if slug in self.tenants:
            return self.tenants[slug]
        if self.tenant_resolver is not None:
            try:
                resolved = await self.tenant_resolver.resolve_tenant(slug)
                if resolved and resolved.is_active:
                    return resolved
            except Exception as exc:
                logger.warning("Tenant resolver failed for slug %s: %s", slug, exc)
        return None

    def register_tenant(
        self, tenant: TenantContext, branding: WidgetBranding | None = None
    ) -> None:
        """Registers a tenant and optional branding in memory."""
        self.tenants[tenant.slug] = tenant
        if branding:
            self.brandings[tenant.slug] = sanitize_widget_branding(branding)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        """ASGI 3.0 entry point."""
        if scope["type"] != "http":
            return

        method = scope["method"].upper()
        path = scope["path"].rstrip("/") or "/"
        headers = {
            k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])
        }
        client = scope.get("client") or ("127.0.0.1", 0)
        client_ip = client[0]

        if path == "/healthz" and method == "GET":
            await self._send_json(send, 200, {"status": "ok", "contour": "widget-api"})
            return

        if path == "/api/v1/widget/session" and method == "POST":
            await self._handle_session(receive, send, headers)
            return

        if path == "/api/v1/widget/config" and method == "GET":
            await self._handle_config(scope, send)
            return

        if path == "/api/v1/chat/stream" and method == "POST":
            await self._handle_chat_stream(receive, send, headers, client_ip)
            return

        if path in ("/widget/v1.js", "/widget/v1"):
            await self._handle_static_widget_js(send)
            return

        await self._send_json(send, 404, {"error": "Not Found", "path": path})

    async def _handle_session(
        self,
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        headers: dict[str, str],
    ) -> None:
        """Handles POST /api/v1/widget/session (Two-step session handshake)."""
        body_bytes = await self._read_body(receive)
        try:
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            req = WebWidgetSessionRequest.model_validate(body_json)
        except Exception as exc:
            await self._send_json(send, 400, {"error": f"Invalid request body: {exc}"})
            return

        tenant = await self._resolve_tenant(req.slug)
        if not tenant or not tenant.is_active:
            await self._send_json(
                send, 404, {"error": f"Tenant '{req.slug}' not found or inactive"}
            )
            return

        # Strict Origin allowlist validation without wildcards
        origin = headers.get("origin")
        if not validate_origin(origin, tenant.allowed_origins):
            logger.warning(
                "Rejected widget handshake for tenant '%s' with disallowed Origin '%s'",
                req.slug,
                origin,
            )
            await self._send_json(
                send,
                403,
                {"error": "Origin not allowed by tenant policy"},
            )
            return

        # Resolve access level and user
        if req.auth_mode == "anonymous":
            user_id = uuid4()
            access_level = AccessLevel.PUBLIC
            subject = None
            group_ids: list[UUID] = []
        elif req.auth_mode == "oidc":
            if not req.id_token:
                await self._send_json(send, 401, {"error": "Missing id_token for oidc auth_mode"})
                return
            # Fail closed until verified IdP OIDC signature check and tenant claim mapping are wired
            await self._send_json(
                send,
                501,
                {"error": "OIDC auth_mode verification is not yet enabled on this contour"},
            )
            return
        else:
            await self._send_json(send, 400, {"error": f"Unsupported auth_mode: {req.auth_mode}"})
            return

        token = create_session_jwt(
            tenant_id=tenant.tenant_id,
            user_id=user_id,
            auth_mode=req.auth_mode,
            secret=self.jwt_secret,
            ttl_seconds=3600,
            access_level=access_level,
            group_ids=group_ids,
            subject=subject,
        )

        resp = WebWidgetSessionResponse(
            session_token=token,
            tenant_id=tenant.tenant_id,
            expires_in=3600,
            auth_mode=req.auth_mode,
        )
        await self._send_json(send, 200, resp.model_dump(mode="json"))

    async def _handle_config(
        self,
        scope: dict[str, Any],
        send: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Handles GET /api/v1/widget/config."""
        query_string = scope.get("query_string", b"").decode("latin1")
        query_params = parse_qs(query_string)
        slug_list = query_params.get("slug")
        if not slug_list or not slug_list[0].strip():
            await self._send_json(send, 400, {"error": "Missing required query parameter 'slug'"})
            return
        slug = slug_list[0].strip()

        tenant = await self._resolve_tenant(slug)
        if not tenant:
            await self._send_json(send, 404, {"error": f"Tenant '{slug}' not found"})
            return

        raw_branding = self.brandings.get(slug, WidgetBranding())
        sanitized = sanitize_widget_branding(raw_branding)
        capabilities = get_channel_capabilities(ChannelType.WEB_WIDGET)

        data = {
            "slug": slug,
            "tenant_id": str(tenant.tenant_id),
            "deployment_mode": self.settings.deployment_mode.value,
            "branding": sanitized.model_dump(mode="json"),
            "capabilities": capabilities.model_dump(mode="json"),
        }
        await self._send_json(send, 200, data)

    async def _handle_chat_stream(
        self,
        receive: Callable[[], Any],
        send: Callable[[dict[str, Any]], Any],
        headers: dict[str, str],
        client_ip: str,
    ) -> None:
        """Handles POST /api/v1/chat/stream with SSE response."""
        auth_header = headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            await self._send_json(send, 401, {"error": "Missing or invalid Authorization header"})
            return

        token = auth_header[len("Bearer ") :].strip()
        try:
            claims = verify_session_jwt(token, self.jwt_secret)
        except SessionSecurityError as exc:
            await self._send_json(send, 401, {"error": f"Unauthorized session token: {exc}"})
            return

        body_bytes = await self._read_body(receive)
        try:
            body_json = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
            query_text = body_json.get("query") or body_json.get("message") or ""
            session_id = body_json.get("session_id") or body_json.get("conversation_id")
        except Exception as exc:
            await self._send_json(send, 400, {"error": f"Invalid JSON body: {exc}"})
            return

        if not query_text.strip():
            await self._send_json(send, 400, {"error": "Query text cannot be empty"})
            return

        # Rate limiting check by IP and session (claims['sub'])
        user_sub = claims.get("sub", "anon")
        rate_key = f"{client_ip}:{user_sub}"
        try:
            await self.rate_limiter.check_and_record(rate_key)
        except RateLimitExceeded as exc:
            await self._send_json(
                send,
                429,
                {"error": str(exc)},
                headers=[("retry-after", str(exc.retry_after))],
            )
            return

        user_ctx = build_widget_user_context(claims=claims)

        # Stream SSE response
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-cache"),
                    (b"connection", b"keep-alive"),
                    (b"x-accel-buffering", b"no"),
                ],
            }
        )

        try:
            if self.pipeline is not None:
                # Use GenerationPipeline
                rag_resp = await self.pipeline.generate_response(
                    user=user_ctx,
                    query=query_text,
                    session_id=session_id,
                )

                async def token_generator() -> Any:
                    for token_word in rag_resp.answer.split(" "):
                        yield token_word + " "
                        await asyncio.sleep(0.01)

                async for frame in self.sse_channel.format_stream_events(
                    user_id=str(user_ctx.user_id),
                    token_stream=token_generator(),
                    citations_fn=lambda _: rag_resp.citations,
                    trace_id=rag_resp.trace_id,
                    confidence_passed=rag_resp.confidence_passed,
                ):
                    await send(
                        {
                            "type": "http.response.body",
                            "body": frame.encode("utf-8"),
                            "more_body": True,
                        }
                    )
                # Send terminal empty chunk
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                )
            else:
                # Fallback mock response when no pipeline is wired
                async def mock_token_generator() -> Any:
                    mock_tokens = ["This ", "is ", "the ", "Enterprise ", "RAG ", "response."]
                    for tok in mock_tokens:
                        yield tok
                        await asyncio.sleep(0.01)

                async for frame in self.sse_channel.format_stream_events(
                    user_id=str(user_ctx.user_id),
                    token_stream=mock_token_generator(),
                    trace_id="mock_trace",
                ):
                    await send(
                        {
                            "type": "http.response.body",
                            "body": frame.encode("utf-8"),
                            "more_body": True,
                        }
                    )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"",
                        "more_body": False,
                    }
                )

        except Exception as exc:
            err_frame = format_sse_event("error", {"error": str(exc)})
            await send(
                {
                    "type": "http.response.body",
                    "body": err_frame.encode("utf-8"),
                    "more_body": False,
                }
            )

    async def _handle_static_widget_js(self, send: Callable[[dict[str, Any]], Any]) -> None:
        """Serves the widget embed script bundle."""
        dist_path = Path(__file__).resolve().parent.parent.parent / "apps" / "widget" / "dist"
        js_file = None
        if dist_path.exists():
            for f in (dist_path / "assets").glob("*.js"):
                js_file = f
                break

        if js_file and js_file.exists():
            content = js_file.read_bytes()
        else:
            content = b"/* Enterprise RAG Widget v1 bundle */\nconsole.log('RAG Widget loaded');"

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/javascript; charset=utf-8"),
                    (b"cache-control", b"public, max-age=3600"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": content, "more_body": False})

    async def _read_body(self, receive: Callable[[], Any]) -> bytes:
        chunks: list[bytes] = []
        while True:
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body", False):
                    break
        return b"".join(chunks)

    async def _send_json(
        self,
        send: Callable[[dict[str, Any]], Any],
        status: int,
        data: dict[str, Any],
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        resp_headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        if headers:
            for k, v in headers:
                resp_headers.append((k.encode("latin1"), v.encode("latin1")))

        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": resp_headers,
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


app = WidgetAPIApp()
