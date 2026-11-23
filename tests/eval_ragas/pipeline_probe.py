"""A :class:`~tests.eval_ragas.probe.PipelineProbe` backed by the real Stage E/F pipeline.

This is the production wiring used by the CI gate (``python -m tests.eval_ragas``). It runs the
Stage-E :class:`~rag_core.retrieval.pipeline.SearchPipeline` to obtain the reranked ``chunk_id``
ordering (the IR signal) and the hydrated parent contexts, and the Stage-F
:class:`~rag_core.generation.pipeline.GenerationPipeline` to obtain the generated answer (the RAGAS
signal). Both operate under the per-sample ``access_level`` so the gate also exercises the
access-control contract end to end.

Heavy providers (embedder, LLM, DB) are injected rather than imported at module load, so importing
this module stays cheap and the gate's pure core is unaffected.
"""

from __future__ import annotations

from contextvars import ContextVar
from uuid import UUID

from rag_core.contracts.enums import AccessLevel, ChannelType
from rag_core.contracts.models import UserContext
from rag_core.generation.pipeline import GenerationPipeline
from rag_core.retrieval.pipeline import SearchPipeline
from tests.eval_ragas.dataset import GoldenSample
from tests.eval_ragas.probe import ProbeResult

# RLS isolation GUCs carried from the probe into the DB sessions the retrieval stack creates
# internally (PgVectorStore / PDP / hydrator). Under the app_user role (POSTGRES_APP_DSN) the RLS
# policies read app.current_tenant_id / app.user_access_level and fail-closed to 0 rows when unset
# (storage_schema.md §4.0); ContextVars propagate through the asyncio.to_thread worker threads the
# synchronous ORM sessions run on.
EVAL_TENANT_ID: ContextVar[UUID | None] = ContextVar("eval_tenant_id", default=None)
EVAL_ACCESS_LEVEL: ContextVar[int | None] = ContextVar("eval_access_level", default=None)


class RealPipelineProbe:
    """Adapts the wired Stage E/F pipelines to the :class:`PipelineProbe` contract."""

    name = "real_pipeline"

    def __init__(
        self,
        search_pipeline: SearchPipeline,
        generation_pipeline: GenerationPipeline,
        tenant_id: UUID,
        user_id: UUID,
        *,
        channel: ChannelType = ChannelType.WEB_WIDGET,
    ) -> None:
        self._search = search_pipeline
        self._generation = generation_pipeline
        self._tenant_id = tenant_id
        self._user_id = user_id
        self._channel = channel

    def _user_for(self, sample: GoldenSample) -> UserContext:
        # access_level/group_ids are the evaluation's ground truth here (a seeded corpus), NOT a
        # client-supplied value; see the UserContext invariant in contracts/models.py.
        return UserContext(
            user_id=self._user_id,
            tenant_id=self._tenant_id,
            channel_user_id=f"eval:{sample.id}",
            channel=self._channel,
            access_level=AccessLevel(sample.access_level),
        )

    async def __call__(self, sample: GoldenSample) -> ProbeResult:
        user = self._user_for(sample)

        # Stamp the per-sample tenant/level so the RLS GUC is applied inside the DB sessions the
        # retrieval stack creates (see EVAL_TENANT_ID / EVAL_ACCESS_LEVEL above).
        EVAL_TENANT_ID.set(self._tenant_id)
        EVAL_ACCESS_LEVEL.set(int(sample.access_level))

        # Run Stage-E retrieval exactly once and reuse it for generation: the IR metrics and the
        # RAGAS generation metrics must be computed from the SAME retrieval pass (a second pass
        # through a non-deterministic reranker/LLM would make the two halves disagree) and it halves
        # the per-sample DB/LLM cost. ``force_rag`` bypasses the intent router so a golden question
        # is never misrouted to a canned chit-chat/meta answer with no retrieval.
        search_res = await self._search.search(user, sample.question)
        retrieved_chunk_ids = [str(c.child_id) for c in search_res.candidates]
        contexts = [p.content for p in search_res.parents]

        response = await self._generation.generate_response(
            user,
            sample.question,
            force_rag=True,
            precomputed_search=search_res,
        )

        return ProbeResult(
            retrieved_chunk_ids=retrieved_chunk_ids,
            answer=response.answer,
            retrieved_contexts=contexts,
            is_confident=response.confidence_passed and search_res.is_confident,
        )
