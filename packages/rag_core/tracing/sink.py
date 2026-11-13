"""Tracing sink implementations (`core/eval_and_ops.md` §2, Stage F).

Provides:
- `NoOpTraceSink` - zero-overhead sink that discards all trace events;
- `InMemoryTraceSink` - records traces in memory for unit testing;
- `TraceManager` - helper for emitting timed spans and tracking errors.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from rag_core.contracts.interfaces import BaseTraceSink
from rag_core.contracts.models import TraceContext


class NoOpTraceSink(BaseTraceSink):
    """Zero-overhead no-op trace sink."""

    async def start_trace(self, ctx: TraceContext, name: str, input: dict[str, Any]) -> str:
        return str(uuid.uuid4())

    async def log_span(
        self,
        trace_id: str,
        name: str,
        *,
        latency_ms: float,
        input: dict[str, Any],
        output: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        return None

    async def log_score(self, trace_id: str, name: str, value: float) -> None:
        return None

    async def flush(self) -> None:
        return None


@dataclass(slots=True)
class RecordedSpan:
    """A span captured in-memory."""

    trace_id: str
    name: str
    latency_ms: float
    input: dict[str, Any]
    output: dict[str, Any]
    metadata: dict[str, Any]


@dataclass(slots=True)
class RecordedTrace:
    """A trace captured in-memory."""

    trace_id: str
    ctx: TraceContext
    name: str
    input: dict[str, Any]
    spans: list[RecordedSpan] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


class InMemoryTraceSink(BaseTraceSink):
    """In-memory trace sink for testing and inspection."""

    def __init__(self) -> None:
        self.traces: dict[str, RecordedTrace] = {}
        self.spans: list[RecordedSpan] = []

    async def start_trace(self, ctx: TraceContext, name: str, input: dict[str, Any]) -> str:
        trace_id = str(uuid.uuid4())
        self.traces[trace_id] = RecordedTrace(
            trace_id=trace_id,
            ctx=ctx,
            name=name,
            input=input,
        )
        return trace_id

    async def log_span(
        self,
        trace_id: str,
        name: str,
        *,
        latency_ms: float,
        input: dict[str, Any],
        output: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        span = RecordedSpan(
            trace_id=trace_id,
            name=name,
            latency_ms=latency_ms,
            input=input,
            output=output,
            metadata=metadata,
        )
        self.spans.append(span)
        if trace_id in self.traces:
            self.traces[trace_id].spans.append(span)

    async def log_score(self, trace_id: str, name: str, value: float) -> None:
        if trace_id in self.traces:
            self.traces[trace_id].scores[name] = value

    async def flush(self) -> None:
        return None


class TraceManager:
    """Helper for managing trace lifecycle and recording timed spans."""

    def __init__(self, sink: BaseTraceSink) -> None:
        self._sink = sink

    @property
    def sink(self) -> BaseTraceSink:
        return self._sink

    async def start_trace(self, ctx: TraceContext, name: str, input: dict[str, Any]) -> str:
        try:
            return await self._sink.start_trace(ctx, name, input)
        except Exception:  # noqa: BLE001
            # Fire-and-forget: sink failures never break application flow
            return str(uuid.uuid4())

    @asynccontextmanager
    async def span(
        self,
        trace_id: str,
        name: str,
        *,
        input: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Context manager measuring execution time and logging the span on exit."""
        span_input = input or {}
        span_meta = metadata or {}
        span_output: dict[str, Any] = {}
        t0 = time.perf_counter()
        try:
            yield span_output
        except Exception as exc:
            span_meta["error"] = str(exc)
            span_meta["error_type"] = type(exc).__name__
            raise
        finally:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            with contextlib.suppress(Exception):
                await self._sink.log_span(
                    trace_id,
                    name,
                    latency_ms=latency_ms,
                    input=span_input,
                    output=span_output,
                    metadata=span_meta,
                )
