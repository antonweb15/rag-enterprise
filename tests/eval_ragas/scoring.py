"""RAGAS generation/alignment scorers (``core/eval_and_ops.md`` §1.2).

The gate needs four generation metrics - ``Faithfulness``, ``Answer Relevance``,
``Context Precision`` and ``Context Recall``. Two interchangeable implementations are provided
behind the :class:`GenerationScorer` protocol:

- :class:`RagasGenerationScorer` - the production path. It lazily imports the ``ragas`` package and
  delegates to its LLM-judged metrics. Used by the CI gate when ``ragas`` (and an LLM/embeddings
  backend) is available.
- :class:`HeuristicGenerationScorer` - a deterministic, dependency-free fallback used when ``ragas``
  is not installed (e.g. the lint/type/unit test environment). It derives the four metrics from
  lexical overlap and the QRELS judgements so the gate still produces a meaningful, reproducible
  report offline. It is explicitly an approximation, not a replacement for the LLM judge.

:func:`build_generation_scorer` picks the production scorer when possible and transparently falls
back to the heuristic one otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.util import find_spec

from tests.eval_ragas.dataset import GoldenSample
from tests.eval_ragas.probe import ProbeResult

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class RagasScores:
    """The four RAGAS metrics for a single sample (all in ``[0, 1]``)."""

    faithfulness: float
    answer_relevance: float
    context_precision: float
    context_recall: float


class GenerationScorer:
    """Base class / protocol for a per-sample generation scorer.

    Implementations must be deterministic for a fixed input to keep the gate reproducible.
    """

    name: str = "base"

    def score(self, sample: GoldenSample, result: ProbeResult) -> RagasScores:  # noqa: D401
        """Return the four RAGAS metrics for one (sample, pipeline result) pair."""
        raise NotImplementedError


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text or "")}


def _overlap(candidate: str, reference: str) -> float:
    """Token-recall of ``reference`` covered by ``candidate`` (``|c∩r| / |r|``)."""
    ref = _tokens(reference)
    if not ref:
        return 0.0
    cand = _tokens(candidate)
    return len(cand & ref) / len(ref)


class HeuristicGenerationScorer(GenerationScorer):
    """Deterministic lexical fallback used when ``ragas`` is unavailable.

    The four metrics are approximated as follows (binary QRELS where applicable):

    - ``faithfulness`` - share of the answer's tokens that are supported by the retrieved contexts
      (grounding); a confident, context-backed answer scores high, an ungrounded one low;
    - ``answer_relevance`` - token overlap between the answer and the reference answer;
    - ``context_precision`` - share of retrieved chunks that are in the QRELS relevant set;
    - ``context_recall`` - share of the QRELS relevant chunks that were retrieved.
    """

    name = "heuristic"

    def score(self, sample: GoldenSample, result: ProbeResult) -> RagasScores:
        relevant = set(sample.ground_truth_chunk_ids)
        retrieved = result.retrieved_chunk_ids

        if not result.is_confident or not result.answer.strip():
            # A blocked/empty answer is maximally faithful (it asserts nothing) but not relevant.
            faithfulness = 1.0
            answer_relevance = 0.0
        else:
            joined_contexts = " ".join(result.retrieved_contexts)
            faithfulness = _overlap(joined_contexts, result.answer)
            answer_relevance = _overlap(result.answer, sample.ground_truth_answer)

        if retrieved:
            precision = sum(1 for c in retrieved if c in relevant) / len(retrieved)
        else:
            precision = 0.0

        recall = (len(relevant & set(retrieved)) / len(relevant)) if relevant else 0.0

        return RagasScores(
            faithfulness=round(faithfulness, 6),
            answer_relevance=round(answer_relevance, 6),
            context_precision=round(precision, 6),
            context_recall=round(recall, 6),
        )


class RagasGenerationScorer(GenerationScorer):
    """Production scorer delegating to the ``ragas`` package (lazily imported).

    The import and evaluation happen inside :meth:`score`/:meth:`_ensure_loaded` so merely
    importing this module never requires the heavy optional dependency. If ``ragas`` cannot be
    imported at construction time a clear :class:`RuntimeError` is raised, letting the caller fall
    back to the heuristic scorer.
    """

    name = "ragas"

    def __init__(self, llm: object | None = None, embeddings: object | None = None) -> None:
        if find_spec("ragas") is None:  # pragma: no cover - depends on the environment
            raise RuntimeError(
                "the 'ragas' package is not installed; install the 'eval' extra or use the "
                "heuristic fallback scorer"
            )
        # ragas >= 0.2 metrics are stateful instances that need an LLM judge and embeddings; the
        # caller injects them (wrapping the project's own LLM/embedder). They are optional here so
        # construction stays cheap - ragas resolves its own defaults from the environment otherwise.
        self._llm = llm
        self._embeddings = embeddings

    def score(self, sample: GoldenSample, result: ProbeResult) -> RagasScores:  # pragma: no cover
        # Importing lazily keeps the module import light; the real evaluation is delegated to the
        # ragas >= 0.2 API: instance metrics over a single-turn EvaluationDataset. The 0.1
        # module-level metrics (`answer_relevancy`, bare `evaluate(dataset, metrics)`) are gone.
        from ragas import EvaluationDataset, SingleTurnSample
        from ragas import evaluate as ragas_evaluate
        from ragas.metrics import (
            Faithfulness,
            LLMContextPrecisionWithReference,
            LLMContextRecall,
            ResponseRelevancy,
        )

        sample_row = SingleTurnSample(
            user_input=sample.question,
            response=result.answer,
            retrieved_contexts=result.retrieved_contexts or [""],
            reference=sample.ground_truth_answer,
        )
        dataset = EvaluationDataset(samples=[sample_row])
        metrics = [
            Faithfulness(),
            ResponseRelevancy(),
            LLMContextPrecisionWithReference(),
            LLMContextRecall(),
        ]
        eval_kwargs: dict[str, object] = {}
        if self._llm is not None:
            eval_kwargs["llm"] = self._llm
        if self._embeddings is not None:
            eval_kwargs["embeddings"] = self._embeddings
        scores = ragas_evaluate(dataset, metrics=metrics, **eval_kwargs)

        row = scores.to_pandas().iloc[0]

        def _pick(*names: str) -> float:
            # ragas 0.2 result columns are named after the metric instances; tolerate the few
            # known aliases so a minor ragas point release does not silently break the gate.
            for name in names:
                if name in row:
                    return float(row[name])
            raise KeyError(f"none of {names!r} present in ragas result columns {list(row.index)!r}")

        return RagasScores(
            faithfulness=_pick("faithfulness"),
            answer_relevance=_pick("answer_relevancy", "answer_relevance"),
            context_precision=_pick("llm_context_precision_with_reference", "context_precision"),
            context_recall=_pick("context_recall", "llm_context_recall"),
        )


def build_generation_scorer(prefer_ragas: bool = True) -> GenerationScorer:
    """Return the production RAGAS scorer when possible, else the deterministic fallback."""
    if prefer_ragas and find_spec("ragas") is not None:
        try:  # pragma: no cover - exercised only when ragas is installed
            return RagasGenerationScorer()
        except RuntimeError:
            pass
    return HeuristicGenerationScorer()
