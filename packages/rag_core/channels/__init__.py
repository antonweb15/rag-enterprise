"""Delivery channels package (`core/channels.md`).

Exports:
- Channel capabilities registry: CANONICAL_CHANNEL_CAPABILITIES, get_channel_capabilities;
- Typed branding validation: sanitize_branding_config, sanitize_widget_branding,
  sanitize_telegram_branding;
- Public rate limiter: InMemoryRateLimiter, RateLimitExceeded;
- Session security & JWT helpers: create_session_jwt, verify_session_jwt, validate_origin,
  resolve_api_url_for_mode, build_widget_user_context;
- Channel adapters: WebChannel, TelegramChannel, RestSSEChannel, CLIChannel.
"""

from __future__ import annotations

from rag_core.channels.branding import (
    sanitize_branding_config,
    sanitize_telegram_branding,
    sanitize_text,
    sanitize_widget_branding,
    validate_logo_url,
    validate_primary_color,
)
from rag_core.channels.capabilities import (
    CANONICAL_CHANNEL_CAPABILITIES,
    get_channel_capabilities,
)
from rag_core.channels.cli import CLIChannel
from rag_core.channels.rate_limit import InMemoryRateLimiter, RateLimitExceeded
from rag_core.channels.rest_sse import RestSSEChannel, format_sse_event
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

__all__ = [
    "CANONICAL_CHANNEL_CAPABILITIES",
    "get_channel_capabilities",
    "sanitize_branding_config",
    "sanitize_widget_branding",
    "sanitize_telegram_branding",
    "sanitize_text",
    "validate_logo_url",
    "validate_primary_color",
    "InMemoryRateLimiter",
    "RateLimitExceeded",
    "SessionSecurityError",
    "create_session_jwt",
    "verify_session_jwt",
    "validate_origin",
    "resolve_api_url_for_mode",
    "build_widget_user_context",
    "WebChannel",
    "TelegramChannel",
    "format_citations_markdown",
    "split_telegram_message",
    "RestSSEChannel",
    "format_sse_event",
    "CLIChannel",
]
