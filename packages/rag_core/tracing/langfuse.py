"""Langfuse Trace Sink implementation (`core/eval_and_ops.md` §2, `IMPLEMENTATION_PLAN.md` Stage H).

Provides:
- `LangfuseTraceSink`: Concrete implementation of `BaseTraceSink` sending trace spans,
  tokens, latencies, metadata and user feedback scores to a self-hosted or cloud Langfuse instance.
- Fire-and-forget delivery: Ingestion errors / Langfuse outages never block or fail RAG responses.
- Automatic PII masking & stripping of raw document/chunk texts.
- Per-environment sampling and retention metadata.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import random
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from rag_core.contracts.enums import DeploymentMode
from rag_core.contracts.interfaces import BaseTraceSink
from rag_core.contracts.models import TraceContext
from rag_core.contracts.settings import Settings
from rag_core.tracing.masking import mask_pii

logger = logging.getLogger(__name__)

# Transport signature for testing/mocking:
# (url, headers, json_bytes, timeout) -> (status_code, response_bytes)
HttpTransportFn = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


def _default_http_post(
    url: str,
    headers: dict[str, str],
    payload: bytes,
    timeout: float = 3.0,
) -> tuple[int, bytes]:
    """Synchronous HTTP POST executed in thread pool."""
    req = urllib.request.Request(
        url=url,
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


class LangfuseTraceSink(BaseTraceSink):
    """Production Langfuse trace sink adhering to the Stage-H observability contract."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        host: str | None = None,
        public_key: str | None = None,
        secret_key: str | None = None,
        project_id: str | None = None,
        sample_rate: float | None = None,
        retention_days: int | None = None,
        mask_fields: Sequence[str] | None = None,
        deployment_mode: DeploymentMode | None = None,
        http_transport: HttpTransportFn | None = None,
        buffer_size_limit: int = 5000,
        request_timeout: float = 3.0,
        auto_flush: bool = True,
    ) -> None:
        cfg = settings or Settings()
        raw_host = host or cfg.langfuse_host or "https://cloud.langfuse.com"
        self.host = raw_host.rstrip("/")
        self.public_key = public_key if public_key is not None else cfg.langfuse_public_key
        self.secret_key = secret_key if secret_key is not None else cfg.langfuse_secret_key
        self.project_id = project_id if project_id is not None else cfg.langfuse_project_id
        self.sample_rate = sample_rate if sample_rate is not None else cfg.trace_sample_rate
        self.retention_days = (
            retention_days if retention_days is not None else cfg.trace_retention_days
        )
        self.mask_fields = (
            list(mask_fields) if mask_fields is not None else list(cfg.trace_mask_fields)
        )
        self.deployment_mode = (
            deployment_mode if deployment_mode is not None else cfg.deployment_mode
        )
        self._http_transport = http_transport or _default_http_post
        self._buffer_size_limit = buffer_size_limit
        self._request_timeout = request_timeout
        self._auto_flush = auto_flush

        # Unsampled trace ids are only meaningful for the in-flight request (a trace lives
        # milliseconds); the set is bounded so sampling never leaks memory over time.
        self._unsampled_trace_cap = 10_000
        self._unsampled_traces: set[str] = set()
        self._events_buffer: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()

    @property
    def is_configured(self) -> bool:
        """Returns True when both Langfuse public and secret keys are present."""
        return bool(self.public_key and self.secret_key)

    async def start_trace(self, ctx: TraceContext, name: str, input: dict[str, Any]) -> str:
        """Starts a trace with mandatory TraceContext attributes (tenant_id, env, channel, etc.)."""
        trace_id = str(uuid4())

        if not self.is_configured:
            return trace_id

        # Evaluate sampling rate
        if self.sample_rate < 1.0 and random.random() >= self.sample_rate:
            if len(self._unsampled_traces) >= self._unsampled_trace_cap:
                self._unsampled_traces.clear()
            self._unsampled_traces.add(trace_id)
            return trace_id

        env_val = (
            ctx.environment.value if hasattr(ctx.environment, "value") else str(ctx.environment)
        )
        chan_val = ctx.channel.value if hasattr(ctx.channel, "value") else str(ctx.channel)
        level_val = (
            int(ctx.access_level.value)
            if hasattr(ctx.access_level, "value")
            else int(ctx.access_level)
        )

        metadata: dict[str, Any] = {
            "tenant_id": str(ctx.tenant_id),
            "environment": env_val,
            "channel": chan_val,
            "access_level": level_val,
            "trace_retention_days": self.retention_days,
        }
        if ctx.deployment_id:
            metadata["deployment_id"] = str(ctx.deployment_id)
        if ctx.license_id:
            metadata["license_id"] = str(ctx.license_id)
        if self.project_id:
            metadata["project_id"] = self.project_id

        trace_body: dict[str, Any] = {
            "id": trace_id,
            "name": name,
            "userId": str(ctx.user_id) if ctx.user_id else None,
            "sessionId": str(ctx.session_id) if ctx.session_id else None,
            "metadata": metadata,
            "tags": [chan_val, env_val],
            "input": mask_pii(input, self.mask_fields, strip_raw_chunk_text=True),
            "timestamp": datetime.now(UTC).isoformat(),
        }

        event = {
            "id": str(uuid4()),
            "type": "trace-create",
            "timestamp": datetime.now(UTC).isoformat(),
            "body": trace_body,
        }

        await self._enqueue_event(event)
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
        """Logs a child span adhering to the Stage-H trace tree contract."""
        if not self.is_configured or trace_id in self._unsampled_traces:
            return

        end_time = datetime.now(UTC)
        start_time = end_time - timedelta(milliseconds=latency_ms)

        sanitized_input = mask_pii(input, self.mask_fields, strip_raw_chunk_text=True)
        sanitized_output = mask_pii(output, self.mask_fields, strip_raw_chunk_text=True)
        sanitized_metadata = mask_pii(metadata, self.mask_fields, strip_raw_chunk_text=True)

        span_body: dict[str, Any] = {
            "id": str(uuid4()),
            "traceId": trace_id,
            "name": name,
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
            "input": sanitized_input,
            "output": sanitized_output,
            "metadata": sanitized_metadata,
        }

        event_type = "generation-create" if name == "llm_call" else "span-create"
        if event_type == "generation-create":
            # Extract generation usage metadata if present
            model_id = (
                sanitized_output.get("model_id")
                or sanitized_metadata.get("model_id")
                or "default_model"
            )
            span_body["model"] = model_id
            prompt_tokens = sanitized_output.get("prompt_tokens")
            completion_tokens = sanitized_output.get("completion_tokens")
            if prompt_tokens is not None or completion_tokens is not None:
                span_body["usage"] = {
                    "promptTokens": prompt_tokens or 0,
                    "completionTokens": completion_tokens or 0,
                    "totalTokens": (prompt_tokens or 0) + (completion_tokens or 0),
                }

        event = {
            "id": str(uuid4()),
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "body": span_body,
        }

        await self._enqueue_event(event)

    async def log_score(
        self,
        trace_id: str,
        name: str,
        value: float,
        comment: str | None = None,
    ) -> None:
        """Logs user feedback 👍/👎 (+1.0 / -1.0) or LLM evaluation score."""
        if not self.is_configured or trace_id in self._unsampled_traces:
            return

        score_body: dict[str, Any] = {
            "id": str(uuid4()),
            "traceId": trace_id,
            "name": name,
            "value": float(value),
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if comment:
            score_body["comment"] = mask_pii(comment, self.mask_fields)

        event = {
            "id": str(uuid4()),
            "type": "score-create",
            "timestamp": datetime.now(UTC).isoformat(),
            "body": score_body,
        }

        await self._enqueue_event(event)

    async def flush(self) -> None:
        """Flushes buffered events to Langfuse Ingestion API (Fire-and-forget, never raises)."""
        if not self.is_configured:
            async with self._lock:
                self._events_buffer.clear()
            return

        async with self._lock:
            if not self._events_buffer:
                return
            batch_to_send = list(self._events_buffer)
            self._events_buffer.clear()

        # Fire-and-forget HTTP request in worker thread
        await self._send_batch_safe(batch_to_send)

    async def _enqueue_event(self, event: dict[str, Any]) -> None:
        """Appends event to buffer and schedules asynchronous background flush if enabled."""
        async with self._lock:
            if len(self._events_buffer) >= self._buffer_size_limit:
                # Drop oldest event to avoid unbounded memory growth
                self._events_buffer.pop(0)
            self._events_buffer.append(event)

        if self._auto_flush:
            with contextlib.suppress(RuntimeError):
                asyncio.create_task(self.flush())

    async def _send_batch_safe(self, batch: list[dict[str, Any]]) -> None:
        """Safely dispatches batch of ingestion events to Langfuse API."""
        if not batch:
            return

        url = f"{self.host}/api/public/ingestion"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Langfuse-Sdk-Name": "python-enterprise-rag",
            "X-Langfuse-Sdk-Version": "0.1.0",
        }

        if self.public_key and self.secret_key:
            auth_str = f"{self.public_key}:{self.secret_key}"
            b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {b64_auth}"

        payload_dict = {"batch": batch}
        try:
            payload_bytes = json.dumps(payload_dict, default=str).encode("utf-8")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to serialize Langfuse batch payload: %s", exc)
            return

        try:
            await asyncio.to_thread(
                self._http_transport,
                url,
                headers,
                payload_bytes,
                self._request_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            # Fire-and-forget: Langfuse failure must NEVER propagate to caller or fail RAG
            logger.warning("Langfuse trace ingestion failed (fire-and-forget suppressed): %s", exc)
