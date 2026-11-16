"""Unit and contract tests for Stage G (Delivery Channels, `core/channels.md`).

Tests:
1. Canonical capabilities for all channels (Web, Telegram, REST/SSE, CLI);
2. Native event normalization to ChannelEvent;
3. Render logic respects channel capabilities;
4. Telegram message splitting (4096 limit) and stream edit throttling (0.8-1.0s);
5. Typed branding validation, limits, and HTML sanitization (XSS defense);
6. Web widget session handshake, short-lived JWT, and fail-closed anonymous access_level=0;
7. Origin allowlist rejects disallowed domains without wildcards;
8. data-api-url mode-dependent behavior (ignored in Cloud, applied in Box);
9. Server-side sliding-window rate limiting;
10. Widget API ASGI endpoints (/api/v1/widget/session, /api/v1/widget/config, /api/v1/chat/stream);
11. Telegram bot runner command handling and feedback callback queries;
12. DI container channel port resolution.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import pytest

from rag_core.channels.branding import (
    sanitize_widget_branding,
    validate_logo_url,
    validate_primary_color,
)
from rag_core.channels.capabilities import (
    get_channel_capabilities,
)
from rag_core.channels.cli import CLIChannel
from rag_core.channels.rate_limit import InMemoryRateLimiter, RateLimitExceeded
from rag_core.channels.rest_sse import RestSSEChannel
from rag_core.channels.session import (
    SessionSecurityError,
    build_widget_user_context,
    create_session_jwt,
    resolve_api_url_for_mode,
    validate_origin,
    verify_session_jwt,
)
from rag_core.channels.telegram import (
    TelegramChannel,
    format_citations_markdown,
    split_telegram_message,
)
from rag_core.channels.web import WebChannel
from rag_core.contracts.enums import (
    AccessLevel,
    ChannelEventType,
    ChannelType,
    DeploymentMode,
)
from rag_core.contracts.interfaces import BaseChannel
from rag_core.contracts.models import (
    GenerationCitation,
    TenantContext,
    WidgetBranding,
)
from rag_core.di.container import Container
from services.api.channels.telegram_bot import TelegramBotRunner
from services.api.widget_app import WidgetAPIApp


# --- 1. Channel Capabilities Contract ---
def test_channel_capabilities_match_canonical_table() -> None:
    """Verifies that each channel's declared capabilities match core/channels.md §3."""
    web_caps = get_channel_capabilities(ChannelType.WEB_WIDGET)
    assert web_caps.supports_streaming is True
    assert web_caps.supports_markdown is True
    assert web_caps.supports_citations is True
    assert web_caps.supports_feedback_buttons is True
    assert web_caps.supports_callback_payloads is True
    assert web_caps.supports_cancellation is True
    assert web_caps.supports_attachments is False
    assert web_caps.max_message_length is None

    tg_caps = get_channel_capabilities(ChannelType.TELEGRAM)
    assert tg_caps.supports_streaming is True
    assert tg_caps.supports_markdown is True
    assert tg_caps.supports_citations is True
    assert tg_caps.supports_feedback_buttons is True
    assert tg_caps.supports_attachments is True
    assert tg_caps.supports_reply_threading is True
    assert tg_caps.supports_commands is True
    assert tg_caps.supports_callback_payloads is True
    assert tg_caps.max_message_length == 4096
    assert tg_caps.max_attachments_per_message > 0

    rest_caps = get_channel_capabilities(ChannelType.REST_SSE)
    assert rest_caps.supports_streaming is True
    assert rest_caps.supports_markdown is False
    assert rest_caps.supports_citations is True
    assert rest_caps.supports_feedback_buttons is False
    assert rest_caps.supports_cancellation is True

    cli_caps = get_channel_capabilities(ChannelType.CLI)
    assert cli_caps.supports_streaming is True
    assert cli_caps.supports_markdown is False
    assert cli_caps.supports_citations is True
    assert cli_caps.supports_commands is True
    assert cli_caps.supports_cancellation is True


