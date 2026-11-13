"""Citation resolution module (`core/soul.md` §1.3, `core/architecture.md` §2 p.10, Stage F).

Extracts and validates footnote citations [1], [2] from LLM-generated responses
and maps them to concrete parent chunks and source documents.
"""

from __future__ import annotations

import re
from typing import Any

from rag_core.contracts.interfaces import BasePolicyEvaluator
from rag_core.contracts.models import GenerationCitation, ParentChunk, UserContext

_CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


class CitationResolver:
    """Resolves inline citation markers like [1], [2] against the formatted chunk context."""

    def __init__(self, policy_evaluator: BasePolicyEvaluator | None = None) -> None:
        self._policy = policy_evaluator

    async def resolve_citations(
        self,
        answer_text: str,
        chunks: list[ParentChunk],
        user: UserContext | None = None,
    ) -> list[GenerationCitation]:
        """Validates citations with optional PDP re-check (core/architecture.md §2 p.9)."""
        citations = self.extract_citations(answer_text, chunks)
        if self._policy is None or user is None:
            return citations

        chunk_id_map = {c.id: c.document_id for c in chunks}
        verified: list[GenerationCitation] = []
        for cit in citations:
            doc_id = chunk_id_map.get(cit.chunk_id)
            if doc_id is None:
                continue
            if await self._policy.can_access_document(user, doc_id):
                verified.append(cit)
        return verified

    @staticmethod
    def extract_citations(answer_text: str, chunks: list[ParentChunk]) -> list[GenerationCitation]:
        """Parses citation markers from the answer text and maps them to concrete chunk metadata."""
        if not answer_text or not chunks:
            return []

        # Map 1-based index to chunk
        chunk_map = dict(enumerate(chunks, start=1))

        referenced_indices: list[int] = []
        for match in _CITATION_RE.finditer(answer_text):
            group = match.group(1)
            for part in group.split(","):
                part_clean = part.strip()
                if part_clean.isdigit():
                    idx = int(part_clean)
                    if idx in chunk_map and idx not in referenced_indices:
                        referenced_indices.append(idx)

        # Sort indices in order of appearance
        citations: list[GenerationCitation] = []
        for idx in referenced_indices:
            chunk = chunk_map[idx]
            meta: dict[str, Any] = chunk.metadata or {}
            doc_title = (
                str(meta.get("title"))
                if meta.get("title")
                else (
                    str(meta.get("file_name"))
                    if meta.get("file_name")
                    else f"Document {chunk.document_id}"
                )
            )
            source_uri = (
                str(meta.get("source_uri"))
                if meta.get("source_uri")
                else (
                    str(meta.get("file_name"))
                    if meta.get("file_name")
                    else f"urn:doc:{chunk.document_id}"
                )
            )
            page_val = meta.get("page")
            page_num = int(page_val) if isinstance(page_val, int) else None

            citations.append(
                GenerationCitation(
                    citation_index=idx,
                    chunk_id=chunk.id,
                    document_title=doc_title,
                    source_uri=source_uri,
                    page=page_num,
                )
            )

        return citations
