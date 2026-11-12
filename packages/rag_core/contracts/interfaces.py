"""Abstract core interfaces - the `Base*` ports (`core/modules_spec.md` §2).

These ABCs define the swappable ports of the clean core (licensing, policy decision point,
storage, embedder, retriever/reranker, LLM, channels, tracing). Concrete providers are bound to
the ports via the DI container (`rag_core.di`); the core depends only on the abstractions.

The single concrete class here is the reference `Retriever`: it performs Reciprocal Rank Fusion
(RRF) in the application layer (not in SQL), as mandated by the contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel

from rag_core.contracts.enums import IntentType, LicensePlan
from rag_core.contracts.models import (
    AccessDecision,
    ActivationCode,
    ChannelCapabilities,
    ChannelEvent,
    ChildChunk,
    LicenseIssueRequest,
    LicensePayload,
    LicenseRevokeRequest,
    ParentChunk,
    RAGResponse,
    RawDocument,
    RetrievalCandidate,
    TenantContext,
    TraceContext,
    UserContext,
)
from rag_core.contracts.settings import Settings

# --- 2.0. Licensing & Multi-Tenancy Interfaces ---


class BaseLicenseValidator(ABC):
    @abstractmethod
    def validate_token(self, token: str) -> LicensePayload:
        """Offline validation of the Ed25519 digital signature and the token's expiry."""

    @abstractmethod
    async def verify_heartbeat(self, license_payload: LicensePayload) -> bool:
        """An online ping to the license server to verify absence of revocation (72h grace)."""

    @abstractmethod
    async def enforce_quota(
        self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]
    ) -> None:
        """Checks license quotas (max_documents, max_users); raises QuotaExceededError on excess."""


class BaseLicenseServer(ABC):
    """The server half of licensing (vendor contour ONLY, see core/license_server.md).

    The private Ed25519 signing key lives in a separate signing service / HSM / KMS, not in this
    process and not in the backoffice DB.
    """

    @abstractmethod
    async def issue_license(self, request: LicenseIssueRequest) -> str:
        """Issues a signed Ed25519 license token (issuance). Returns license_token."""

    @abstractmethod
    async def revoke_license(self, request: LicenseRevokeRequest) -> None:
        """Revokes a license: marks revoked_at/reason + publishes to the CRL (revocation list)."""

    @abstractmethod
    async def register_heartbeat(self, hardware_id: str, license_id: UUID) -> None:
        """Receives a heartbeat from a box: updates last_heartbeat_at, computes grace/restricted."""

    @abstractmethod
    async def generate_activation_code(
        self, customer_id: UUID, plan: LicensePlan
    ) -> ActivationCode:
        """Generates a code 'RAG-ENT-XXXX-YYYY-ZZZZ' per order/customer."""


class BaseTenantResolver(ABC):
    @abstractmethod
    async def resolve_tenant(self, identifier: str) -> TenantContext:
        """Resolves the tenant context by API key, slug or widget domain."""

    @abstractmethod
    async def enforce_quota(
        self, tenant_id: UUID, metric: Literal["documents", "users", "requests"]
    ) -> None:
        """Checks the tenant's current resource consumption against the set limits."""


# --- 2.0.2. Policy Decision Point (the single access-decision point; Stage B/D) ---


class BasePolicyEvaluator(ABC):
    """The single PDP (Policy Decision Point): computes a subject's effective access.

    Called BEFORE the search (Retriever/VectorStore), on parent-content hydration and in the
    citation resolver (citation re-check). Returns NOT a bool, but a predicate/set of allowed
    resources, to preserve the correctness of top-k/RRF (see AccessDecision). When the
    enterprise_acl flag is off, only user.access_level is considered (ACL/groups are ignored).
    """

    @abstractmethod
    async def effective_access(self, user: UserContext) -> AccessDecision:
        """Effective right = f(user.access_level, group.access_level, ACL allow/deny).

        effective_access_level = max(user.access_level, max(group.access_level)).
        """

    @abstractmethod
    async def can_access_document(self, user: UserContext, document_id: UUID) -> bool:
        """A point-check (used for citation re-check/parent hydration). fail-closed."""


