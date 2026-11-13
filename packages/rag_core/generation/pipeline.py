"""Generation pipeline orchestration (`core/architecture.md` §2, Stage F).

Provides:
- :class:`GenerationPipeline` - end-to-end orchestration of query understanding, retrieval,
  anti-prompt-injection packing, LLM generation, citation resolution, and tracing;
- :func:`build_generation_pipeline` - composition root for the generation flow.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from rag_core.contracts.enums import IntentType
from rag_core.contracts.interfaces import (
    BaseIntentRouter,
    BaseLLM,
    BaseQueryRewriter,
    BaseTraceSink,
)
from rag_core.contracts.models import (
    RAGResponse,
    TraceContext,
    UserContext,
)
from rag_core.contracts.settings import Settings
from rag_core.generation.citations import CitationResolver
from rag_core.generation.intent_router import IntentRouter
from rag_core.generation.prompt import PromptBuilder
from rag_core.generation.query_condenser import QueryCondenser
from rag_core.generation.session_memory import BaseSessionManager, InMemorySessionManager
from rag_core.llm.fallback import FallbackLLMRunner
from rag_core.retrieval.pipeline import SearchPipeline
from rag_core.tracing.sink import NoOpTraceSink, TraceManager

NO_INFO_FALLBACK_ANSWER = (
    "Предоставленные корпоративные документы не содержат информации для ответа на этот вопрос."
)

CHIT_CHAT_SYSTEM_PROMPT = (
    "You are the Enterprise RAG corporate AI assistant. "
    "Respond to the user's greeting, small talk, or polite phrase concisely, "
    "professionally, and politely in Russian."
)

META_COMMAND_SYSTEM_PROMPT = (
    "You are the Enterprise RAG corporate AI assistant. "
    "Explain what you can do (answer questions based on indexed corporate knowledge base), "
    "mention available features, and invite the user to ask a question."
)


class GenerationPipeline:
    """End-to-end RAG Generation Pipeline."""

    def __init__(
        self,
        search_pipeline: SearchPipeline,
        llm: BaseLLM,
        intent_router: BaseIntentRouter,
        query_condenser: BaseQueryRewriter,
        session_manager: BaseSessionManager,
        trace_sink: BaseTraceSink | None = None,
        prompt_builder: PromptBuilder | None = None,
        citation_resolver: CitationResolver | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._search = search_pipeline
        self._llm = llm
        self._router = intent_router
        self._condenser = query_condenser
        self._session_mgr = session_manager
        self._trace_mgr = TraceManager(trace_sink or NoOpTraceSink())
        self._prompt_builder = prompt_builder or PromptBuilder(settings)
        self._citation_resolver = citation_resolver or CitationResolver()
        self._settings = settings or Settings()

    async def generate_response(
        self,
        user: UserContext,
        query: str,
        *,
        session_id: UUID | str | None = None,
        trace_ctx: TraceContext | None = None,
    ) -> RAGResponse:
        """Executes the complete Query Understanding -> Retrieval -> LLM Generation pipeline."""
        conv_id = session_id or uuid4()
        effective_trace_ctx = trace_ctx or TraceContext(
            tenant_id=user.tenant_id,
            user_id=user.user_id,
            access_level=user.access_level,
            channel=user.channel,
            environment=self._settings.deployment_mode,
        )

        trace_id = await self._trace_mgr.start_trace(
            effective_trace_ctx,
            name="rag_query_pipeline",
            input={"query": query, "user_id": str(user.user_id)},
        )

        # 1. Load conversation history (1 turn = 2 messages: user + assistant)
        history = await self._session_mgr.get_history(
            user.tenant_id, conv_id, limit=self._settings.max_history_turns * 2
        )

        # 2. Intent routing
        async with self._trace_mgr.span(
            trace_id, "intent_routing", input={"query": query}
        ) as span_out:
            intent = await self._router.route_intent(query)
            span_out["intent"] = intent.value

        # 3. Handle non-RAG intents
        if intent == IntentType.CHIT_CHAT:
            return await self._handle_chit_chat(user, query, conv_id, trace_id)

        if intent == IntentType.META_COMMAND:
            return await self._handle_meta_command(user, query, conv_id, trace_id)

        # 4. RAG Search path: Query condensation
        standalone_query = query
        if history:
            async with self._trace_mgr.span(
                trace_id,
                "query_condensation",
                input={"raw_query": query, "history_len": len(history)},
            ) as span_out:
                standalone_query = await self._condenser.rewrite_query(query, history)
                span_out["standalone_query"] = standalone_query

        # 5. Hybrid search + Reranking (emits hybrid_retrieval and reranking spans)
        search_res = await self._search.search(
            user,
            standalone_query,
            trace_id=trace_id,
            trace_manager=self._trace_mgr,
        )

        # 6. Confidence check
        if not search_res.is_confident or not search_res.parents:
            await self._persist_turn(
                user,
                conv_id,
                user_message=query,
                assistant_message=NO_INFO_FALLBACK_ANSWER,
                trace_id=trace_id,
            )
            return RAGResponse(
                answer=NO_INFO_FALLBACK_ANSWER,
                citations=[],
                confidence_passed=False,
                trace_id=trace_id,
            )

        # 7. Prompt packing (with anti-injection sandbox, dedup and token budget)
        user_prompt, sys_prompt, included_chunks = self._prompt_builder.build_generation_prompt(
            standalone_query, search_res.parents
        )

        # 8. LLM call
        async with self._trace_mgr.span(
            trace_id,
            "llm_call",
            input={"query": standalone_query, "chunks_count": len(included_chunks)},
        ) as span_out:
            raw_answer = await self._llm.generate(
                prompt=user_prompt,
                system_prompt=sys_prompt,
                temperature=self._settings.generation_temperature,
                max_tokens=self._settings.generation_max_tokens,
            )
            span_out["answer_length"] = len(raw_answer)

        # 9. Citation resolution with PDP recheck
        citations = await self._citation_resolver.resolve_citations(
            raw_answer, included_chunks, user=user
        )

        # 10. Record to session memory
        await self._persist_turn(
            user,
            conv_id,
            user_message=query,
            assistant_message=raw_answer,
            trace_id=trace_id,
        )

        return RAGResponse(
            answer=raw_answer,
            citations=citations,
            confidence_passed=True,
            trace_id=trace_id,
        )

    async def _persist_turn(
        self,
        user: UserContext,
        conv_id: UUID | str,
        user_message: str,
        assistant_message: str,
        trace_id: str,
    ) -> None:
        """Helper for recording dialog turn to session storage."""
        channel_str = user.channel.value if hasattr(user.channel, "value") else str(user.channel)
        await self._session_mgr.add_message(
            user.tenant_id,
            conv_id,
            role="user",
            content=user_message,
            user_id=str(user.user_id),
            channel=channel_str,
            trace_id=trace_id,
        )
        await self._session_mgr.add_message(
            user.tenant_id,
            conv_id,
            role="assistant",
            content=assistant_message,
            user_id=str(user.user_id),
            channel=channel_str,
            trace_id=trace_id,
        )

    async def _handle_chit_chat(
        self, user: UserContext, query: str, conv_id: UUID | str, trace_id: str
    ) -> RAGResponse:
        async with self._trace_mgr.span(
            trace_id, "llm_call", input={"query": query, "type": "chit_chat"}
        ) as span_out:
            ans = await self._llm.generate(
                prompt=query,
                system_prompt=CHIT_CHAT_SYSTEM_PROMPT,
                temperature=self._settings.generation_temperature,
                max_tokens=250,
            )
            span_out["answer"] = ans

        await self._persist_turn(
            user,
            conv_id,
            user_message=query,
            assistant_message=ans,
            trace_id=trace_id,
        )
        return RAGResponse(answer=ans, citations=[], confidence_passed=True, trace_id=trace_id)

    async def _handle_meta_command(
        self, user: UserContext, query: str, conv_id: UUID | str, trace_id: str
    ) -> RAGResponse:
        async with self._trace_mgr.span(
            trace_id, "llm_call", input={"query": query, "type": "meta_command"}
        ) as span_out:
            ans = await self._llm.generate(
                prompt=query,
                system_prompt=META_COMMAND_SYSTEM_PROMPT,
                temperature=self._settings.generation_temperature,
                max_tokens=300,
            )
            span_out["answer"] = ans

        await self._persist_turn(
            user,
            conv_id,
            user_message=query,
            assistant_message=ans,
            trace_id=trace_id,
        )
        return RAGResponse(answer=ans, citations=[], confidence_passed=True, trace_id=trace_id)


def build_generation_pipeline(
    search_pipeline: SearchPipeline,
    llm: BaseLLM | None = None,
    session_manager: BaseSessionManager | None = None,
    trace_sink: BaseTraceSink | None = None,
    settings: Settings | None = None,
) -> GenerationPipeline:
    """Composition root for Stage F generation pipeline."""
    resolved_settings = settings or Settings()
    effective_llm = llm or FallbackLLMRunner(settings=resolved_settings)
    intent_router = IntentRouter(effective_llm, resolved_settings)
    query_condenser = QueryCondenser(effective_llm, resolved_settings)
    effective_session_mgr = session_manager or InMemorySessionManager()
    effective_trace_sink = trace_sink or NoOpTraceSink()

    return GenerationPipeline(
        search_pipeline=search_pipeline,
        llm=effective_llm,
        intent_router=intent_router,
        query_condenser=query_condenser,
        session_manager=effective_session_mgr,
        trace_sink=effective_trace_sink,
        prompt_builder=PromptBuilder(resolved_settings),
        citation_resolver=CitationResolver(policy_evaluator=search_pipeline.policy_evaluator),
        settings=resolved_settings,
    )