# --- 2. Event Normalization for all Channels ---
@pytest.mark.asyncio
async def test_event_normalization_for_all_channels() -> None:
    """Each channel adapter must map native inputs into a unified ChannelEvent."""
    tenant_id = uuid4()
    user_id = uuid4()

    # WebChannel
    web = WebChannel()
    web_event = await web.enqueue_event(
        tenant_id=tenant_id,
        user_id=user_id,
        text="Hello Web",
        session_id="web_session_1",
    )
    assert web_event.channel == ChannelType.WEB_WIDGET
    assert web_event.text == "Hello Web"
    assert web_event.event_type == ChannelEventType.TEXT_MESSAGE

    # TelegramChannel
    tg = TelegramChannel()
    tg_event = await tg.normalize_native_event(
        tenant_id=tenant_id,
        user_id=user_id,
        channel_user_id="123456789",
        text="/start",
        event_type=ChannelEventType.COMMAND,
    )
    assert tg_event.channel == ChannelType.TELEGRAM
    assert tg_event.channel_user_id == "123456789"
    assert tg_event.text == "/start"

    # RestSSEChannel
    rest = RestSSEChannel()
    rest_event = await rest.normalize_request(
        tenant_id=tenant_id,
        user_id=user_id,
        text="API Query",
        session_id="rest_sess",
    )
    assert rest_event.channel == ChannelType.REST_SSE
    assert rest_event.text == "API Query"

    # CLIChannel
    cli = CLIChannel()
    cli_event = await cli.normalize_command(
        tenant_id=tenant_id,
        user_id=user_id,
        command="/help",
    )
    assert cli_event.channel == ChannelType.CLI
    assert cli_event.event_type == ChannelEventType.COMMAND


# --- 3. Render Behavior & Formatting ---
def test_telegram_citations_markdown_formatting() -> None:
    """Telegram formats citations as clean Markdown footnotes."""
    citations = [
        GenerationCitation(
            citation_index=1,
            chunk_id=uuid4(),
            document_title="Security Policy",
            source_uri="s3://docs/sec.pdf",
            page=12,
        ),
        GenerationCitation(
            citation_index=2,
            chunk_id=uuid4(),
            document_title="Employee Handbook",
            source_uri="s3://docs/handbook.docx",
            page=None,
        ),
    ]
    formatted = format_citations_markdown(citations)
    assert "**Sources:**" in formatted
    assert "[1] Security Policy, p. 12" in formatted
    assert "[2] Employee Handbook" in formatted


def test_telegram_message_splitting_respects_max_length() -> None:
    """Messages > 4096 chars are split into paragraphs/chunks <= 4096 chars."""
    short_text = "Hello world"
    assert split_telegram_message(short_text) == ["Hello world"]

    long_text = "A" * 3000 + "\n\n" + "B" * 3000
    chunks = split_telegram_message(long_text, max_length=4096)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len(chunk) <= 4096
    assert chunks[0].startswith("A")
    assert chunks[1].startswith("B")


# --- 4. Telegram Streaming Throttling ---
@pytest.mark.asyncio
async def test_telegram_streaming_throttling() -> None:
    """Telegram channel throttles edit updates (0.8-1.0s interval) to prevent 429."""
    edited_calls: list[tuple[str, str, str, Any]] = []

    def mock_edit(user_id: str, msg_id: str, text: str, markup: Any) -> None:
        edited_calls.append((user_id, msg_id, text, markup))

    channel = TelegramChannel(throttle_seconds=0.1, edit_message_fn=mock_edit)

    async def token_gen() -> Any:
        for i in range(5):
            yield f"token_{i} "
            await asyncio.sleep(0.04)

    resp = await channel.stream_response("user_123", token_gen(), initial_message_id="msg_99")
    assert "token_0 token_1 token_2 token_3 token_4" in resp.answer

    # Must contain throttled intermediate updates and final edit with keyboard
    assert len(edited_calls) >= 2
    final_call = edited_calls[-1]
    assert final_call[0] == "user_123"
    assert final_call[1] == "msg_99"


# --- 5. Branding Sanitization & Limits ---
def test_branding_sanitization_and_xss_protection() -> None:
    """HTML tags in branding are escaped and limits are enforced."""
    raw_widget = WidgetBranding(
        title="<script>alert('xss')</script>Assistant",
        subtitle="<b>Bold</b> subtitle",
        logo_url="https://example.com/logo.png",
        primary_color="#0066FF",
        welcome_message="<img src=x onerror=alert(1)>Hello",
        disclaimer="Legal <i>disclaimer</i>",
        suggested_questions=[
            "<b>Question 1</b>",
            "Question 2",
        ],
    )
    sanitized = sanitize_widget_branding(raw_widget)
    assert sanitized.title is not None and "<script>" not in sanitized.title
    assert sanitized.title is not None and "&lt;script&gt;" in sanitized.title
    assert sanitized.subtitle is not None and "<b>" not in sanitized.subtitle
    assert sanitized.subtitle is not None and "&lt;b&gt;" in sanitized.subtitle
    assert sanitized.welcome_message is not None and "<img" not in sanitized.welcome_message
    assert sanitized.welcome_message is not None and "&lt;img" in sanitized.welcome_message
    assert sanitized.primary_color == "#0066FF"
    assert sanitized.logo_url == "https://example.com/logo.png"