class BaseParentRepository(ABC):
    """The parent-hydration persistence port (tenant-scoped, read-only).

    Centralized here with the other `Base*` ports; the concrete SQLAlchemy implementation lives in
    `rag_core.retrieval.repository` and is consumed by the Small-to-Big hydrator.
    """

    @abstractmethod
    async def fetch_parents(self, tenant_id: UUID, parent_ids: list[UUID]) -> list[ParentChunk]:
        """Loads the parent chunks for the given ids (tenant-scoped). Order is not guaranteed."""


# --- 2.0.1. Storage Ports (swappable implementations) ---


class VectorStoreCapabilities(BaseModel):
    supports_sparse: bool = True
    supports_hybrid_rrf: bool = False  # True only where a native server implementation exists
    max_dimensions: int = 1024


class BaseVectorStore(ABC):
    """The vector-store port.

    The contract is the separate search arms; RRF fusion is performed in the application layer
    (Retriever), not inside the store.

    `user_access_level` in the arm signatures is typed `int` (not the `AccessLevel` enum) on
    purpose: it carries the raw value of the Postgres GUC `app.user_access_level` / the SQL
    `access_level <= :level` predicate. `AccessLevel` is the domain-model enum (an `int`
    subclass), so an `AccessLevel` may be passed here as well.
    """

    @property
    @abstractmethod
    def capabilities(self) -> VectorStoreCapabilities:
        """Capability flags of a specific implementation (pgvector / Qdrant / Chroma)."""

    @abstractmethod
    async def upsert_chunks(self, tenant_id: UUID, chunks: list[ChildChunk]) -> None:
        """Idempotent write/update of child chunks together with their vectors."""

    @abstractmethod
    async def search_dense(
        self,
        tenant_id: UUID,
        query_dense: list[float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        """The dense arm: Top-k by cosine distance with a tenant_id and access_level filter.

        The access predicate matches the PDP (AccessDecision): a chunk is visible when
        ``access_level <= user_access_level`` **OR** its document is in ``allowed_document_ids``
        (an explicit ACL grant can raise visibility above the clearance level). Anything in
        ``denied_document_ids`` is excluded regardless (denials win). All predicates are applied
        BEFORE LIMIT k - never as a post-filter, so as not to break top-k/RRF.
        """

    @abstractmethod
    async def search_sparse(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        """The lexical arm (sparsevec / BM25). If supports_sparse=False - returns an empty list.

        allowed_document_ids/denied_document_ids are applied the same way as in search_dense
        (OR-combined with the level filter / excluded; a predicate before LIMIT).
        """

    @abstractmethod
    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        """Cascade deletion of a document's vectors (for DELETE /admin/documents/{id})."""


class BaseObjectStorage(ABC):
    """The object-storage port (S3 / MinIO / R2 / local). All operations are strictly per-tenant."""

    @abstractmethod
    async def put(
        self, tenant_id: UUID, key: str, data: bytes, content_type: str | None = None
    ) -> str:
        """Writes an object under the prefix {tenant_id}/{key}. Returns a storage_ref (URI)."""

    @abstractmethod
    async def get(self, tenant_id: UUID, key: str) -> bytes:
        """Reads an object stored under the prefix {tenant_id}/{key}."""

    @abstractmethod
    async def delete(self, tenant_id: UUID, key: str) -> None:
        """Deletes an object stored under the prefix {tenant_id}/{key}."""

    @abstractmethod
    async def list(self, tenant_id: UUID, prefix: str = "", limit: int = 1000) -> list[str]:
        """Lists object keys for the tenant under an optional prefix."""


# --- 2.1. Embedder Interface ---


class BaseEmbedder(ABC):
    @abstractmethod
    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        """A single BGE-M3 pass: generation of dense (1024-dim) and sparse representations."""

    @abstractmethod
    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        """Generation of the dense and sparse vector for a search query."""


# --- 2.2. Query Rewriter & Intent Router ---


class BaseQueryRewriter(ABC):
    @abstractmethod
    async def rewrite_query(self, query: str, history: list[dict[str, str]]) -> str:
        """Query Condensation: transforming a follow-up question into a standalone query."""


class BaseIntentRouter(ABC):
    @abstractmethod
    async def route_intent(self, query: str) -> IntentType:
        """Intent routing: RAG search, Chit-Chat or Meta command."""


# --- 2.3. Retriever & Reranker Interface ---


class BaseRetriever(ABC):
    @abstractmethod
    async def retrieve(
        self,
        query: str,
        tenant_id: UUID,
        user_access_level: int = 0,
        top_k: int | None = None,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        """Hybrid Dense + Sparse search with RRF fusion and isolation by tenant_id.

        user_access_level/allowed_document_ids/denied_document_ids are taken from AccessDecision
        (PolicyEvaluator), computed on the backend; the client does not pass them. user_access_level
        is `int` (the raw Postgres GUC value); see BaseVectorStore for the int-vs-AccessLevel
        rationale. `top_k=None` defers to `Settings.retrieval_top_candidates`.
        """


class Retriever(BaseRetriever):
    """The reference implementation: calls BaseVectorStore.search_dense()/.search_sparse(),

    then performs RRF fusion IN CODE using the RRF parameters from `Settings`
    (rrf_k/rrf_alpha/rrf_beta, retrieval_top_candidates) - the single source of values, no
    hardcode. If no `Settings` is supplied a default one is constructed, so changing e.g. RRF_K
    in the environment propagates into fusion rather than being silently ignored.

    RRF is an application-layer contract, not SQL. The SQL function hybrid_search_rrf remains an
    internal optimization detail of PgVectorStore (called only when
    capabilities.supports_hybrid_rrf = True and identical in result to the code fusion).
    """

    def __init__(
        self,
        vector_store: BaseVectorStore,
        embedder: BaseEmbedder,
        settings: Settings | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._embedder = embedder
        self._settings = settings or Settings()

    async def retrieve(
        self,
        query: str,
        tenant_id: UUID,
        user_access_level: int = 0,
        top_k: int | None = None,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        # None => use the canonical default from Settings (retrieval_top_candidates).
        effective_top_k = self._settings.retrieval_top_candidates if top_k is None else top_k
        query_dense, query_sparse = await self._embedder.embed_query(query)
        dense = await self._vector_store.search_dense(
            tenant_id,
            query_dense,
            k=effective_top_k,
            user_access_level=user_access_level,
            allowed_document_ids=allowed_document_ids,
            denied_document_ids=denied_document_ids,
        )
        sparse: list[RetrievalCandidate] = []
        if self._vector_store.capabilities.supports_sparse:
            sparse = await self._vector_store.search_sparse(
                tenant_id,
                query_sparse,
                k=effective_top_k,
                user_access_level=user_access_level,
                allowed_document_ids=allowed_document_ids,
                denied_document_ids=denied_document_ids,
            )
        fused = self.fuse_rrf(
            dense,
            sparse,
            k=self._settings.rrf_k,
            alpha=self._settings.rrf_alpha,
            beta=self._settings.rrf_beta,
        )
        # Cap the fused list to Top-N (architecture.md §2 p.8: "the resulting Top-30 chunks are
        # reranked"): each arm yields up to effective_top_k, so the union can be up to 2x that.
        return fused[:effective_top_k]

    @staticmethod
    def fuse_rrf(
        dense: list[RetrievalCandidate],
        sparse: list[RetrievalCandidate],
        k: int = 60,
        alpha: float = 0.6,
        beta: float = 0.4,
    ) -> list[RetrievalCandidate]:
        """RRF(d) = alpha/(k + rank_dense(d)) + beta/(k + rank_sparse(d)).

        A document absent from a list => rank infinity => the term's contribution is 0.
        Ranks are 1-based (the position in the already-sorted search arm).

        Tie-breaking: `sorted` is stable, so candidates with an equal rrf_score keep their
        insertion order, i.e. dense-arm order first, then sparse-only candidates.
        """
        fused: dict[UUID, RetrievalCandidate] = {}
        scores: dict[UUID, float] = {}

        def _accumulate(
            candidates: list[RetrievalCandidate], weight: float, arm: Literal["dense", "sparse"]
        ) -> None:
            for rank, candidate in enumerate(candidates, start=1):
                base = fused.get(candidate.child_id)
                if base is None:
                    # Copy so the original arm candidates are not mutated.
                    base = candidate.model_copy()
                    fused[candidate.child_id] = base
                    scores[candidate.child_id] = 0.0
                if arm == "dense":
                    base.dense_rank = rank
                else:
                    base.sparse_rank = rank
                scores[candidate.child_id] += weight / (k + rank)

        _accumulate(dense, alpha, "dense")
        _accumulate(sparse, beta, "sparse")

        for child_id, candidate in fused.items():
            candidate.rrf_score = scores[child_id]

        return sorted(fused.values(), key=lambda c: c.rrf_score, reverse=True)


class BaseReranker(ABC):
    @abstractmethod
    async def rerank(
        self, query: str, candidates: list[RetrievalCandidate]
    ) -> list[RetrievalCandidate]:
        """Re-ranking of candidates (FlashRank) with rerank_score computation."""


class BaseConfidenceGate(ABC):
    @abstractmethod
    def evaluate(
        self, candidates: list[RetrievalCandidate], threshold: float | None = None
    ) -> bool:
        """Checks max(rerank_score) >= threshold.

        If threshold=None, settings.confidence_threshold is used.
        """


# --- 2.4. LLM Provider Interface ---


class BaseLLM(ABC):
    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        """Generation of full text or valid JSON.

        When temperature/max_tokens=None, settings.generation_temperature and
        settings.generation_max_tokens are used (a single source of values, no hardcode).
        """

    @abstractmethod
    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming token generation (Token Stream).

        When temperature/max_tokens=None, settings.generation_temperature and
        settings.generation_max_tokens are used.
        """


# --- 2.5. Document Source Interface ---


class BaseDocumentSource(ABC):
    @abstractmethod
    async def fetch_documents(self) -> list[RawDocument]:
        """Loading documents from a source (local folder, S3, Confluence)."""

    @abstractmethod
    async def watch_for_changes(self) -> AsyncGenerator[RawDocument, None]:
        """A generator of file add/change events with debounce."""


# --- 2.6. Channel Interface (see core/channels.md) ---


class BaseChannel(ABC):
    """The delivery-channel port.

    The invariant is 'one input, two outputs': receive() normalizes native events into
    ChannelEvent (input), send_response()/stream_response() are the output. The retrieval core
    uses only ChannelEvent and capabilities flags at the channel boundary.
    """

    @property
    @abstractmethod
    def capabilities(self) -> ChannelCapabilities:
        """The adapter's capabilities (streaming/markdown/citations/buttons/...).

        Confirmed by the adapter's mandatory contract test.
        """

    @abstractmethod
    async def start(self) -> None:
        """Starts the channel's incoming-message handler."""

    @abstractmethod
    async def receive(self) -> AsyncGenerator[ChannelEvent, None]:
        """A generator of normalized incoming channel events (ChannelEvent)."""

    @abstractmethod
    async def send_response(self, user_id: str, response: RAGResponse) -> None:
        """Sends the composed response to the user."""

    @abstractmethod
    async def stream_response(
        self,
        user_id: str,
        token_stream: AsyncGenerator[str, None],
        initial_message_id: str | None = None,
    ) -> RAGResponse:
        """Streaming response delivery with throttling and assembly of the final RAGResponse."""

    async def request_cancellation(self, user_id: str) -> None:
        """An optional generation-cancellation hook.

        The core calls it but is not obliged to; adapters without supports_cancellation may leave
        it unoverridden (no-op).
        """
        return None


# --- 2.5. Observability (Tracing Sink) ---


class BaseTraceSink(ABC):
    """The trace-writing port (implementation - Langfuse).

    Fire-and-forget writes: a sink failure does not block the RAG response
    (see core/eval_and_ops.md §2.5).
    """

    @abstractmethod
    async def start_trace(self, ctx: TraceContext, name: str, input: dict[str, Any]) -> str:
        """Opens a trace; returns a trace_id for correlation (messages.langfuse_trace_id)."""

    @abstractmethod
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
        """Writes a span (intent/retrieval/rerank/llm_call) with mandatory attributes."""

    @abstractmethod
    async def log_score(self, trace_id: str, name: str, value: float) -> None:
        """Online feedback 👍/👎 (+1/-1) and LLM-as-a-Judge metrics."""

    async def flush(self) -> None:
        """An optional buffer-flush hook (best-effort, raises no exceptions)."""
        return None
