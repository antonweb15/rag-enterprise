"""Console CLI channel adapter (`core/channels.md` §2, §3).

Implements BaseChannel for ChannelType.CLI (air-gapped Box typer console).
Supports streaming token output to stdout/sink, plain citations, commands, and cancellation.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
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


class CLIChannel(BaseChannel):
    """Channel adapter for CLI (Air-Gapped Box console)."""

    def __init__(
        self,
        output_sink: Callable[[str], Any] | None = None,
    ) -> None:
        self._capabilities = get_channel_capabilities(ChannelType.CLI)
        self._sink = output_sink
        self._inbox: asyncio.Queue[ChannelEvent] = asyncio.Queue()
        self._outbox: dict[str, list[RAGResponse]] = {}
        self._cancelled_users: set[str] = set()

    @property
    def capabilities(self) -> ChannelCapabilities:
        return self._capabilities

    async def start(self) -> None:
        """Starts the CLI channel handler."""
        return None

    async def normalize_command(
        self,
        *,
        tenant_id: UUID,
        user_id: UUID,
        command: str,
        session_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> ChannelEvent:
        """Normalizes a CLI input/command into a ChannelEvent."""
        event_type = (
            ChannelEventType.COMMAND if command.startswith("/") else ChannelEventType.TEXT_MESSAGE
        )
        event = ChannelEvent(
            event_type=event_type,
            user_id=user_id,
            tenant_id=tenant_id,
            channel=ChannelType.CLI,
            channel_user_id=str(user_id),
            session_id=session_id,
            text=command,
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

    def format_cli_response(self, response: RAGResponse) -> str:
        """Formats plain text response and citations for CLI console."""
        text = response.answer
        if self._capabilities.supports_citations and response.citations:
            text += "\n\nSources:\n"
            for c in sorted(response.citations, key=lambda x: x.citation_index):
                page_str = f", page {c.page}" if c.page is not None else ""
                text += f"[{c.citation_index}] {c.document_title} ({c.source_uri}{page_str})\n"
        return text

    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Sends composed RAGResponse to console sink."""
        if user_id not in self._outbox:
            self._outbox[user_id] = []
        self._outbox[user_id].append(response)

        formatted = self.format_cli_response(response)
        if self._sink is not None:
            res = self._sink(formatted)
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
        """Streams tokens directly to console sink as they arrive."""
        chunks: list[str] = []
        async for token in token_stream:
            if user_id in self._cancelled_users:
                self._cancelled_users.remove(user_id)
                if self._sink is not None:
                    res = self._sink("\n[Generation cancelled]\n")
                    if asyncio.iscoroutine(res):
                        await res
                break
            chunks.append(token)
            if self._sink is not None:
                res = self._sink(token)
                if asyncio.iscoroutine(res):
                    await res

        full_answer = "".join(chunks)
        response = RAGResponse(
            answer=full_answer,
            citations=citations or [],
            confidence_passed=confidence_passed,
            trace_id=trace_id,
        )
        if self._sink is not None and self._capabilities.supports_citations and citations:
            citations_text = self.format_cli_response(response)[len(full_answer) :]
            if citations_text:
                res = self._sink(citations_text)
                if asyncio.iscoroutine(res):
                    await res

        await self.send_response(user_id, response)
        return response

    async def request_cancellation(self, user_id: str) -> None:
        """Sets cancellation flag for CLI user."""
        self._cancelled_users.add(user_id)