def test_branding_validation_rejects_insecure_inputs() -> None:
    """Rejects http logo schemes, invalid hex colors, and non-image paths."""
    with pytest.raises(ValueError, match="https"):
        validate_logo_url("http://example.com/logo.png")

    with pytest.raises(ValueError, match="image file"):
        validate_logo_url("https://example.com/malicious.exe")

    with pytest.raises(ValueError, match="hex color"):
        validate_primary_color("red")

    with pytest.raises(ValueError, match="hex color"):
        validate_primary_color("#12345")


# --- 6. Widget Handshake & Session JWT ---
def test_widget_session_jwt_roundtrip_and_fail_closed_defaults() -> None:
    """Short-lived JWT roundtrip preserves tenant, user, and access_level."""
    secret = "test-secret-key-at-least-32-characters-long"
    tenant_id = uuid4()
    user_id = uuid4()

    token = create_session_jwt(
        tenant_id=tenant_id,
        user_id=user_id,
        auth_mode="anonymous",
        secret=secret,
        access_level=AccessLevel.PUBLIC,
    )

    claims = verify_session_jwt(token, secret)
    assert claims["sub"] == str(user_id)
    assert claims["tenant_id"] == str(tenant_id)
    assert claims["auth_mode"] == "anonymous"
    assert claims["access_level"] == int(AccessLevel.PUBLIC)

    user_ctx = build_widget_user_context(claims=claims)
    assert user_ctx.access_level == AccessLevel.PUBLIC
    assert user_ctx.channel == ChannelType.WEB_WIDGET


def test_widget_session_jwt_expiration_and_tampering() -> None:
    """Expired or tampered JWTs are strictly rejected."""
    secret = "test-secret-key-at-least-32-characters-long"
    tenant_id = uuid4()
    user_id = uuid4()

    token = create_session_jwt(
        tenant_id=tenant_id,
        user_id=user_id,
        auth_mode="anonymous",
        secret=secret,
        ttl_seconds=10,
        now=1000.0,
    )

    # Valid at now = 1005
    assert verify_session_jwt(token, secret, now=1005.0)

    # Expired at now = 1015
    with pytest.raises(SessionSecurityError, match="expired"):
        verify_session_jwt(token, secret, now=1015.0)

    # Tampered signature
    tampered = token[:-4] + "abcd"
    with pytest.raises(SessionSecurityError, match="signature"):
        verify_session_jwt(tampered, secret, now=1005.0)


# --- 7. Origin Allowlist Validation ---
def test_origin_allowlist_strict_matching_and_no_wildcards() -> None:
    """Origin check matches allowed domains strictly and ignores wildcards."""
    allowed = ["https://company.com", "https://kb.internal.ru", "https://*.wildcard.com"]

    assert validate_origin("https://company.com", allowed) is True
    assert validate_origin("https://company.com/", allowed) is True
    assert validate_origin("https://kb.internal.ru", allowed) is True

    # Disallowed domains
    assert validate_origin("https://evil.com", allowed) is False
    assert validate_origin("http://company.com", allowed) is False
    assert validate_origin(None, allowed) is False

    # Wildcards are ignored (core/channels.md §5.3)
    assert validate_origin("https://sub.wildcard.com", allowed) is False


# --- 8. data-api-url Mode-Dependent Behavior ---
def test_data_api_url_mode_dependent_behavior() -> None:
    """SaaS Cloud ignores data-api-url; Box applies it after Origin allowlist check."""
    allowed = ["https://client.ru"]

    # Cloud mode: data-api-url is IGNORED
    resolved_cloud = resolve_api_url_for_mode(
        deployment_mode=DeploymentMode.SAAS_CLOUD,
        configured_api_url="https://cloud.rag-domain.com",
        client_data_api_url="https://phishing-site.com",
        origin="https://client.ru",
        allowed_origins=allowed,
    )
    assert resolved_cloud == "https://cloud.rag-domain.com"

    # Box mode: data-api-url is APPLIED
    resolved_box = resolve_api_url_for_mode(
        deployment_mode=DeploymentMode.ON_PREMISE_BOX,
        configured_api_url="https://kb.client.ru",
        client_data_api_url="https://kb.client.ru:8002",
        origin="https://client.ru",
        allowed_origins=allowed,
    )
    assert resolved_box == "https://kb.client.ru:8002"

    # Box mode: disallowed origin is REJECTED (fail-closed)
    with pytest.raises(SessionSecurityError):
        resolve_api_url_for_mode(
            deployment_mode=DeploymentMode.ON_PREMISE_BOX,
            configured_api_url="https://kb.client.ru",
            client_data_api_url="https://kb.client.ru:8002",
            origin="https://unauthorized-origin.com",
            allowed_origins=allowed,
        )


