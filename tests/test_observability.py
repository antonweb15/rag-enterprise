"""Observability, Langfuse Tracing & Feedback tests (`core/eval_and_ops.md` §2, Stage H).

Covers:
1. TraceContext mandatory attributes and serialization;
2. PII masking (passwords, tokens, emails, phone numbers) and raw chunk omission;
3. LangfuseTraceSink trace contract & 5-span tree (intent_routing, query_condensation,
   hybrid_retrieval, reranking, llm_call);
4. Fire-and-forget fault tolerance (Langfuse outage, 500, timeouts do not break RAG answer);
5. Sampling rate and retention metadata;
6. Online user feedback (👍 / 👎, score +1.0 / -1.0) and trace correlation;
7. Telegram & Web Widget feedback endpoint integrations;
8. Batch ingestion payload format and DI container wiring.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_core.channels.session import create_session_jwt
from rag_core.contracts.enums import (
    AccessLevel,
    ChannelType,
    DeploymentMode,
    IntentType,
)
from rag_core.contracts.interfaces import (
    BaseConfidenceGate,
    BaseIntentRouter,
    BaseLLM,
    BasePolicyEvaluator,
    BaseQueryRewriter,
    BaseReranker,
    BaseRetriever,
    BaseTraceSink,
)
from rag_core.contracts.models import (
    AccessDecision,
    ParentChunk,
    RetrievalCandidate,
    TenantContext,
    TraceContext,
    UserContext,
)
from rag_core.contracts.settings import Settings
from rag_core.di.container import Container
from rag_core.generation.citations import CitationResolver
from rag_core.generation.pipeline import GenerationPipeline
from rag_core.generation.prompt import PromptBuilder
from rag_core.generation.session_memory import InMemorySessionManager
from rag_core.retrieval.hydration import SmallToBigHydrator
from rag_core.retrieval.pipeline import SearchPipeline
from rag_core.tracing.langfuse import LangfuseTraceSink
from rag_core.tracing.masking import (
    mask_pii,
)
from rag_core.tracing.sink import (
    InMemoryTraceSink,
)
from services.api.channels.telegram_bot import TelegramBotRunner
from services.api.widget_app import WidgetAPIApp

# --- Fakes & Mocks ---


class FakePolicyEvaluator(BasePolicyEvaluator):
    async def effective_access(self, user: UserContext) -> AccessDecision:
        return AccessDecision(effective_access_level=user.access_level)

    async def can_access_document(self, user: UserContext, document_id: UUID) -> bool:
        return True


class FakeRetriever(BaseRetriever):
    def __init__(self, candidates: list[RetrievalCandidate] | None = None) -> None:
        self.candidates = candidates or []

    async def retrieve(
        self,
        query: str,
        tenant_id: UUID,
        user_access_level: int = 0,
        top_k: int | None = None,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        return self.candidates


class FakeReranker(BaseReranker):
    async def rerank(
        self, query: str, candidates: list[RetrievalCandidate]
    ) -> list[RetrievalCandidate]:
        for c in candidates:
            c.rerank_score = 0.95
        return candidates


class FakeConfidenceGate(BaseConfidenceGate):
    def evaluate(
        self, candidates: list[RetrievalCandidate], threshold: float | None = None
    ) -> bool:
        return True


class FakeParentRepo:
    def __init__(self, parents: dict[UUID, ParentChunk]) -> None:
        self.parents = parents

    async def fetch_parents(self, tenant_id: UUID, parent_ids: list[UUID]) -> list[ParentChunk]:
        return [self.parents[pid] for pid in parent_ids if pid in self.parents]


class FakeIntentRouter(BaseIntentRouter):
    def __init__(self, intent: IntentType = IntentType.RAG_SEARCH) -> None:
        self.intent = intent

    async def route_intent(self, query: str) -> IntentType:
        return self.intent


class FakeQueryCondenser(BaseQueryRewriter):
    async def rewrite_query(self, query: str, history: list[Any]) -> str:
        return f"condensed: {query}"


class FakeLLM(BaseLLM):
    def __init__(self, answer: str = "This is the generated answer [1].") -> None:
        self.answer = answer
        self.model_id = "test-llama-3"

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Any = "text",
    ) -> str:
        return self.answer

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        for word in self.answer.split(" "):
            yield word + " "


def _build_test_pipeline(
    trace_sink: BaseTraceSink,
    *,
    candidate_chunk_id: UUID | None = None,
    parent_chunk_id: UUID | None = None,
) -> GenerationPipeline:
    cid = candidate_chunk_id or uuid4()
    pid = parent_chunk_id or uuid4()
    doc_id = uuid4()

    candidate = RetrievalCandidate(
        child_id=cid,
        parent_id=pid,
        document_id=doc_id,
        content="Child chunk content that should not leak raw into traces",
        dense_score=0.9,
        sparse_score=0.8,
        rrf_score=0.85,
    )
    parent = ParentChunk(
        id=pid,
        tenant_id=uuid4(),
        document_id=doc_id,
        content="Parent chunk full documentation text",
        token_count=100,
        chunk_index=0,
        access_level=AccessLevel.PUBLIC,
        metadata={"source": "manual.pdf", "page": 1},
    )

    policy = FakePolicyEvaluator()
    parent_repo = FakeParentRepo({pid: parent})
    hydrator = SmallToBigHydrator(parent_repo, policy)  # type: ignore[arg-type]

    search_pipe = SearchPipeline(
        policy_evaluator=policy,
        retriever=FakeRetriever([candidate]),
        reranker=FakeReranker(),
        confidence_gate=FakeConfidenceGate(),
        hydrator=hydrator,
    )

    return GenerationPipeline(
        search_pipeline=search_pipe,
        llm=FakeLLM(),
        intent_router=FakeIntentRouter(IntentType.RAG_SEARCH),
        query_condenser=FakeQueryCondenser(),
        session_manager=InMemorySessionManager(),
        trace_sink=trace_sink,
        prompt_builder=PromptBuilder(Settings()),
        citation_resolver=CitationResolver(),
        settings=Settings(),
    )


# --- Tests ---


def test_trace_context_mandatory_attributes() -> None:
    """Verifies that TraceContext captures all mandatory attributes (core/eval_and_ops.md §2.3)."""
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    deployment_id = uuid4()
    license_id = uuid4()

    ctx = TraceContext(
        tenant_id=tenant_id,
        environment=DeploymentMode.ON_PREMISE_BOX,
        deployment_id=deployment_id,
        channel=ChannelType.TELEGRAM,
        session_id=session_id,
        user_id=user_id,
        access_level=AccessLevel.CONFIDENTIAL,
        license_id=license_id,
    )

    dumped = ctx.model_dump(mode="json")
    assert dumped["tenant_id"] == str(tenant_id)
    assert dumped["environment"] == "on_premise_box"
    assert dumped["deployment_id"] == str(deployment_id)
    assert dumped["channel"] == "telegram"
    assert dumped["session_id"] == str(session_id)
    assert dumped["user_id"] == str(user_id)
    assert dumped["access_level"] == 2
    assert dumped["license_id"] == str(license_id)


def test_pii_masking_credentials_and_inline_patterns() -> None:
    """Verifies recursive masking of credentials and pattern-based masking of PII."""
    payload = {
        "user": {
            "name": "Ivan Ivanov",
            "email": "ivan.ivanov@company.corp",
            "phone": "+7 912 345-67-89",
            "password": "SuperSecretPassword123!",
            "api_key": "sk_test_1234567890abcdef12345",
            "token": "secret_jwt_token_here",
        },
        "query": (
            "My email is support@enterprise.com, phone is 8 (999) 123-45-67 and "
            "auth is Bearer eyJhbGciOiJIUzI1NiJ9.test"
        ),
        "nested": [
            {"secret_key": "some_secret", "public_info": "safe"},
        ],
    }

    masked = mask_pii(payload)
    assert masked["user"]["password"] == "[MASKED]"
    assert masked["user"]["api_key"] == "[MASKED]"
    assert masked["user"]["token"] == "[MASKED]"
    assert masked["user"]["email"] == "[EMAIL_MASKED]"
    assert masked["user"]["phone"] == "[PHONE_MASKED]"
    assert "[EMAIL_MASKED]" in masked["query"]
    assert "[PHONE_MASKED]" in masked["query"]
    assert "Bearer [MASKED]" in masked["query"]
    assert masked["nested"][0]["secret_key"] == "[MASKED]"
    assert masked["nested"][0]["public_info"] == "safe"


def test_raw_chunk_text_strictly_omitted_from_traces() -> None:
    """Verifies that raw document/chunk texts are omitted from trace structures."""
    chunk_data = {
        "chunk_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
        "chunk_content": "This is raw secret document text that MUST NOT enter Langfuse",
        "raw_text": "Confidential financial report details",
        "page_content": "Employee records page 42",
        "score": 0.92,
    }

    masked = mask_pii(chunk_data, strip_raw_chunk_text=True)
    assert masked["chunk_id"] == "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11"
    assert masked["score"] == 0.92
    assert masked["chunk_content"] == "[RAW_TEXT_OMITTED]"
    assert masked["raw_text"] == "[RAW_TEXT_OMITTED]"
    assert masked["page_content"] == "[RAW_TEXT_OMITTED]"


@pytest.mark.asyncio
async def test_langfuse_trace_sink_span_tree_contract() -> None:
    """Verifies the complete 5-span tree contract (intent, condensation, retrieval, rerank, llm)."""
    recorded_batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        data = json.loads(payload_bytes.decode("utf-8"))
        recorded_batches.append(data)
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        host="https://langfuse.internal",
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        http_transport=mock_transport,
    )

    pipeline = _build_test_pipeline(sink)
    user = UserContext(
        user_id=uuid4(),
        tenant_id=uuid4(),
        channel_user_id="web_user_1",
        channel=ChannelType.WEB_WIDGET,
        access_level=AccessLevel.PUBLIC,
    )

    # Add 1 turn in history so query_condensation span is exercised
    await pipeline._session_mgr.add_message(
        user.tenant_id,
        "session_1",
        role="user",
        content="Prior question",
    )
    await pipeline._session_mgr.add_message(
        user.tenant_id,
        "session_1",
        role="assistant",
        content="Prior answer",
    )

    # Execute generation pipeline
    resp = await pipeline.generate_response(
        user=user,
        query="What is the parent chunk size?",
        session_id="session_1",
    )

    assert resp.answer != ""
    assert resp.trace_id is not None

    # Flush traces to transport
    await sink.flush()

    assert len(recorded_batches) > 0
    batch = recorded_batches[0]["batch"]

    # Check trace creation
    trace_events = [e for e in batch if e["type"] == "trace-create"]
    assert len(trace_events) == 1
    t_body = trace_events[0]["body"]
    assert t_body["id"] == resp.trace_id
    assert t_body["metadata"]["tenant_id"] == str(user.tenant_id)
    assert t_body["metadata"]["environment"] == "saas_cloud"
    assert t_body["metadata"]["channel"] == "web"
    assert t_body["metadata"]["access_level"] == 0

    # Check 5 spans
    span_events = [e for e in batch if e["type"] in ("span-create", "generation-create")]
    span_names = {e["body"]["name"] for e in span_events}

    expected_spans = {
        "intent_routing",
        "query_condensation",
        "hybrid_retrieval",
        "reranking",
        "llm_call",
    }
    assert expected_spans.issubset(span_names), f"Missing spans: {expected_spans - span_names}"

    # Verify retrieval span has chunk_ids and scores, but NO raw chunk text
    retrieval_span = next(e["body"] for e in span_events if e["body"]["name"] == "hybrid_retrieval")
    assert "chunk_ids" in retrieval_span["output"]
    assert "scores" in retrieval_span["output"]
    assert "chunk_content" not in retrieval_span["output"]

    # Verify reranking span has reranked_count and confidence_gate_passed
    rerank_span = next(e["body"] for e in span_events if e["body"]["name"] == "reranking")
    assert "flashrank_scores" in rerank_span["output"]
    assert "confidence_gate_passed" in rerank_span["output"]

    # Verify llm_call span has model and metrics
    llm_span = next(e["body"] for e in span_events if e["body"]["name"] == "llm_call")
    assert "model" in llm_span or "model_id" in llm_span["output"]
    assert "answer_length" in llm_span["output"]


@pytest.mark.asyncio
async def test_langfuse_trace_sink_fire_and_forget_on_network_failure() -> None:
    """Verifies that complete Langfuse network / server failures NEVER break RAG execution."""

    def failing_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        raise ConnectionRefusedError("Langfuse service is down on port 3000")

    sink = LangfuseTraceSink(
        Settings(),
        host="http://localhost:3000",
        public_key="pk-lf-test",
        secret_key="sk-lf-test",
        http_transport=failing_transport,
    )

    pipeline = _build_test_pipeline(sink)
    user = UserContext(
        user_id=uuid4(),
        tenant_id=uuid4(),
        channel_user_id="web_user_1",
        channel=ChannelType.WEB_WIDGET,
        access_level=AccessLevel.PUBLIC,
    )

    # Must succeed cleanly despite transport explosion
    resp = await pipeline.generate_response(user, "Test query with broken Langfuse")
    assert resp.answer != ""
    assert resp.trace_id is not None

    # Flush must also suppress exception
    await sink.flush()


@pytest.mark.asyncio
async def test_langfuse_sampling_rate() -> None:
    """Verifies that trace_sample_rate = 0.0 records no spans and sample_rate = 1.0 records all."""
    batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        batches.append(json.loads(payload_bytes.decode("utf-8")))
        return 200, b'{"status": "ok"}'

    # 1. Zero sample rate
    zero_sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        sample_rate=0.0,
        http_transport=mock_transport,
    )
    ctx = TraceContext(tenant_id=uuid4())
    t_id = await zero_sink.start_trace(ctx, "test_trace", {"query": "hi"})
    await zero_sink.log_span(
        t_id,
        "test_span",
        latency_ms=10.0,
        input={},
        output={},
        metadata={},
    )
    await zero_sink.flush()
    assert len(batches) == 0

    # 2. 100% sample rate
    full_sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        sample_rate=1.0,
        http_transport=mock_transport,
    )
    t_id_full = await full_sink.start_trace(ctx, "test_trace_full", {"query": "hi"})
    await full_sink.log_span(
        t_id_full,
        "test_span",
        latency_ms=10.0,
        input={"ok": 1},
        output={"done": 1},
        metadata={},
    )
    await full_sink.flush()
    assert len(batches) == 1
    assert len(batches[0]["batch"]) == 2  # trace-create + span-create


@pytest.mark.asyncio
async def test_online_feedback_score_logging() -> None:
    """Verifies logging feedback score (+1 / -1) and score event payload."""
    batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        batches.append(json.loads(payload_bytes.decode("utf-8")))
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        http_transport=mock_transport,
    )

    trace_id = str(uuid4())
    await sink.log_score(trace_id, name="user_feedback", value=1.0, comment="Very helpful answer!")
    await sink.log_score(
        trace_id, name="user_feedback", value=-1.0, comment="Password is secret123"
    )
    await sink.flush()

    assert len(batches) == 1
    scores = batches[0]["batch"]
    assert len(scores) == 2

    assert scores[0]["type"] == "score-create"
    assert scores[0]["body"]["traceId"] == trace_id
    assert scores[0]["body"]["value"] == 1.0
    assert scores[0]["body"]["comment"] == "Very helpful answer!"

    assert scores[1]["body"]["value"] == -1.0
    # Comment should be sanitized by PII mask
    assert (
        "[MASKED]" in scores[1]["body"]["comment"]
        or "secret123" not in scores[1]["body"]["comment"]
    )


@pytest.mark.asyncio
async def test_telegram_and_widget_feedback_integration() -> None:
    """Verifies feedback score recording from Telegram and Web Widget channels."""
    in_memory_sink = InMemoryTraceSink()
    pipeline = _build_test_pipeline(in_memory_sink)

    tenant_id = uuid4()
    trace_id = str(uuid4())

    # Seed the assistant message for this trace so the feedback is recognized as tenant-owned
    # (update_feedback must find it to authorize the Langfuse score write).
    await pipeline.session_manager.add_message(
        tenant_id=tenant_id,
        conversation_id=uuid4(),
        role="assistant",
        content="Seeded answer for feedback integration test",
        trace_id=trace_id,
    )

    # 1. Telegram feedback test
    tg_runner = TelegramBotRunner(
        settings=Settings(telegram_bot_token="test_token"),
        generation_pipeline=pipeline,
        tenant_id=tenant_id,
    )

    tg_pos = await tg_runner.handle_callback_query(
        telegram_user_id=12345,
        callback_data=f"feedback:thumb_up:{trace_id}",
    )
    assert "Thank you" in tg_pos
    assert len(in_memory_sink.scores) == 1
    assert in_memory_sink.scores[0].trace_id == trace_id
    assert in_memory_sink.scores[0].value == 1.0

    tg_neg = await tg_runner.handle_callback_query(
        telegram_user_id=12345,
        callback_data=f"feedback:thumb_down:{trace_id}",
    )
    assert "Thank you" in tg_neg
    assert len(in_memory_sink.scores) == 2
    assert in_memory_sink.scores[1].value == -1.0

    # 2. Web Widget API feedback endpoint test
    app = WidgetAPIApp(
        settings=Settings(jwt_secret="widget-test-secret-at-least-32-chars-long"),
        generation_pipeline=pipeline,
    )
    tenant = TenantContext(
        tenant_id=tenant_id,
        name="Corp Docs",
        slug="corp-docs",
        allowed_origins=["https://corp.com"],
        is_active=True,
    )
    app.register_tenant(tenant)

    token = create_session_jwt(
        tenant_id=tenant_id,
        user_id=uuid4(),
        auth_mode="anonymous",
        secret=app.jwt_secret,
        ttl_seconds=3600,
    )

    # Call POST /api/v1/chat/feedback via ASGI
    sent_events: list[dict[str, Any]] = []

    async def mock_receive() -> dict[str, Any]:
        return {
            "type": "http.request",
            "body": json.dumps({"trace_id": trace_id, "score": 1.0, "comment": "Great!"}).encode(
                "utf-8"
            ),
            "more_body": False,
        }

    async def mock_send(event: dict[str, Any]) -> None:
        sent_events.append(event)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/chat/feedback",
        "headers": [
            (b"authorization", f"Bearer {token}".encode("latin1")),
            (b"content-type", b"application/json"),
        ],
    }

    await app(scope, mock_receive, mock_send)
    start_event = next(e for e in sent_events if e["type"] == "http.response.start")
    assert start_event["status"] == 200

    assert len(in_memory_sink.scores) == 3
    assert in_memory_sink.scores[2].value == 1.0
    assert in_memory_sink.scores[2].comment == "Great!"

    # A foreign trace_id (not owned by this tenant) must NOT record a Langfuse score.
    async def mock_receive_foreign() -> dict[str, Any]:
        return {
            "type": "http.request",
            "body": json.dumps({"trace_id": str(uuid4()), "score": 1.0}).encode("utf-8"),
            "more_body": False,
        }

    await app(scope, mock_receive_foreign, mock_send)
    assert len(in_memory_sink.scores) == 3


def test_di_container_wires_trace_sink() -> None:
    """Verifies that BaseTraceSink is registered and resolvable via DI container."""
    container = Container()
    sink = LangfuseTraceSink(Settings())
    container.register_instance(BaseTraceSink, sink)

    resolved = container.resolve(BaseTraceSink)
    assert resolved is sink
    assert isinstance(resolved, LangfuseTraceSink)


@pytest.mark.asyncio
async def test_unconfigured_langfuse_sink_noop() -> None:
    """Verifies that an unconfigured LangfuseTraceSink is a clean no-op."""
    called = False

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        nonlocal called
        called = True
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        public_key=None,
        secret_key=None,
        http_transport=mock_transport,
    )
    assert not sink.is_configured

    ctx = TraceContext(tenant_id=uuid4())
    t_id = await sink.start_trace(ctx, "noop_trace", {"query": "test"})
    await sink.log_span(t_id, "test_span", latency_ms=5.0, input={}, output={}, metadata={})
    await sink.log_score(t_id, "feedback", 1.0)
    await sink.flush()

    assert not called
    assert len(sink._events_buffer) == 0


@pytest.mark.asyncio
async def test_unsampled_traces_drop_scores() -> None:
    """Verifies that log_score drops scores for unsampled traces."""
    batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        batches.append(json.loads(payload_bytes.decode("utf-8")))
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        sample_rate=0.0,
        http_transport=mock_transport,
    )

    ctx = TraceContext(tenant_id=uuid4())
    t_id = await sink.start_trace(ctx, "unsampled_trace", {"query": "hello"})
    assert t_id in sink._unsampled_traces

    await sink.log_score(t_id, "user_feedback", 1.0)
    await sink.flush()

    assert len(batches) == 0


@pytest.mark.asyncio
async def test_first_turn_query_condensation_span_emitted() -> None:
    """Verifies that query_condensation span is emitted unconditionally even with empty history."""
    recorded_batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        recorded_batches.append(json.loads(payload_bytes.decode("utf-8")))
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        http_transport=mock_transport,
    )
    pipeline = _build_test_pipeline(sink)
    user = UserContext(
        user_id=uuid4(),
        tenant_id=uuid4(),
        channel_user_id="user_first_turn",
        channel=ChannelType.WEB_WIDGET,
        access_level=AccessLevel.PUBLIC,
    )

    resp = await pipeline.generate_response(user, "First turn question with no history")
    assert resp.trace_id is not None
    await sink.flush()

    assert len(recorded_batches) > 0
    batch = recorded_batches[0]["batch"]
    span_names = [e["body"]["name"] for e in batch if "name" in e["body"]]
    assert "query_condensation" in span_names
    qc_span = next(e["body"] for e in batch if e["body"].get("name") == "query_condensation")
    assert qc_span["output"]["was_condensed"] is False


@pytest.mark.asyncio
async def test_background_auto_flush() -> None:
    """Verifies that events auto-flush asynchronously when auto_flush=True."""
    batches: list[dict[str, Any]] = []

    def mock_transport(
        url: str, headers: dict[str, str], payload_bytes: bytes, timeout: float
    ) -> tuple[int, bytes]:
        batches.append(json.loads(payload_bytes.decode("utf-8")))
        return 200, b'{"status": "ok"}'

    sink = LangfuseTraceSink(
        Settings(),
        public_key="pk_test",
        secret_key="sk_test",
        auto_flush=True,
        http_transport=mock_transport,
    )

    ctx = TraceContext(tenant_id=uuid4())
    t_id = await sink.start_trace(ctx, "auto_flush_trace", {"query": "ping"})
    await sink.log_score(t_id, "user_feedback", 1.0)

    # Allow asyncio tasks to yield
    import asyncio

    await asyncio.sleep(0.05)

    assert len(batches) >= 1
    event_types = [e["type"] for b in batches for e in b["batch"]]
    assert "trace-create" in event_types
    assert "score-create" in event_types


def test_build_generation_pipeline_auto_wires_langfuse_sink() -> None:
    """Verifies that build_generation_pipeline constructs LangfuseTraceSink when configured."""
    from rag_core.generation.pipeline import build_generation_pipeline

    policy = FakePolicyEvaluator()
    search_pipe = SearchPipeline(
        policy_evaluator=policy,
        retriever=FakeRetriever(),
        reranker=FakeReranker(),
        confidence_gate=FakeConfidenceGate(),
        hydrator=SmallToBigHydrator(FakeParentRepo({}), policy),  # type: ignore[arg-type]
    )

    settings_with_langfuse = Settings(
        langfuse_public_key="pk_auto_test",
        langfuse_secret_key="sk_auto_test",
    )
    pipe = build_generation_pipeline(search_pipe, settings=settings_with_langfuse)
    assert isinstance(pipe.trace_sink, LangfuseTraceSink)
    assert pipe.trace_sink.is_configured
