"""The pipeline probe contract consumed by the evaluation gate.

The gate does not import the Stage E/F pipeline directly; it depends on this small async callable
protocol instead. That keeps the gate logic pure and unit-testable (a fake probe can replay fixed
retrievals/answers) while ``__main__`` plugs in a probe backed by the real
:class:`~rag_core.generation.pipeline.GenerationPipeline`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tests.eval_ragas.dataset import GoldenSample


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What the RAG pipeline returned for one golden question.

    - ``retrieved_chunk_ids`` - the reranked child ``chunk_id`` values, best first (drives the IR
      metrics against ``ground_truth_chunk_ids``);
    - ``answer`` - the generated answer text (drives Faithfulness / Answer Relevance);
    - ``retrieved_contexts`` - the hydrated context passages actually fed to the LLM (drives Context
      Precision / Recall);
    - ``is_confident`` - whether the confidence gate passed (a blocked query yields no answer).
    """

    retrieved_chunk_ids: list[str] = field(default_factory=list)
    answer: str = ""
    retrieved_contexts: list[str] = field(default_factory=list)
    is_confident: bool = True


@runtime_checkable
class PipelineProbe(Protocol):
    """An async callable that runs the RAG pipeline for a single golden sample."""

    async def __call__(self, sample: GoldenSample) -> ProbeResult: ...
