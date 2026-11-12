"""Contract tests for Stage C - core contracts (`core/modules_spec.md`).

Covers:
- Pydantic V2 schema validation (boundary values, surplus-field rejection), round-trip
  serialization of the domain models;
- the `Settings` object (defaults + environment overrides);
- contract tests of the abstractions (abstract methods are mandatory; fake implementations are
  registrable in the DI container);
- the reference `Retriever.fuse_rrf` RRF formula (an absent document contributes 0).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from rag_core.contracts import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_LICENSE_FEATURES,
    AccessLevel,
    BaseChannel,
    BaseConfidenceGate,
    BaseDocumentSource,
    BaseEmbedder,
    BaseIntentRouter,
    BaseLicenseServer,
    BaseLicenseValidator,
    BaseLLM,
    BaseObjectStorage,
    BaseParentRepository,
    BasePolicyEvaluator,
    BaseQueryRewriter,
    BaseReranker,
    BaseRetriever,
    BaseTenantResolver,
    BaseTraceSink,
    BaseVectorStore,
    ChannelType,
    ChildChunk,
    LicenseFeature,
    LicensePayload,
    LicensePlan,
    RetrievalCandidate,
    Retriever,
    Settings,
    UserContext,
    VectorStoreCapabilities,
    WidgetBranding,
)
from rag_core.di import Container, DependencyError

# --------------------------------------------------------------------------------------------
# Pydantic model validation
# --------------------------------------------------------------------------------------------


def test_license_payload_defaults_match_canonical_feature_set() -> None:
    """A fresh LicensePayload gets the canonical default feature set (base plan)."""
    payload = LicensePayload(
        license_id=str(uuid4()),
        client_id="acme",
        issued_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert payload.plan is LicensePlan.STANDARD
    # DEFAULT_LICENSE_FEATURES is an immutable tuple; the model copies it into a fresh list.
    assert payload.features == list(DEFAULT_LICENSE_FEATURES)
    # Delivery channels are NOT in the default (granted separately on issuance).
    assert LicenseFeature.WEB_WIDGET not in payload.features
    # The default list is a copy, not a shared mutable reference.
    other = LicensePayload(
        license_id=str(uuid4()),
        client_id="globex",
        issued_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    payload.features.append(LicenseFeature.TELEGRAM)
    assert LicenseFeature.TELEGRAM not in other.features


def test_models_reject_extra_fields() -> None:
    """Domain models forbid surplus keys (extra='forbid', defense-in-depth at the boundary)."""
    with pytest.raises(ValidationError):
        UserContext(
            user_id=uuid4(),
            tenant_id=uuid4(),
            channel_user_id="u1",
            bogus_field="nope",  # type: ignore[call-arg]
        )


def test_access_level_is_int_enum() -> None:
    """AccessLevel is an ordered int scale 0..3 (used for `access_level <= clearance` filters)."""
    assert AccessLevel.PUBLIC < AccessLevel.STRICT
    assert int(AccessLevel.CONFIDENTIAL) == 2


def test_widget_branding_boundary_validation() -> None:
    """WidgetBranding enforces field constraints (hex color, https logo, <=5 questions)."""
    with pytest.raises(ValidationError):
        WidgetBranding(primary_color="red")  # not a #RRGGBB hex
    with pytest.raises(ValidationError):
        WidgetBranding(logo_url="http://insecure.example/logo.png")  # must be https
    with pytest.raises(ValidationError):
        WidgetBranding(suggested_questions=[f"q{i}" for i in range(6)])  # max 5
    ok = WidgetBranding(primary_color="#1A2B3C", logo_url="https://cdn.example/logo.png")
    assert ok.config_version == 1


def test_license_payload_round_trip_serialization() -> None:
    """Round-trip: model -> JSON -> model preserves the data (enum values included)."""
    payload = LicensePayload(
        license_id=str(uuid4()),
        client_id="acme",
        plan=LicensePlan.ENTERPRISE_BOX,
        issued_at=datetime(2026, 1, 1, tzinfo=UTC),
        features=[LicenseFeature.HYBRID_SEARCH, LicenseFeature.CLI],
    )
    restored = LicensePayload.model_validate_json(payload.model_dump_json())
    assert restored == payload
    assert restored.plan is LicensePlan.ENTERPRISE_BOX


def test_user_context_default_channel_and_level() -> None:
    """UserContext defaults: Telegram channel, PUBLIC level, empty groups."""
    user = UserContext(user_id=uuid4(), tenant_id=uuid4(), channel_user_id="123")
    assert user.channel is ChannelType.TELEGRAM
    assert user.access_level is AccessLevel.PUBLIC
    assert user.group_ids == []


# --------------------------------------------------------------------------------------------
# Settings (pydantic-settings)
# --------------------------------------------------------------------------------------------


def test_settings_defaults() -> None:
    """Canonical runtime defaults from modules_spec.md §1."""
    settings = Settings(_env_file=None)
    assert settings.rrf_k == 60
    assert settings.rrf_alpha == 0.6
    assert settings.rrf_beta == 0.4
    assert settings.confidence_threshold == 0.35
    assert settings.embedding_batch_size == 32
    assert settings.telemetry_opt_out is True
    assert settings.observability_forward_enabled is False


def test_settings_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables (UPPER-case field names) override the defaults."""
    monkeypatch.setenv("RRF_K", "40")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.5")
    monkeypatch.setenv("OIDC_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert settings.rrf_k == 40
    assert settings.confidence_threshold == 0.5
    assert settings.oidc_enabled is True


def test_settings_ignores_surplus_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings is tolerant to surplus env vars (extra='ignore'), unlike the domain models.

    A stray environment variable must NOT raise - this is the deliberate asymmetry with the
    domain models' extra='forbid'.
    """
    monkeypatch.setenv("SOME_UNRELATED_VARIABLE", "whatever")
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "some_unrelated_variable")


# --------------------------------------------------------------------------------------------
# Abstractions: abstract methods are mandatory; fakes satisfy the contract
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "port",
    [
        BaseLicenseValidator,
        BaseLicenseServer,
        BaseTenantResolver,
        BasePolicyEvaluator,
        BaseParentRepository,
        BaseVectorStore,
        BaseObjectStorage,
        BaseEmbedder,
        BaseQueryRewriter,
        BaseIntentRouter,
        BaseRetriever,
        BaseReranker,
        BaseConfidenceGate,
        BaseLLM,
        BaseDocumentSource,
        BaseChannel,
        BaseTraceSink,
    ],
)
def test_abstract_port_cannot_be_instantiated(port: type) -> None:
    """Every Base* port has >=1 abstract method, so it cannot be instantiated directly."""
    with pytest.raises(TypeError):
        port()


class _FakeEmbedder(BaseEmbedder):
    """A deterministic fake for DI/contract tests (no external model)."""

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        return ([[0.0] * 3 for _ in texts], [{0: 1.0} for _ in texts])

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        return ([0.1, 0.2, 0.3], {1: 0.5})


class _CallableEmbedder(_FakeEmbedder):
    """A fake embedder that is ALSO callable, to prove the DI container treats a callable

    object as an instance (not a factory).
    """

    def __call__(self) -> _FakeEmbedder:  # pragma: no cover - must never be invoked by the DI
        raise AssertionError("A callable instance must be stored as-is, never invoked as a factory")


class _FakeVectorStore(BaseVectorStore):
    """An in-memory fake vector store returning preconfigured arms."""

    def __init__(self, dense: list[RetrievalCandidate], sparse: list[RetrievalCandidate]) -> None:
        self._dense = dense
        self._sparse = sparse

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        return VectorStoreCapabilities(supports_sparse=True, supports_hybrid_rrf=False)

    async def upsert_chunks(self, tenant_id: UUID, chunks: list[Any]) -> None:
        return None

    async def search_dense(
        self,
        tenant_id: UUID,
        query_dense: list[float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        return self._dense

    async def search_sparse(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        return self._sparse

    async def delete_by_document(self, tenant_id: UUID, document_id: UUID) -> int:
        return 0


def test_fake_implementations_satisfy_contract() -> None:
    """Fake implementations of the ports instantiate (all abstract methods overridden)."""
    embedder = _FakeEmbedder()
    store = _FakeVectorStore([], [])
    assert isinstance(embedder, BaseEmbedder)
    assert isinstance(store, BaseVectorStore)
    assert store.capabilities.supports_sparse is True


# --------------------------------------------------------------------------------------------
# DI container
# --------------------------------------------------------------------------------------------


def test_container_resolves_instance_and_factory_singleton() -> None:
    """register_instance returns the same object; register builds a lazily cached singleton."""
    container = Container()
    embedder = _FakeEmbedder()
    container.register_instance(BaseEmbedder, embedder)
    assert container.resolve(BaseEmbedder) is embedder

    calls = {"n": 0}

    def _factory() -> _FakeVectorStore:
        calls["n"] += 1
        return _FakeVectorStore([], [])

    container.register(BaseVectorStore, _factory)
    first = container.resolve(BaseVectorStore)
    second = container.resolve(BaseVectorStore)
    assert first is second  # singleton
    assert calls["n"] == 1  # factory invoked exactly once (lazy)


def test_container_unregistered_and_double_registration() -> None:
    """Resolving an unbound port and double registration both raise DependencyError."""
    container = Container()
    with pytest.raises(DependencyError):
        container.resolve(BaseEmbedder)
    container.register_instance(BaseEmbedder, _FakeEmbedder())
    with pytest.raises(DependencyError):
        container.register_instance(BaseEmbedder, _FakeEmbedder())


def test_container_override_replaces_binding() -> None:
    """override() replaces an existing binding (used in tests to inject fakes)."""
    container = Container()
    first = _FakeEmbedder()
    second = _FakeEmbedder()
    container.register_instance(BaseEmbedder, first)
    container.override(BaseEmbedder, second)
    assert container.resolve(BaseEmbedder) is second


def test_container_override_with_callable_instance_is_stored_as_instance() -> None:
    """A callable object (has __call__) is a provider INSTANCE, not a factory - returned as-is."""
    container = Container()
    callable_instance = _CallableEmbedder()
    container.override(BaseEmbedder, callable_instance)
    # Must return the callable object itself, not the result of calling it.
    assert container.resolve(BaseEmbedder) is callable_instance


def test_container_override_with_factory_builds_lazily() -> None:
    """A plain factory function is invoked lazily and cached as a singleton."""
    container = Container()
    built = _FakeEmbedder()
    container.override(BaseEmbedder, lambda: built)
    assert container.resolve(BaseEmbedder) is built


# --------------------------------------------------------------------------------------------
# Reference Retriever: RRF fusion
# --------------------------------------------------------------------------------------------


def _candidate(child: UUID, score: float = 0.0) -> RetrievalCandidate:
    return RetrievalCandidate(
        child_id=child,
        parent_id=uuid4(),
        document_id=uuid4(),
        content="c",
        rrf_score=score,
    )


def test_fuse_rrf_absent_document_contributes_zero() -> None:
    """A document present in only one arm scores from that arm alone; ranking is by rrf_score."""
    shared = uuid4()
    dense_only = uuid4()
    sparse_only = uuid4()
    dense = [_candidate(shared), _candidate(dense_only)]
    sparse = [_candidate(shared), _candidate(sparse_only)]

    fused = Retriever.fuse_rrf(dense, sparse, k=60, alpha=0.6, beta=0.4)

    by_id = {c.child_id: c for c in fused}
    # The shared doc appears at rank 1 in both arms: 0.6/61 + 0.4/61.
    assert by_id[shared].rrf_score == pytest.approx(0.6 / 61 + 0.4 / 61)
    # dense_only: only the dense term (rank 2) contributes; the sparse term is 0.
    assert by_id[dense_only].rrf_score == pytest.approx(0.6 / 62)
    assert by_id[dense_only].sparse_rank is None
    # sparse_only: only the sparse term (rank 2) contributes.
    assert by_id[sparse_only].rrf_score == pytest.approx(0.4 / 62)
    # The shared document ranks first.
    assert fused[0].child_id == shared


@pytest.mark.asyncio
async def test_retriever_retrieve_uses_both_arms() -> None:
    """The reference Retriever embeds the query and fuses both arms via RRF."""
    c1, c2 = uuid4(), uuid4()
    store = _FakeVectorStore(dense=[_candidate(c1)], sparse=[_candidate(c2)])
    retriever = Retriever(vector_store=store, embedder=_FakeEmbedder())
    result = await retriever.retrieve("hello", tenant_id=uuid4())
    assert {c.child_id for c in result} == {c1, c2}


class _DenseOnlyVectorStore(_FakeVectorStore):
    """A fake store that does NOT support the sparse arm (supports_sparse=False)."""

    @property
    def capabilities(self) -> VectorStoreCapabilities:
        return VectorStoreCapabilities(supports_sparse=False, supports_hybrid_rrf=False)

    async def search_sparse(
        self,
        tenant_id: UUID,
        query_sparse: dict[int, float],
        k: int = 30,
        user_access_level: int = 0,
        allowed_document_ids: list[UUID] | None = None,
        denied_document_ids: list[UUID] | None = None,
    ) -> list[RetrievalCandidate]:
        raise AssertionError("search_sparse must not be called when supports_sparse=False")


@pytest.mark.asyncio
async def test_retriever_skips_sparse_when_unsupported() -> None:
    """With supports_sparse=False the retriever uses dense results only (no sparse call)."""
    c1 = uuid4()
    store = _DenseOnlyVectorStore(dense=[_candidate(c1)], sparse=[])
    retriever = Retriever(vector_store=store, embedder=_FakeEmbedder())
    result = await retriever.retrieve("hello", tenant_id=uuid4())
    assert {c.child_id for c in result} == {c1}


def test_fuse_rrf_does_not_mutate_inputs() -> None:
    """fuse_rrf copies candidates (model_copy); input arm lists/objects stay untouched."""
    shared = uuid4()
    dense = [_candidate(shared)]
    sparse = [_candidate(shared)]
    Retriever.fuse_rrf(dense, sparse)
    # The original arm candidates keep their pre-fusion state (ranks/score unchanged).
    assert dense[0].dense_rank is None
    assert dense[0].rrf_score == 0.0
    assert sparse[0].sparse_rank is None


def test_retrieval_candidate_rrf_score_defaults_to_zero() -> None:
    """An arm candidate carries rrf_score=0.0 before fusion (no placeholder needed)."""
    candidate = RetrievalCandidate(
        child_id=uuid4(), parent_id=uuid4(), document_id=uuid4(), content="c"
    )
    assert candidate.rrf_score == 0.0
    assert candidate.dense_score is None
    assert candidate.sparse_score is None


def test_embedding_model_id_is_single_source_of_truth() -> None:
    """The canonical embedding model id is one constant shared by ChildChunk and Settings."""
    chunk = ChildChunk(
        tenant_id=uuid4(),
        parent_id=uuid4(),
        content="c",
        token_count=1,
        chunk_index=0,
    )
    assert chunk.embedding_model_id == DEFAULT_EMBEDDING_MODEL
    assert Settings(_env_file=None).embedding_model == DEFAULT_EMBEDDING_MODEL
