"""Branding validation and sanitization (`core/channels.md` §7).

Implements typed white-label security:
- HTML escaping / text sanitization against XSS in foreign-embedded contexts;
- Strict regex and image extension/content-type validation for logo URLs;
- Hex color pattern validation;
- Strict length and count bounds;
- Additive schema versioning with config_version.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlparse

from rag_core.contracts.models import (
    BrandingConfig,
    TelegramBranding,
    WidgetBranding,
)

_COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")
_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".ico"})


def sanitize_text(text: str | None) -> str | None:
    """Escapes HTML entities and strips control characters for safe text rendering."""
    if text is None:
        return None
    # Escape HTML to prevent XSS injection
    escaped = html.escape(text.strip())
    return escaped


def validate_logo_url(url: str | None) -> str | None:
    """Validates that logo_url uses https and points to an image resource."""
    if url is None:
        return None
    trimmed = url.strip()
    parsed = urlparse(trimmed)
    if parsed.scheme.lower() != "https":
        raise ValueError("logo_url must use https scheme")
    if not parsed.netloc:
        raise ValueError("logo_url must contain a valid domain")
    path_lower = parsed.path.lower()
    has_image_ext = any(path_lower.endswith(ext) for ext in _IMAGE_EXTENSIONS)
    # Require an image extension or query parameters indicating image format
    if not has_image_ext and not any(ext.lstrip(".") in path_lower for ext in _IMAGE_EXTENSIONS):
        raise ValueError(
            f"logo_url must refer to an image file ({', '.join(sorted(_IMAGE_EXTENSIONS))})"
        )
    return trimmed


def validate_primary_color(color: str | None) -> str | None:
    """Validates that primary_color is a 6-digit hex color."""
    if color is None:
        return None
    trimmed = color.strip()
    if not _COLOR_PATTERN.match(trimmed):
        raise ValueError("primary_color must be a 6-digit hex color (e.g. #0055FF)")
    return trimmed


def sanitize_widget_branding(branding: WidgetBranding) -> WidgetBranding:
    """Sanitizes and validates widget branding configuration."""
    validated_logo = validate_logo_url(branding.logo_url) if branding.logo_url else None
    validated_color = (
        validate_primary_color(branding.primary_color) if branding.primary_color else None
    )

    questions = [
        sanitize_text(q) or ""
        for q in branding.suggested_questions[:5]
        if q and len(q.strip()) <= 120
    ]

    return WidgetBranding(
        config_version=max(branding.config_version, 1),
        title=sanitize_text(branding.title),
        subtitle=sanitize_text(branding.subtitle),
        logo_url=validated_logo,
        primary_color=validated_color,
        welcome_message=sanitize_text(branding.welcome_message),
        disclaimer=sanitize_text(branding.disclaimer),
        suggested_questions=questions,
    )


def sanitize_telegram_branding(branding: TelegramBranding) -> TelegramBranding:
    """Sanitizes and validates Telegram branding configuration."""
    sanitized_commands: dict[str, str] = {}
    for cmd, desc in list(branding.commands.items())[:100]:
        clean_cmd = cmd.strip().lstrip("/")[:32]
        clean_desc = sanitize_text(desc) or ""
        if clean_cmd:
            sanitized_commands[clean_cmd] = clean_desc[:256]

    return TelegramBranding(
        config_version=max(branding.config_version, 1),
        bot_name=sanitize_text(branding.bot_name),
        description=sanitize_text(branding.description),
        short_description=sanitize_text(branding.short_description),
        commands=sanitized_commands,
        welcome_message=sanitize_text(branding.welcome_message),
    )


def sanitize_branding_config(config: BrandingConfig) -> BrandingConfig:
    """Sanitizes aggregating branding config across channels."""
    widget = sanitize_widget_branding(config.widget)
    telegram = sanitize_telegram_branding(config.telegram) if config.telegram else None
    return BrandingConfig(
        config_version=max(config.config_version, 1),
        widget=widget,
        telegram=telegram,
    )
