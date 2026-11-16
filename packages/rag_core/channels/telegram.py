"""Telegram channel adapter (`core/channels.md` §6).

Implements BaseChannel for ChannelType.TELEGRAM (aiogram 3.x white-label integration).
Supports streaming with throttling (0.8-1.0s), Markdown formatting, citations footnotes,
inline feedback buttons (👍/👎), 4096-char message splitting, commands and attachments.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rag_core.channels.capabilities import get_channel_capabilities
from rag_core.contracts.enums import ChannelEventType, ChannelType
from rag_core.contracts.interfaces import BaseChannel
from rag_core.contracts.models import (
    AttachmentRef,
    ChannelCapabilities,
    ChannelEvent,
    GenerationCitation,
    RAGResponse,
)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
DEFAULT_STREAM_THROTTLE_SECONDS = 0.85


def split_telegram_message(text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH) -> list[str]:
    """Splits a long message into safe chunks <= max_length without breaking mid-word."""
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to split at a double newline, newline, or space
        split_pos = remaining.rfind("\n\n", 0, max_length)
        if split_pos == -1:
            split_pos = remaining.rfind("\n", 0, max_length)
        if split_pos == -1:
            split_pos = remaining.rfind(" ", 0, max_length)
        if split_pos == -1:
            split_pos = max_length

        chunk = remaining[:split_pos].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_pos:].lstrip()

    return chunks


def format_citations_markdown(citations: list[GenerationCitation]) -> str:
    """Formats generation citations as clean Markdown footnotes."""
    if not citations:
        return ""
    lines = ["\n\n**Sources:**"]
    for c in sorted(citations, key=lambda x: x.citation_index):
        page_str = f", p. {c.page}" if c.page is not None else ""
        lines.append(f"[{c.citation_index}] {c.document_title}{page_str}")
    return "\n".join(lines)


class TelegramChannel(BaseChannel):
    """Channel adapter for Telegram Bot."""

    def __init__(
        self,
        throttle_seconds: float = DEFAULT_STREAM_THROTTLE_SECONDS,
        send_message_fn: Callable[[str, str, dict[str, Any] | None], Any] | None = None,
        edit_message_fn: Callable[[str, str, str, dict[str, Any] | None], Any] | None = None,
    ) -> None:
        self._capabilities = get_channel_capabilities(ChannelType.TELEGRAM)
        self._throttle_seconds = throttle_seconds
        self._send_fn = send_message_fn
        self._edit_fn = edit_message_fn
        self._inbox: asyncio.Queue[ChannelEvent] = asyncio.Queue()
        self._outbox: dict[str, list[RAGResponse]] = {}
        self._edit_history: list[tuple[str, str, float]] = []  # (user_id, text, timestamp)

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def start(self) -> None:
        """Starts the telegram channel handler."""
        return None

    async def normalize_native_event(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        channel_user_id: str,
        text: str | None = None,
        event_type: ChannelEventType = ChannelEventType.TEXT_MESSAGE,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
        attachments: list[AttachmentRef] | None = None,
    ) -> ChannelEvent:
        """Normalizes and enqueues an incoming Telegram message or callback query."""
        event = ChannelEvent(
            event_type=event_type,
            user_id=user_id,
            tenant_id=tenant_id,
            channel=ChannelType.TELEGRAM,
            channel_user_id=channel_user_id,
            session_id=session_id,
            text=text,
            payload=payload or {},
            attachments=attachments or [],
            created_at=datetime.now(UTC),
        )
        await self._inbox.put(event)
        return event

    async def receive(self) -> AsyncGenerator[ChannelEvent, None]:
        """Yields normalized incoming channel events."""
        while True:
            event = await self._inbox.get()
            yield event
            self._inbox.task_done()

    def format_response_payload(self, response: RAGResponse) -> tuple[str, dict[str, Any]]:
        """Formats the full text and inline keyboard markup for a Telegram response."""
        formatted_text = response.answer
        if self._capabilities.supports_citations and response.citations:
            formatted_text += format_citations_markdown(response.citations)

        reply_markup: dict[str, Any] = {}
        if self._capabilities.supports_feedback_buttons and response.trace_id:
            reply_markup = {
                "inline_keyboard": [
                    [
                        {
                            "text": "👍",
                            "callback_data": f"feedback:thumb_up:{response.trace_id}",
                        },
                        {
                            "text": "👎",
                            "callback_data": f"feedback:thumb_down:{response.trace_id}",
                        },
                    ]
                ]
            }

        return formatted_text, reply_markup

    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Sends composed RAGResponse with message splitting and inline feedback buttons."""
        if user_id not in self._outbox:
            self._outbox[user_id] = []
        self._outbox[user_id].append(response)

        formatted_text, reply_markup = self.format_response_payload(response)
        chunks = split_telegram_message(formatted_text)

        if self._send_fn is not None:
            for idx, chunk in enumerate(chunks):
                is_last = idx == len(chunks) - 1
                markup = reply_markup if is_last else None
                res = self._send_fn(user_id, chunk, markup)
                if asyncio.iscoroutine(res):
                    await res

    async def stream_response(
        self,
        user_id: str,
        token_stream: AsyncGenerator[str, None],
        initial_message_id: str | None = None,
        citations: list[GenerationCitation] | None = None,
        confidence_passed: bool = True,
        trace_id: str | None = None,
    ) -> RAGResponse:
        """Streams tokens with throttling (0.8-1.0s) between Telegram edit calls."""
        chunks: list[str] = []
        last_edit_time = time.time()
        last_rendered_text = ""

        msg_id = initial_message_id or "msg_0"

        async for token in token_stream:
            chunks.append(token)
            current_text = "".join(chunks)
            now = time.time()

            if (
                now - last_edit_time
            ) >= self._throttle_seconds and current_text != last_rendered_text:
                last_rendered_text = current_text
                last_edit_time = now
                self._edit_history.append((user_id, current_text, now))
                if self._edit_fn is not None:
                    res = self._edit_fn(
                        user_id, msg_id, current_text[:TELEGRAM_MAX_MESSAGE_LENGTH], None
                    )
                    if asyncio.iscoroutine(res):
                        await res

        full_answer = "".join(chunks)
        response = RAGResponse(
            answer=full_answer,
            citations=citations or [],
            confidence_passed=confidence_passed,
            trace_id=trace_id,
        )

        # Final edit with complete message and inline feedback buttons
        now = time.time()
        self._edit_history.append((user_id, full_answer, now))
        formatted_text, reply_markup = self.format_response_payload(response)

        if self._edit_fn is not None:
            res = self._edit_fn(
                user_id,
                msg_id,
                formatted_text[:TELEGRAM_MAX_MESSAGE_LENGTH],
                reply_markup or None,
            )
            if asyncio.iscoroutine(res):
                await res

        await self.send_response(user_id, response)
        return response

    def get_edit_history(self) -> list[tuple[str, str, float]]:
        """Returns edit timestamps and text chunks for throttling verification."""
        return list(self._edit_history)
