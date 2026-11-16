"""Web widget channel adapter (`core/channels.md` §5).

Implements BaseChannel for ChannelType.WEB_WIDGET (Shadow DOM embed).
Supports streaming, markdown, citations, feedback buttons, callback payloads, cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from rag_core.channels.capabilities import get_channel_capabilities
from rag_core.contracts.enums import ChannelEventType, ChannelType
from rag_core.contracts.interfaces import BaseChannel
from rag_core.contracts.models import (
    ChannelCapabilities,
    ChannelEvent,
    GenerationCitation,
    RAGResponse,
)


class WebChannel(BaseChannel):
    """Channel adapter for Web Widget (Shadow DOM embed)."""

    def __init__(self) -> None:
        self._capabilities = get_channel_capabilities(ChannelType.WEB_WIDGET)
        self._inbox: asyncio.Queue[ChannelEvent] = asyncio.Queue()
        self._outbox: dict[str, list[RAGResponse]] = {}
        self._cancelled_users: set[str] = set()

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def start(self) -> None:
        """Starts the web channel handler."""
        return None

    async def enqueue_event(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        text: str | None = None,
        event_type: ChannelEventType = ChannelEventType.TEXT_MESSAGE,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ChannelEvent:
        """Normalizes and enqueues an incoming web widget event."""
        event = ChannelEvent(
            event_type=event_type,
            user_id=user_id,
            tenant_id=tenant_id,
            channel=ChannelType.WEB_WIDGET,
            channel_user_id=str(user_id),
            session_id=session_id,
            text=text,
            payload=payload or {},
            attachments=[],
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

    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Sends composed RAGResponse to user outbox."""
        if user_id not in self._outbox:
            self._outbox[user_id] = []
        self._outbox[user_id].append(response)

    async def stream_response(
        self,
        user_id: str,
        token_stream: AsyncGenerator[str, None],
        initial_message_id: str | None = None,
        citations: list[GenerationCitation] | None = None,
        confidence_passed: bool = True,
        trace_id: str | None = None,
    ) -> RAGResponse:
        """Streams tokens and accumulates full answer for Web Widget."""
        chunks: list[str] = []
        async for token in token_stream:
            if user_id in self._cancelled_users:
                self._cancelled_users.remove(user_id)
                break
            chunks.append(token)

        full_answer = "".join(chunks)
        response = RAGResponse(
            answer=full_answer,
            citations=citations or [],
            confidence_passed=confidence_passed,
            trace_id=trace_id,
        )
        await self.send_response(user_id, response)
        return response

    async def request_cancellation(self, user_id: str) -> None:
        """Cancels active stream for the user."""
        self._cancelled_users.add(user_id)

    def get_sent_responses(self, user_id: str) -> list[RAGResponse]:
        """Retrieves sent responses for inspection/testing."""
        return list(self._outbox.get(user_id, []))
