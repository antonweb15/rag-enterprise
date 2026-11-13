"""Query Condenser module (`core/soul.md` §2.2, `core/architecture.md` §2 p.5, Stage F).

Transforms conversational context-dependent questions into standalone search queries.
"""

from __future__ import annotations

import re

from rag_core.contracts.interfaces import BaseLLM, BaseQueryRewriter
from rag_core.contracts.settings import Settings

QUERY_CONDENSER_SYSTEM_PROMPT = """You are the search-query normalization module of a RAG system.
Formulate a single standalone search query from dialog history and the user's question.

RULES:
- If the question is already standalone, return it unchanged.
- Replace pronouns ("it", "this", "that") with concrete entities from dialog history.
- Do not answer the question or carry on a dialog — return ONLY the search query.
- Do not add quotes, prefixes or explanations.

DIALOG HISTORY:
{chat_history}

NEW QUESTION:
{raw_user_query}

STANDALONE QUERY:"""


class QueryCondenser(BaseQueryRewriter):
    """Normalizes multi-turn follow-up questions using conversation history (K=5)."""

    def __init__(self, llm: BaseLLM, settings: Settings | None = None) -> None:
        self._llm = llm
        self._settings = settings or Settings()

    async def rewrite_query(self, query: str, history: list[dict[str, str]]) -> str:
        trimmed = query.strip()
        if not history or not trimmed:
            return trimmed

        # Format chat history turns
        history_lines: list[str] = []
        for msg in history:
            role = "User" if msg.get("role") == "user" else "Assistant"
            content = str(msg.get("content", "")).strip()
            if content:
                history_lines.append(f"{role}: {content}")

        if not history_lines:
            return trimmed

        chat_history_str = "\n".join(history_lines)
        prompt = QUERY_CONDENSER_SYSTEM_PROMPT.format(
            chat_history=chat_history_str,
            raw_user_query=trimmed,
        )

        try:
            condensed = await self._llm.generate(
                prompt=prompt,
                temperature=0.0,
                max_tokens=200,
            )
            cleaned = self._clean_query(condensed)
            if cleaned:
                return cleaned
        except Exception:  # noqa: BLE001
            # Fail-safe: fall back to original query
            pass

        return trimmed

    @staticmethod
    def _clean_query(raw_text: str) -> str:
        cleaned = raw_text.strip()
        # Remove potential prefixes returned by the model
        prefixes = [
            "STANDALONE QUERY:",
            "Standalone query:",
            "Query:",
            "Search query:",
            "Normalized query:",
        ]
        for prefix in prefixes:
            if cleaned.startswith(prefix):
                cleaned = cleaned[len(prefix) :].strip()

        # Remove outer quotes if wrapped
        if (cleaned.startswith('"') and cleaned.endswith('"')) or (
            cleaned.startswith("'") and cleaned.endswith("'")
        ):
            cleaned = cleaned[1:-1].strip()

        # Remove single-line or multi-line code wrappers
        cleaned = re.sub(r"^```(?:text)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        return cleaned.strip()
