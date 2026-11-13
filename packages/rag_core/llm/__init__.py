"""LLM package (`core/architecture.md` §2, §4, `IMPLEMENTATION_PLAN.md` Stage F)."""

from __future__ import annotations

from rag_core.contracts.interfaces import BaseLLM
from rag_core.llm.fallback import FallbackLLMRunner
from rag_core.llm.providers import (
    GEMINI_DEFAULT_MODEL,
    GROQ_DEFAULT_MODEL,
    OLLAMA_DEFAULT_MODEL,
    GeminiLLM,
    GroqLLM,
    MockLLM,
    OllamaLLM,
)
from rag_core.llm.retry import (
    AllProvidersFailedError,
    LLMError,
    LLMNonRetryableError,
    LLMTransientError,
    is_llm_transient_error,
    retry_llm_call,
)

__all__ = [
    "GEMINI_DEFAULT_MODEL",
    "GROQ_DEFAULT_MODEL",
    "OLLAMA_DEFAULT_MODEL",
    "AllProvidersFailedError",
    "BaseLLM",
    "FallbackLLMRunner",
    "GeminiLLM",
    "GroqLLM",
    "LLMError",
    "LLMNonRetryableError",
    "LLMTransientError",
    "MockLLM",
    "OllamaLLM",
    "is_llm_transient_error",
    "retry_llm_call",
]
