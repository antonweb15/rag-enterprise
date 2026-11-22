"""Tracing subpackage (`core/eval_and_ops.md` §2, `IMPLEMENTATION_PLAN.md` Stage H)."""

from __future__ import annotations

from rag_core.tracing.langfuse import HttpTransportFn, LangfuseTraceSink
from rag_core.tracing.masking import DEFAULT_MASK_FIELDS, mask_pii, mask_pii_string
from rag_core.tracing.sink import (
    InMemoryTraceSink,
    NoOpTraceSink,
    RecordedScore,
    RecordedSpan,
    RecordedTrace,
    TraceManager,
)

__all__ = [
    "DEFAULT_MASK_FIELDS",
    "HttpTransportFn",
    "InMemoryTraceSink",
    "LangfuseTraceSink",
    "NoOpTraceSink",
    "RecordedScore",
    "RecordedSpan",
    "RecordedTrace",
    "TraceManager",
    "mask_pii",
    "mask_pii_string",
]