# --- 9. Server-Side Rate Limiter ---
@pytest.mark.asyncio
async def test_server_side_rate_limiter() -> None:
    """Rate limiter enforces per-minute threshold and reports retry_after."""
    limiter = InMemoryRateLimiter(max_requests_per_minute=3)
    now = 1000.0

    # First 3 requests succeed
    await limiter.check_and_record("127.0.0.1:user1", now=now)
    await limiter.check_and_record("127.0.0.1:user1", now=now + 1)
    await limiter.check_and_record("127.0.0.1:user1", now=now + 2)

    # 4th request exceeds rate limit
    with pytest.raises(RateLimitExceeded) as exc_info:
        await limiter.check_and_record("127.0.0.1:user1", now=now + 3)
    assert exc_info.value.retry_after > 0

    # Other user key is independent
    await limiter.check_and_record("127.0.0.1:user2", now=now + 3)

    # Window advances after 60s
    await limiter.check_and_record("127.0.0.1:user1", now=now + 61)


# --- 10. Widget API ASGI App Endpoints ---
@pytest.mark.asyncio
async def test_widget_api_asgi_endpoints() -> None:
    """Exercises WidgetAPIApp endpoints over ASGI."""
    app = WidgetAPIApp()
    tenant = TenantContext(
        tenant_id=uuid4(),
        slug="test-tenant",
        name="Test Tenant",
        allowed_origins=["https://allowed.domain.com"],
    )
    app.register_tenant(tenant)

    # Helper for ASGI call
    async def call_asgi(
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        query_string: str = "",
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = [
            (k.lower().encode("latin1"), v.encode("latin1")) for k, v in (headers or {}).items()
        ]
        body_bytes = json.dumps(body).encode("utf-8") if body else b""

        scope = {
            "type": "http",
            "method": method,
            "path": path,
            "headers": req_headers,
            "query_string": query_string.encode("latin1"),
            "client": ("127.0.0.1", 12345),
        }

        status_code = 0
        resp_headers: dict[str, str] = {}
        body_chunks: list[bytes] = []

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body_bytes, "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            nonlocal status_code, resp_headers
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message.get("headers", []):
                    resp_headers[k.decode("latin1").lower()] = v.decode("latin1")
            elif message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))

        await app(scope, receive, send)
        return status_code, resp_headers, b"".join(body_chunks)

    # 1. GET /healthz
    status, _, data = await call_asgi("GET", "/healthz")
    assert status == 200
    assert json.loads(data)["status"] == "ok"

    # 2. POST /api/v1/widget/session (disallowed origin -> 403)
    status, _, data = await call_asgi(
        "POST",
        "/api/v1/widget/session",
        body={"slug": "test-tenant", "auth_mode": "anonymous"},
        headers={"origin": "https://evil.com"},
    )
    assert status == 403

    # 2b. POST /api/v1/widget/session (oidc auth_mode fails closed with 501 / 401)
    status, _, data = await call_asgi(
        "POST",
        "/api/v1/widget/session",
        body={"slug": "test-tenant", "auth_mode": "oidc", "id_token": "some-token"},
        headers={"origin": "https://allowed.domain.com"},
    )
    assert status == 501

    # 3. POST /api/v1/widget/session (allowed origin -> 200)
    status, _, data = await call_asgi(
        "POST",
        "/api/v1/widget/session",
        body={"slug": "test-tenant", "auth_mode": "anonymous"},
        headers={"origin": "https://allowed.domain.com"},
    )
    assert status == 200
    session_data = json.loads(data)
    token = session_data["session_token"]
    assert token is not None

    # 4. GET /api/v1/widget/config
    status, _, data = await call_asgi(
        "GET", "/api/v1/widget/config", query_string="slug=test-tenant"
    )
    assert status == 200
    cfg = json.loads(data)
    assert "branding" in cfg
    assert cfg["deployment_mode"] == "saas_cloud"
    assert cfg["capabilities"]["supports_streaming"] is True

    # 5. POST /api/v1/chat/stream (with session token -> 200 SSE)
    status, hdrs, stream_data = await call_asgi(
        "POST",
        "/api/v1/chat/stream",
        body={"query": "Hello RAG"},
        headers={"authorization": f"Bearer {token}"},
    )
    assert status == 200
    assert "text/event-stream" in hdrs["content-type"]
    assert b"event: token" in stream_data
    assert b"event: done" in stream_data
    assert b'"confidence_passed"' in stream_data


