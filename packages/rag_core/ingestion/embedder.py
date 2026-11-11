"""BGE-M3 embedder (`core/architecture.md` §3, `IMPLEMENTATION_PLAN.md` Stage D).

A single BGE-M3 pass produces both the dense (1024-dim) and the sparse (lexical) representation.
Encoding is done in strict batches of ``embedding_batch_size`` (canonically 32, see the resilience
matrix in `core/architecture.md` §4 - batching strictly by 32 protects the CPU/embedder from OOM).

The heavy ``FlagEmbedding`` model is imported lazily and built on first use, so this module (and the
whole pipeline) imports and unit-tests without the model installed. Tests inject a lightweight
``encode_fn`` to exercise the batching/aggregation logic deterministically.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import TypeVar

from rag_core.contracts.interfaces import BaseEmbedder
from rag_core.contracts.models import DEFAULT_EMBEDDING_MODEL

DENSE_DIM = 1024

# An encode function maps a batch of texts to (dense_vectors, sparse_vectors).
EncodeFn = Callable[[list[str]], tuple[list[list[float]], list[dict[int, float]]]]

_T = TypeVar("_T")


def batched(items: Sequence[_T], batch_size: int) -> Iterator[list[_T]]:
    """Yields consecutive slices of ``items`` of length ``batch_size`` (last may be shorter)."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")
    for start in range(0, len(items), batch_size):
        yield list(items[start : start + batch_size])


class BgeM3Embedder(BaseEmbedder):
    """The BGE-M3 ``BaseEmbedder`` provider with strict batch-by-N encoding.

    ``encode_fn`` is the single-batch encoder. When omitted, a lazily-constructed
    ``FlagEmbedding.BGEM3FlagModel`` is used (imported only on first encode). The batch size is the
    canonical ``embedding_batch_size`` (32) unless overridden.
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        batch_size: int = 32,
        encode_fn: EncodeFn | None = None,
        use_fp16: bool = True,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be > 0")
        self._model_name = model_name
        self._batch_size = batch_size
        self._use_fp16 = use_fp16
        self._encode_fn = encode_fn
        self._model: object | None = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def embed_documents(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[dict[int, float]]]:
        """A single BGE-M3 pass over all texts, encoded in strict batches of ``batch_size``."""
        dense_all: list[list[float]] = []
        sparse_all: list[dict[int, float]] = []
        encode = self._resolve_encode_fn()
        for batch in batched(texts, self._batch_size):
            dense_batch, sparse_batch = encode(batch)
            if len(dense_batch) != len(batch) or len(sparse_batch) != len(batch):
                raise ValueError("encode_fn must return one vector per input text")
            dense_all.extend(dense_batch)
            sparse_all.extend(sparse_batch)
        return dense_all, sparse_all

    async def embed_query(self, text: str) -> tuple[list[float], dict[int, float]]:
        """Encodes a single search query (dense + sparse)."""
        dense, sparse = await self.embed_documents([text])
        return dense[0], sparse[0]

    def _resolve_encode_fn(self) -> EncodeFn:
        if self._encode_fn is not None:
            return self._encode_fn
        self._encode_fn = self._build_flag_embedding_encoder()
        return self._encode_fn

    def _build_flag_embedding_encoder(self) -> EncodeFn:
        """Lazily constructs the FlagEmbedding BGE-M3 model and wraps it as an ``EncodeFn``."""
        try:
            from FlagEmbedding import BGEM3FlagModel
        except ImportError as exc:  # pragma: no cover - exercised only without the heavy dep
            raise RuntimeError(
                "FlagEmbedding is not installed; install it or inject an encode_fn "
                "(pip install FlagEmbedding)"
            ) from exc

        model = BGEM3FlagModel(self._model_name, use_fp16=self._use_fp16)
        self._model = model

        def _encode(batch: list[str]) -> tuple[list[list[float]], list[dict[int, float]]]:
            output = model.encode(
                batch,
                batch_size=self._batch_size,
                return_dense=True,
                return_sparse=True,
                return_colbert_vecs=False,
            )
            dense = [list(map(float, vec)) for vec in output["dense_vecs"]]
            sparse = [
                {int(k): float(v) for k, v in weights.items()}
                for weights in output["lexical_weights"]
            ]
            return dense, sparse

        return _encode
