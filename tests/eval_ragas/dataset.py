"""Golden verification dataset loader (``core/eval_and_ops.md`` §1.1).

The on-disk format is BEIR/QRELS compatible: every example carries a question, the reference
answer, the reference contexts and - crucially for the IR metrics - the set of reference
``ground_truth_chunk_ids`` (the QRELS relevance judgements) plus the ``access_level`` the example
is evaluated under (so the gate also exercises the access-control contract, not only quality).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The canonical dataset location mandated by the plan (Stage J / core/eval_and_ops.md §1.1).
DEFAULT_DATASET_PATH = Path(__file__).resolve().parents[1] / "data" / "golden_dataset.json"


@dataclass(frozen=True, slots=True)
class GoldenSample:
    """A single evaluation example (one QRELS query with its relevance judgements)."""

    id: str
    question: str
    ground_truth_answer: str
    ground_truth_contexts: list[str] = field(default_factory=list)
    ground_truth_chunk_ids: list[str] = field(default_factory=list)
    access_level: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GoldenSample:
        """Build a sample from one JSON object, validating the mandatory fields."""
        try:
            sample_id = str(raw["id"])
            question = str(raw["question"])
            answer = str(raw["ground_truth_answer"])
        except KeyError as exc:  # pragma: no cover - defensive, surfaced as a clear error
            raise ValueError(f"golden sample is missing a mandatory field: {exc}") from exc

        chunk_ids = [str(c) for c in raw.get("ground_truth_chunk_ids", [])]
        if not chunk_ids:
            raise ValueError(
                f"golden sample {sample_id!r} has no ground_truth_chunk_ids "
                "(QRELS judgements are required for the IR metrics)"
            )

        return cls(
            id=sample_id,
            question=question,
            ground_truth_answer=answer,
            ground_truth_contexts=[str(c) for c in raw.get("ground_truth_contexts", [])],
            ground_truth_chunk_ids=chunk_ids,
            access_level=int(raw.get("access_level", 0)),
        )


def load_golden_dataset(path: str | Path | None = None) -> list[GoldenSample]:
    """Load and validate the golden dataset from ``path`` (default: the canonical location).

    Raises a clear :class:`ValueError` on a malformed file so a broken dataset fails the gate loudly
    rather than silently evaluating zero samples.
    """
    dataset_path = Path(path) if path is not None else DEFAULT_DATASET_PATH
    if not dataset_path.exists():
        raise FileNotFoundError(f"golden dataset not found: {dataset_path}")

    raw_text = dataset_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"golden dataset {dataset_path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise ValueError(f"golden dataset {dataset_path} must be a non-empty JSON array")

    samples = [GoldenSample.from_dict(item) for item in payload]

    ids = [s.id for s in samples]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"golden dataset has duplicate ids: {sorted(duplicates)}")

    return samples
