"""Canonical channel capabilities (`core/channels.md` §3).

Defines the capability flags per delivery channel (Web Widget, Telegram, REST/SSE, CLI).
Key invariant: capability flags are read ONLY at the channel boundary (rendering/delivery);
the search business logic (retrieval/generation) does not branch on capabilities.
"""

from __future__ import annotations

from rag_core.contracts.enums import ChannelType
from rag_core.contracts.models import ChannelCapabilities

CANONICAL_CHANNEL_CAPABILITIES: dict[ChannelType, ChannelCapabilities] = {
    ChannelType.WEB_WIDGET: ChannelCapabilities(
        supports_streaming=True,
        supports_markdown=True,
        supports_citations=True,
        supports_feedback_buttons=True,
        supports_attachments=False,
        supports_reply_threading=False,
        supports_commands=False,
        supports_callback_payloads=True,
        supports_cancellation=True,
        max_message_length=None,
        max_attachments_per_message=0,
    ),
    ChannelType.TELEGRAM: ChannelCapabilities(
        supports_streaming=True,
        supports_markdown=True,
        supports_citations=True,
        supports_feedback_buttons=True,
        supports_attachments=True,
        supports_reply_threading=True,
        supports_commands=True,
        supports_callback_payloads=True,
        supports_cancellation=False,
        max_message_length=4096,
        max_attachments_per_message=5,
    ),
    ChannelType.REST_SSE: ChannelCapabilities(
        supports_streaming=True,
        supports_markdown=False,
        supports_citations=True,
        supports_feedback_buttons=False,
        supports_attachments=False,
        supports_reply_threading=False,
        supports_commands=False,
        supports_callback_payloads=False,
        supports_cancellation=True,
        max_message_length=None,
        max_attachments_per_message=0,
    ),
    ChannelType.CLI: ChannelCapabilities(
        supports_streaming=True,
        supports_markdown=False,
        supports_citations=True,
        supports_feedback_buttons=False,
        supports_attachments=False,
        supports_reply_threading=False,
        supports_commands=True,
        supports_callback_payloads=False,
        supports_cancellation=True,
        max_message_length=None,
        max_attachments_per_message=0,
    ),
}


def get_channel_capabilities(channel: ChannelType) -> ChannelCapabilities:
    """Returns canonical capabilities for the specified channel type."""
    caps = CANONICAL_CHANNEL_CAPABILITIES.get(channel)
    if caps is None:
        return ChannelCapabilities()
    return caps.model_copy()