# --- 10b. Fail-closed tenant resolution ---
@pytest.mark.asyncio
async def test_widget_app_fails_closed_without_configured_tenants() -> None:
    """A widget app with no registry/resolver serves no tenant (fail-closed)."""
    app = WidgetAPIApp()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/widget/session",
        "headers": [(b"origin", b"https://allowed.domain.com")],
        "client": ("127.0.0.1", 12345),
    }
    body = json.dumps({"slug": "unknown-tenant", "auth_mode": "anonymous"}).encode()

    status_code = 0

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status_code
        if message["type"] == "http.response.start":
            status_code = message["status"]

    await app(scope, receive, send)
    assert status_code == 404


# --- 11. Telegram Bot Runner ---
@pytest.mark.asyncio
async def test_telegram_bot_runner_commands_and_feedback() -> None:
    """TelegramBotRunner handles /start, /help, /clear, messages and callback feedback."""
    runner = TelegramBotRunner()

    # /start
    welcome = await runner.handle_message(12345, "/start")
    assert "corporate RAG assistant" in welcome or "Welcome" in welcome

    # /help
    help_text = await runner.handle_message(12345, "/help")
    assert "/start" in help_text
    assert "/help" in help_text

    # /clear
    clear_text = await runner.handle_message(12345, "/clear")
    assert "cleared" in clear_text.lower()

    # Regular message
    msg_res = await runner.handle_message(12345, "How to request a vacation?")
    assert msg_res is not None

    # Callback feedback 👍
    fb_pos = await runner.handle_callback_query(12345, "feedback:thumb_up:trace_123")
    assert "👍" in fb_pos

    # Callback feedback 👎
    fb_neg = await runner.handle_callback_query(12345, "feedback:thumb_down:trace_123")
    assert "👎" in fb_neg


# --- 12. DI Container Wiring ---
def test_di_container_wires_channel_ports() -> None:
    """DI container allows binding BaseChannel to WebChannel or other channel adapters."""
    container = Container()
    container.register(BaseChannel, lambda: WebChannel())

    channel = container.resolve(BaseChannel)
    assert isinstance(channel, WebChannel)
    assert channel.capabilities.supports_streaming is True


# --- 13. CLI Channel and Console Runner ---
@pytest.mark.asyncio
async def test_cli_runner_execution() -> None:
    """Verifies that CLIRunner processes commands and queries."""
    from services.cli import CLIRunner

    output_lines: list[str] = []
    runner = CLIRunner()
    runner.channel = CLIChannel(output_sink=lambda text: output_lines.append(text))

    await runner.execute_query("/help")
    await runner.execute_query("What is the company policy?")
    assert len(output_lines) > 0


# --- 14. Session JWT Hardening & Alg Check ---
def test_session_jwt_algorithm_and_iat_validation() -> None:
    """Session JWT validator rejects alg other than HS256, future tokens and tamper."""
    import base64
    import time

    secret = "test-secret-at-least-32-chars-long-123456"
    valid_token = create_session_jwt(
        tenant_id=uuid4(),
        user_id=uuid4(),
        auth_mode="anonymous",
        secret=secret,
        ttl_seconds=3600,
    )
    claims = verify_session_jwt(valid_token, secret)
    assert claims["sub"] is not None

    # Invalid algorithm "none"
    header_none = (
        base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=").decode("ascii")
    )
    payload = valid_token.split(".")[1]
    sig = valid_token.split(".")[2]
    token_none_alg = f"{header_none}.{payload}.{sig}"
    with pytest.raises(SessionSecurityError, match="Unsupported or invalid token algorithm"):
        verify_session_jwt(token_none_alg, secret)

    # Future token beyond clock skew
    future_now = time.time() + 300
    future_token = create_session_jwt(
        tenant_id=uuid4(),
        user_id=uuid4(),
        auth_mode="anonymous",
        secret=secret,
        ttl_seconds=3600,
        now=future_now,
    )
    with pytest.raises(SessionSecurityError, match="Session token issued in the future"):
        verify_session_jwt(future_token, secret)
