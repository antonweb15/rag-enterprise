"""Fallback LLM Runner with provider chaining (`core/architecture.md` §4, Stage F).

Provides:
- :class:`FallbackLLMRunner` - iterates through a priority list of :class:`BaseLLM` providers
  (e.g. Groq -> Gemini -> Ollama), executing each provider with retry backoff before failing
  over to the next one.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Literal

from rag_core.contracts.interfaces import BaseLLM
from rag_core.contracts.settings import Settings
from rag_core.llm.providers import GeminiLLM, GroqLLM, OllamaLLM
from rag_core.llm.retry import (
    AllProvidersFailedError,
    retry_llm_call,
)


class FallbackLLMRunner(BaseLLM):
    """Orchestrates an ordered fallback chain of LLM providers with retry backoff."""

    def __init__(
        self,
        providers: list[BaseLLM] | None = None,
        settings: Settings | None = None,
        *,
        attempts_per_provider: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._settings = settings or Settings()
        self._providers = (
            providers
            if providers is not None
            else [
                GroqLLM(settings=self._settings),
                GeminiLLM(settings=self._settings),
                OllamaLLM(settings=self._settings),
            ]
        )
        self._attempts = attempts_per_provider
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep

    @property
    def providers(self) -> list[BaseLLM]:
        return list(self._providers)

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        if not self._providers:
            raise AllProvidersFailedError("No LLM providers configured in fallback chain")

        errors: list[tuple[str, Exception]] = []
        for provider in self._providers:
            provider_name = getattr(provider, "model_name", type(provider).__name__)
            try:
                current_prov = provider

                async def _call(p: BaseLLM = current_prov) -> str:
                    return await p.generate(
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                    )

                return await retry_llm_call(
                    _call,
                    attempts=self._attempts,
                    base_delay=self._base_delay,
                    max_delay=self._max_delay,
                    sleep=self._sleep,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append((provider_name, exc))
                continue

        err_summary = "; ".join(f"{name}: {exc}" for name, exc in errors)
        msg = f"All {len(self._providers)} LLM providers failed: {err_summary}"
        raise AllProvidersFailedError(msg)

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        if not self._providers:
            raise AllProvidersFailedError("No LLM providers configured in fallback chain")

        errors: list[tuple[str, Exception]] = []
        for provider in self._providers:
            provider_name = getattr(provider, "model_name", type(provider).__name__)
            try:
                current_prov = provider

                async def _init_stream(
                    p: BaseLLM = current_prov,
                ) -> tuple[AsyncGenerator[str, None], str | None]:
                    stream_gen = p.stream(
                        prompt,
                        system_prompt=system_prompt,
                        temperature=temperature,
                        max_tokens=max_tokens,
                    )
                    try:
                        first = await anext(stream_gen)
                        return stream_gen, first
                    except StopAsyncIteration:
                        return stream_gen, None

                stream_iter, first_token = await retry_llm_call(
                    _init_stream,
                    attempts=self._attempts,
                    base_delay=self._base_delay,
                    max_delay=self._max_delay,
                    sleep=self._sleep,
                )
                if first_token is not None:
                    yield first_token
                async for token in stream_iter:
                    yield token
                return
            except Exception as exc:  # noqa: BLE001
                errors.append((provider_name, exc))
                continue

        err_summary = "; ".join(f"{name}: {exc}" for name, exc in errors)
        msg = f"All {len(self._providers)} LLM stream providers failed: {err_summary}"
        raise AllProvidersFailedError(msg)
