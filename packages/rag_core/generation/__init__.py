"""Generation subpackage (`core/soul.md`, `core/architecture.md` §2, Stage F)."""

from __future__ import annotations

from rag_core.generation.citations import CitationResolver
from rag_core.generation.intent_router import IntentRouter
from rag_core.generation.pipeline import (
    GenerationPipeline,
    build_generation_pipeline,
)
from rag_core.generation.prompt import PromptBuilder
from rag_core.generation.query_condenser import QueryCondenser
from rag_core.generation.session_memory import (
    BaseSessionManager,
    InMemorySessionManager,
    SqlAlchemySessionManager,
)

__all__ = [
    "BaseSessionManager",
    "CitationResolver",
    "GenerationPipeline",
    "InMemorySessionManager",
    "IntentRouter",
    "PromptBuilder",
    "QueryCondenser",
    "SqlAlchemySessionManager",
    "build_generation_pipeline",
]
