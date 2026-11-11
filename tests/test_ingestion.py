"""Unit tests for Stage D - Ingestion Pipeline & Embedder (`IMPLEMENTATION_PLAN.md` §2 Stage D).

Covers the plan's required test matrix:
- ingest idempotency (a repeated file with the same hash is skipped);
- re-embedding: a bumped embedding_version re-indexes only the affected chunks, old versions are not
  mixed into one column;
- token-aware chunk boundaries (no word is cut) and size bounds;
- transactional rollback on embedder failure + retry logic (exponential backoff);
- strict batching by 32 and near-duplicate child suppression;
- per-tenant object storage and the File Watcher (st_size stability + hash dedup).
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from rag_core.contracts.enums import AccessLevel, DocumentStatus
from rag_core.contracts.models import ChildChunk, ParentChunk, RawDocument
from rag_core.ingestion import (
    BgeM3Embedder,
    DocumentParser,
    FileWatcher,
    IngestionPipeline,
    LocalObjectStorage,
    ParentChildSplitter,
    ReembeddingPlanner,
    ReembeddingService,
    S3ObjectStorage,
    SplitterConfig,
    batched,
    build_splitter,
    compute_content_hash,
    near_duplicate_key,
    suppress_near_duplicates,
)
from rag_core.ingestion.pipeline import BaseIngestRepository, retry_async

# --------------------------------------------------------------------------------------------
# Test doubles
# --------------------------------------------------------------------------------------------


class _RecordingEmbedder(BgeM3Embedder):
    """A BGE-M3 embedder with a deterministic encode_fn that records the batch sizes it sees."""

    def __init__(self, batch_size: int = 32, dim: int = 4) -> None:
        self.batch_sizes: list[int] = []

        def _encode(batch: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
            self.batch_sizes.append(len(batch))
            dense = [[float(len(t))] * dim for t in batch]
            sparse = [{0: float(len(t)) + 1.0} for t in batch]
            return dense, sparse

        super().__init__(batch_size=batch_size, encode_fn=_encode)


class _FakeRepository(BaseIngestRepository):
    """An in-memory ingest repository recording saves and notifications."""

    def __init__(self, existing_hashes: set[str] | None = None) -> None:
        self.existing: set[str] = existing_hashes or set()
        self.saved: list[tuple[RawDocument, list[ParentChunk], list[ChildChunk]]] = []

    async def document_exists(self, tenant_id: UUID, content_hash: str) -> bool:
        return content_hash in self.existing

    async def save_document(
        self,
        document: RawDocument,
        parents: list[ParentChunk],
        children: list[ChildChunk],
    ) -> None:
        self.saved.append((document, parents, children))
        self.existing.add(document.content_hash)


async def _no_sleep(_: float) -> None:
    return None


# --------------------------------------------------------------------------------------------
# Splitter: token-aware boundaries and size bounds
# --------------------------------------------------------------------------------------------


def test_splitter_never_cuts_a_word_and_respects_bounds() -> None:
    cfg = SplitterConfig(
        parent_min_tokens=10,
        parent_max_tokens=20,
        child_min_tokens=3,
        child_max_tokens=5,
        child_overlap_tokens=2,
    )
    splitter = ParentChildSplitter(cfg)
    words = [f"word{i}" for i in range(53)]
    text = " ".join(words)

    parents = splitter.split(text)

    assert parents, "expected at least one parent"
    reconstructed: list[str] = []
    for parent in parents:
        assert parent.token_count <= cfg.parent_max_tokens
        # Every parent token is an intact original word (no word was cut).
        for tok in parent.content.split():
            assert tok in words
        for child in parent.children:
            assert 0 < child.token_count <= cfg.child_max_tokens
            for tok in child.content.split():
                assert tok in words
        reconstructed.extend(parent.content.split())
    # Parents partition the token stream in order with no loss.
    assert reconstructed == words


def test_splitter_children_overlap_window_advances() -> None:
    cfg = SplitterConfig(
        parent_min_tokens=1,
        parent_max_tokens=100,
        child_min_tokens=2,
        child_max_tokens=4,
        child_overlap_tokens=2,
    )
    splitter = ParentChildSplitter(cfg)
    parents = splitter.split(" ".join(f"t{i}" for i in range(10)))
    children = parents[0].children
    # step = child_max - overlap = 2; first child tokens t0..t3, second starts at t2.
    assert children[0].content.split()[:4] == ["t0", "t1", "t2", "t3"]
    assert children[1].content.split()[0] == "t2"


def test_splitter_empty_text_yields_no_parents() -> None:
    assert ParentChildSplitter().split("   \n  ") == []


def test_splitter_config_rejects_invalid_overlap() -> None:
    with pytest.raises(ValueError):
        SplitterConfig(child_max_tokens=10, child_overlap_tokens=10)


# --------------------------------------------------------------------------------------------
# Parser: plain-text fast path + Docling dispatch
# --------------------------------------------------------------------------------------------


class _FakeDoclingConverter:
    """A stand-in Docling converter so the rich-format branch is exercised without the heavy dep."""

    def __init__(self) -> None:
        self.converted: list[str] = []

    def convert(self, path: str) -> _FakeDoclingResult:
        self.converted.append(path)
        return _FakeDoclingResult("parsed markdown body")


class _FakeDoclingResult:
    def __init__(self, markdown: str) -> None:
        self.document = _FakeDoclingDocument(markdown)


class _FakeDoclingDocument:
    def __init__(self, markdown: str) -> None:
        self._markdown = markdown

    def export_to_markdown(self) -> str:
        return self._markdown


def test_parser_decodes_plain_text_natively() -> None:
    parsed = DocumentParser().parse("notes.md", "hello wörld café".encode())
    assert parsed.text == "hello wörld café"
    assert parsed.metadata["parser"] == "plain_text"


def test_parser_dispatches_rich_formats_to_docling() -> None:
    converter = _FakeDoclingConverter()
    parser = DocumentParser(docling_converter=converter)

    parsed = parser.parse("report.docx", b"ignored-bytes")

    assert parsed.text == "parsed markdown body"
    assert parsed.metadata["parser"] == "docling"
    assert len(converter.converted) == 1


# --------------------------------------------------------------------------------------------
# Embedder: strict batching by N
# --------------------------------------------------------------------------------------------


def test_batched_splits_strictly_by_size() -> None:
    chunks = list(batched(list(range(70)), 32))
    assert [len(c) for c in chunks] == [32, 32, 6]


@pytest.mark.asyncio
async def test_embedder_batches_strictly_by_32() -> None:
    embedder = _RecordingEmbedder(batch_size=32)
    texts = [f"text-{i}" for i in range(70)]
    dense, sparse = await embedder.embed_documents(texts)
    assert len(dense) == 70
    assert len(sparse) == 70
    # 70 texts => batches of exactly 32, 32, 6.
    assert embedder.batch_sizes == [32, 32, 6]


@pytest.mark.asyncio
async def test_embed_query_returns_single_vectors() -> None:
    embedder = _RecordingEmbedder(batch_size=32)
    dense, sparse = await embedder.embed_query("hello")
    assert isinstance(dense, list)
    assert isinstance(sparse, dict)


# --------------------------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------------------------


def test_suppress_near_duplicates_collapses_whitespace_and_case() -> None:
    items = ["Hello   World", "hello world", "other"]
    assert suppress_near_duplicates(items) == ["Hello   World", "other"]


def test_content_hash_is_stable_and_order_independent_of_type() -> None:
    assert compute_content_hash("abc") == compute_content_hash(b"abc")


# --------------------------------------------------------------------------------------------
# Object storage (local FS, per-tenant)
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_object_storage_roundtrip_is_per_tenant(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    t1, t2 = uuid4(), uuid4()
    ref = await storage.put(t1, "docs/a.txt", b"payload")
    assert ref.startswith("file://")
    assert await storage.get(t1, "docs/a.txt") == b"payload"
    assert await storage.list(t1) == ["docs/a.txt"]
    # Tenant isolation: t2 cannot see t1's object.
    assert await storage.list(t2) == []
    with pytest.raises(FileNotFoundError):
        await storage.get(t2, "docs/a.txt")


@pytest.mark.asyncio
async def test_local_object_storage_rejects_path_traversal(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    with pytest.raises(ValueError):
        await storage.put(uuid4(), "../escape.txt", b"x")


# --------------------------------------------------------------------------------------------
# Pipeline: idempotency, transactional rollback, retry
# --------------------------------------------------------------------------------------------


def _pipeline(
    storage: LocalObjectStorage,
    embedder: BgeM3Embedder,
    repo: BaseIngestRepository,
    **kwargs: object,
) -> IngestionPipeline:
    splitter = ParentChildSplitter(
        SplitterConfig(
            parent_min_tokens=1,
            parent_max_tokens=20,
            child_min_tokens=2,
            child_max_tokens=5,
            child_overlap_tokens=1,
        )
    )
    return IngestionPipeline(
        storage=storage,
        embedder=embedder,
        repository=repo,
        parser=DocumentParser(),
        splitter=splitter,
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_pipeline_indexes_and_notifies(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    tenant = uuid4()
    data = b"alpha beta gamma delta epsilon zeta eta theta iota kappa"
    content_hash = compute_content_hash(data)
    await storage.put(tenant, "a.txt", data)
    repo = _FakeRepository()
    pipeline = _pipeline(storage, _RecordingEmbedder(), repo)

    result = await pipeline.process_document(tenant, "a.txt", content_hash, AccessLevel.INTERNAL)

    assert result.status is DocumentStatus.INDEXED
    assert not result.skipped
    assert result.child_count > 0
    assert len(repo.saved) == 1
    saved_doc, parents, children = repo.saved[0]
    assert saved_doc.access_level is AccessLevel.INTERNAL
    assert all(c.dense_embedding is not None for c in children)
    assert all(c.sparse_embedding is not None for c in children)
    # NOTIFY ingest_events is emitted by the DB trigger (single source of truth), not the pipeline.


@pytest.mark.asyncio
async def test_pipeline_idempotent_skips_known_hash(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    tenant = uuid4()
    data = b"some content here for hashing"
    content_hash = compute_content_hash(data)
    await storage.put(tenant, "a.txt", data)
    repo = _FakeRepository(existing_hashes={content_hash})
    pipeline = _pipeline(storage, _RecordingEmbedder(), repo)

    result = await pipeline.process_document(tenant, "a.txt", content_hash)

    assert result.skipped is True
    assert result.status is DocumentStatus.INDEXED
    assert repo.saved == []


@pytest.mark.asyncio
async def test_pipeline_rolls_back_on_embedder_failure(tmp_path: Path) -> None:
    """A persistent embedder failure must prevent any write (no partial document)."""
    storage = LocalObjectStorage(root=tmp_path)
    tenant = uuid4()
    data = b"alpha beta gamma delta epsilon"
    await storage.put(tenant, "a.txt", data)
    repo = _FakeRepository()

    class _FailingEmbedder(BgeM3Embedder):
        def __init__(self) -> None:
            def _boom(batch: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
                raise RuntimeError("embedder down")

            super().__init__(batch_size=32, encode_fn=_boom)

    pipeline = IngestionPipeline(
        storage=storage,
        embedder=_FailingEmbedder(),
        repository=repo,
        splitter=ParentChildSplitter(
            SplitterConfig(
                parent_min_tokens=1,
                parent_max_tokens=20,
                child_min_tokens=2,
                child_max_tokens=5,
                child_overlap_tokens=1,
            )
        ),
        embed_attempts=2,
    )

    with pytest.raises(RuntimeError, match="embedder down"):
        await pipeline.process_document(tenant, "a.txt", compute_content_hash(data))

    assert repo.saved == []  # nothing persisted (transactional rollback)


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_transient_failures() -> None:
    calls = {"n": 0}

    async def _flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = await retry_async(_flaky, attempts=3, sleep=_no_sleep)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_reraises_after_exhausting_attempts() -> None:
    async def _always_fail() -> str:
        raise RuntimeError("down")

    with pytest.raises(RuntimeError, match="down"):
        await retry_async(_always_fail, attempts=2, sleep=_no_sleep)


# --------------------------------------------------------------------------------------------
# Re-embedding on a version bump
# --------------------------------------------------------------------------------------------


def _child(model_id: str, version: int) -> ChildChunk:
    return ChildChunk(
        tenant_id=uuid4(),
        parent_id=uuid4(),
        content="content",
        token_count=1,
        chunk_index=0,
        embedding_model_id=model_id,
        embedding_version=version,
    )


def test_reembedding_planner_selects_only_affected() -> None:
    target_model, target_version = "bge-m3", 2
    current = _child("bge-m3", 2)
    stale_version = _child("bge-m3", 1)
    stale_model = _child("old-model", 2)

    selected = ReembeddingPlanner.select(
        [current, stale_version, stale_model], target_model, target_version
    )

    assert current not in selected
    assert stale_version in selected
    assert stale_model in selected


@pytest.mark.asyncio
async def test_reembedding_service_stamps_target_version_without_mixing() -> None:
    target_model, target_version = "bge-m3", 2
    chunks = [_child("bge-m3", 1) for _ in range(3)]
    old_dense = [c.dense_embedding for c in chunks]
    service = ReembeddingService(_RecordingEmbedder(), repository=_NullReembedRepo(), batch_size=32)

    updated = await service.reembed_chunks(uuid4(), chunks, target_model, target_version)

    assert all(c.embedding_version == target_version for c in updated)
    assert all(c.embedding_model_id == target_model for c in updated)
    assert all(c.dense_embedding is not None for c in updated)
    # Originals are untouched (old and new versions coexist; no in-place mixing).
    assert [c.embedding_version for c in chunks] == [1, 1, 1]
    assert [c.dense_embedding for c in chunks] == old_dense


# --------------------------------------------------------------------------------------------
# File Watcher: st_size stability + hash dedup
# --------------------------------------------------------------------------------------------


def test_file_watcher_defers_unstable_then_emits_stable(tmp_path: Path) -> None:
    tenant = uuid4()
    tenant_dir = tmp_path / str(tenant)
    tenant_dir.mkdir(parents=True)
    f = tenant_dir / "a.txt"
    f.write_bytes(b"partial")
    clock = {"t": 0.0}
    watcher = FileWatcher(root=tmp_path, debounce_seconds=2.5, clock=lambda: clock["t"])

    # First scan: file seen for the first time => deferred (debounce timer starts now).
    assert watcher.scan(tenant) == []
    # File keeps growing: size changed => debounce timer resets, still deferred.
    f.write_bytes(b"partial-more")
    clock["t"] += 5.0
    assert watcher.scan(tenant) == []
    # Size now stable AND the debounce window has elapsed: emitted.
    clock["t"] += 5.0
    emitted = watcher.scan(tenant)
    assert len(emitted) == 1
    assert emitted[0].key == "a.txt"
    assert emitted[0].content_hash == compute_content_hash(b"partial-more")


def test_file_watcher_respects_debounce_time(tmp_path: Path) -> None:
    """A stable size is NOT enough: the debounce window must also have elapsed."""
    tenant = uuid4()
    tenant_dir = tmp_path / str(tenant)
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "a.txt").write_bytes(b"stable content")
    clock = {"t": 0.0}
    watcher = FileWatcher(root=tmp_path, debounce_seconds=2.5, clock=lambda: clock["t"])

    assert watcher.scan(tenant) == []  # first sighting
    clock["t"] += 1.0  # size stable but < debounce window
    assert watcher.scan(tenant) == []
    clock["t"] += 2.0  # now > 2.5s total since first seen
    assert len(watcher.scan(tenant)) == 1


def test_file_watcher_skips_already_indexed_hash(tmp_path: Path) -> None:
    tenant = uuid4()
    tenant_dir = tmp_path / str(tenant)
    tenant_dir.mkdir(parents=True)
    data = b"already indexed"
    (tenant_dir / "a.txt").write_bytes(data)
    clock = {"t": 0.0}
    watcher = FileWatcher(root=tmp_path, debounce_seconds=2.5, clock=lambda: clock["t"])

    watcher.scan(tenant)  # prime (deferred)
    clock["t"] += 5.0
    emitted = watcher.scan(tenant, known_hashes={compute_content_hash(data)})
    assert emitted == []


# --------------------------------------------------------------------------------------------
# Re-embed repo stub (module-level so it is importable by the async test above)
# --------------------------------------------------------------------------------------------


from rag_core.ingestion.reembedding import BaseReembedRepository  # noqa: E402


class _NullReembedRepo(BaseReembedRepository):
    async def fetch_chunks_batch(
        self, tenant_id: UUID, target_model_id: str, target_version: int, limit: int
    ) -> list[ChildChunk]:
        return []

    async def write_reembedded_batch(self, tenant_id: UUID, chunks: list[ChildChunk]) -> None:
        return None


class _RecordingReembedRepo(BaseReembedRepository):
    """Yields one pending batch, then empties - to exercise the streaming re-embed loop."""

    def __init__(self, pending: list[ChildChunk]) -> None:
        self._pending = list(pending)
        self.written: list[ChildChunk] = []

    async def fetch_chunks_batch(
        self, tenant_id: UUID, target_model_id: str, target_version: int, limit: int
    ) -> list[ChildChunk]:
        batch = self._pending[:limit]
        self._pending = self._pending[limit:]
        return batch

    async def write_reembedded_batch(self, tenant_id: UUID, chunks: list[ChildChunk]) -> None:
        self.written.extend(chunks)


# --------------------------------------------------------------------------------------------
# Splitter: enforced minimum bounds + real token counting
# --------------------------------------------------------------------------------------------


def test_splitter_enforces_child_min_tokens() -> None:
    """The trailing child must not be left below child_min_tokens (back-extended into overlap)."""
    cfg = SplitterConfig(
        parent_min_tokens=1,
        parent_max_tokens=1000,
        child_min_tokens=4,
        child_max_tokens=5,
        child_overlap_tokens=1,
    )
    splitter = ParentChildSplitter(cfg)
    # 11 words, child_max=5, step=4 => naive windows would leave a 3-token tail (< min=4).
    parents = splitter.split(" ".join(f"w{i}" for i in range(11)))
    children = parents[0].children
    assert len(children) >= 2
    assert all(c.token_count >= cfg.child_min_tokens for c in children)
    assert all(c.token_count <= cfg.child_max_tokens for c in children)


def test_splitter_counts_real_tokens_not_words() -> None:
    """A custom count_tokens drives the bounds in model tokens, not whitespace words."""
    # Every word counts as 3 tokens; child_max=6 => at most 2 words per child.
    splitter = ParentChildSplitter(
        SplitterConfig(
            parent_min_tokens=1,
            parent_max_tokens=60,
            child_min_tokens=3,
            child_max_tokens=6,
            child_overlap_tokens=0,
        ),
        count_tokens=lambda _w: 3,
    )
    parents = splitter.split("a b c d e f")
    child = parents[0].children[0]
    assert child.token_count == 6  # 2 words * 3 tokens, not 6 words
    assert len(child.content.split()) == 2


def test_build_splitter_uses_settings_bounds() -> None:
    from rag_core.contracts.settings import Settings

    settings = Settings(
        chunk_parent_min_tokens=5,
        chunk_parent_max_tokens=9,
        chunk_child_min_tokens=2,
        chunk_child_max_tokens=3,
        chunk_child_overlap_tokens=1,
    )
    splitter = build_splitter(settings)
    assert splitter.config.parent_max_tokens == 9
    assert splitter.config.child_max_tokens == 3


# --------------------------------------------------------------------------------------------
# Object storage: storage_ref (URI) round-trip + symlink containment + S3
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_storage_accepts_put_ref_without_double_prefix(tmp_path: Path) -> None:
    storage = LocalObjectStorage(root=tmp_path)
    tenant = uuid4()
    ref = await storage.put(tenant, "docs/a.txt", b"payload")
    # Feeding the URI returned by put() straight back into get() must resolve to the same object.
    assert await storage.get(tenant, ref) == b"payload"
    await storage.delete(tenant, ref)
    with pytest.raises(FileNotFoundError):
        await storage.get(tenant, "docs/a.txt")


@pytest.mark.asyncio
async def test_local_storage_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_bytes(b"secret")
    tenant = uuid4()
    tenant_dir = root / str(tenant)
    tenant_dir.mkdir(parents=True)
    # A symlink inside the tenant dir pointing outside the root must be refused.
    (tenant_dir / "link.txt").symlink_to(outside / "secret.txt")
    storage = LocalObjectStorage(root=root)
    with pytest.raises(ValueError):
        await storage.get(tenant, "link.txt")


class _FakeS3Client:
    """A minimal in-memory S3 client double (records keys, honors the tenant prefix)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **_: object) -> None:  # noqa: N803
        self.objects[Key] = Body

    def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
        class _Body:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

        return {"Body": _Body(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(
        self,
        Bucket: str,
        Prefix: str = "",
        MaxKeys: int = 1000,  # noqa: N803
    ) -> dict[str, object]:
        contents = [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]
        return {"Contents": contents[:MaxKeys]}


@pytest.mark.asyncio
async def test_s3_storage_is_per_tenant_and_ref_roundtrips() -> None:
    client = _FakeS3Client()
    storage = S3ObjectStorage(bucket="b", client=client)
    t1, t2 = uuid4(), uuid4()
    ref = await storage.put(t1, "docs/a.txt", b"payload")
    assert ref == f"s3://b/{t1}/docs/a.txt"
    # Key is stored under the tenant prefix.
    assert f"{t1}/docs/a.txt" in client.objects
    # The returned ref round-trips without double-prefixing the tenant.
    assert await storage.get(t1, ref) == b"payload"
    assert await storage.get(t1, "docs/a.txt") == b"payload"
    # Tenant isolation: the list for t2 is empty, t1 lists the bare relative key.
    assert await storage.list(t1) == ["docs/a.txt"]
    assert await storage.list(t2) == []
    await storage.delete(t1, ref)
    assert client.objects == {}


# --------------------------------------------------------------------------------------------
# Retry policy: non-transient errors are not retried
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_non_transient_errors() -> None:
    calls = {"n": 0}

    async def _bad_data() -> str:
        calls["n"] += 1
        raise ValueError("bad data is not transient")

    with pytest.raises(ValueError, match="bad data"):
        await retry_async(_bad_data, attempts=3, sleep=_no_sleep)
    assert calls["n"] == 1  # raised immediately, no retries


# --------------------------------------------------------------------------------------------
# Dedup: punctuation folding
# --------------------------------------------------------------------------------------------


def test_near_duplicate_key_folds_punctuation() -> None:
    assert near_duplicate_key("Header - 2024") == near_duplicate_key("header 2024")
    assert suppress_near_duplicates(["Header — 2024!", "header 2024", "other"]) == [
        "Header — 2024!",
        "other",
    ]


# --------------------------------------------------------------------------------------------
# Re-embedding: the streaming corpus loop
# --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reembed_corpus_streams_and_writes_only_affected() -> None:
    pending = [_child("old-model", 1) for _ in range(3)]
    repo = _RecordingReembedRepo(pending)
    service = ReembeddingService(_RecordingEmbedder(), repository=repo, batch_size=2)

    total = await service.reembed_corpus(uuid4(), "bge-m3", 2)

    assert total == 3
    assert len(repo.written) == 3
    assert all(c.embedding_version == 2 for c in repo.written)
    assert all(c.embedding_model_id == "bge-m3" for c in repo.written)
    assert all(c.dense_embedding is not None for c in repo.written)
