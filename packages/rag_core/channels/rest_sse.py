"""Headless REST/SSE channel adapter (`core/channels.md` §2, §3).

Implements BaseChannel for ChannelType.REST_SSE (custom integrations via /api/v1/chat/stream).
Outputs structured SSE events (token, citations, done, error) without markdown decoration.
"""

from __future__ import annotations

import asyncio
import json
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


def format_sse_event(event_name: str, data: dict[str, Any]) -> str:
    """Formats a Server-Sent Event frame."""
    payload_json = json.dumps(data, ensure_ascii=False)
    return f"event: {event_name}\ndata: {payload_json}\n\n"


class RestSSEChannel(BaseChannel):
    """Channel adapter for headless REST / SSE endpoint."""

    def __init__(self) -> None:
        self._capabilities = get_channel_capabilities(ChannelType.REST_SSE)
        self._inbox: asyncio.Queue[ChannelEvent] = asyncio.Queue()
        self._outbox: dict[str, list[RAGResponse]] = {}
        self._cancelled_users: set[str] = set()

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def start(self) -> None:
        """Starts the REST/SSE channel handler."""
        return None

    async def normalize_request(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        text: str,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ChannelEvent:
        """Normalizes an incoming REST/SSE request into a ChannelEvent."""
        event = ChannelEvent(
            event_type=ChannelEventType.TEXT_MESSAGE,
            user_id=user_id,
            tenant_id=tenant_id,
            channel=ChannelType.REST_SSE,
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
        """Stores composed RAGResponse in outbox."""
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
        """Accumulates response from token stream for REST/SSE."""
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

    async def format_stream_events(
        self,
        user_id: str,
        token_stream: AsyncGenerator[str, None],
        citations_fn: Any | None = None,
        trace_id: str | None = None,
        confidence_passed: bool = True,
    ) -> AsyncGenerator[str, None]:
        """Yields raw SSE frames for HTTP streaming."""
        chunks: list[str] = []
        try:
            async for token in token_stream:
                if user_id in self._cancelled_users:
                    self._cancelled_users.remove(user_id)
                    yield format_sse_event("cancelled", {"user_id": user_id})
                    return
                chunks.append(token)
                yield format_sse_event("token", {"token": token})

            full_answer = "".join(chunks)
            citations: list[GenerationCitation] = []
            if citations_fn is not None:
                if asyncio.iscoroutinefunction(citations_fn):
                    citations = await citations_fn(full_answer)
                else:
                    citations = citations_fn(full_answer)

            if citations:
                yield format_sse_event(
                    "citations",
                    {
                        "citations": [
                            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
                            for c in citations
                        ]
                    },
                )

            yield format_sse_event(
                "done",
                {
                    "trace_id": trace_id,
                    "confidence_passed": confidence_passed,
                },
            )
        except Exception as exc:
            yield format_sse_event("error", {"error": str(exc)})

    async def request_cancellation(self, user_id: str) -> None:
        """Sets cancellation flag for the streaming user."""
        self._cancelled_users.add(user_id)
