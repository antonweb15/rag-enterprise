"""LLM provider adapters (`core/architecture.md` §2 p.10, Stage F).

Provides implementations of :class:`BaseLLM`:
- :class:`GroqLLM` - Groq Cloud API (canonical model `llama-3.3-70b-versatile`);
- :class:`GeminiLLM` - Google Gemini Free API (canonical model `gemini-1.5-flash`);
- :class:`OllamaLLM` - Local Ollama server (canonical model `qwen2.5:7b-instruct`);
- :class:`MockLLM` - deterministic in-memory provider for unit tests.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncGenerator, Callable
from typing import Any, Literal

from rag_core.contracts.interfaces import BaseLLM
from rag_core.contracts.settings import Settings
from rag_core.llm.retry import LLMNonRetryableError, LLMTransientError

# Canonical model identifiers (single source of truth for prompts/configs/tests)
GROQ_DEFAULT_MODEL = "llama-3.3-70b-versatile"
GEMINI_DEFAULT_MODEL = "gemini-1.5-flash"
OLLAMA_DEFAULT_MODEL = "qwen2.5:7b-instruct"


def _http_post_sync(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Performs a synchronous JSON HTTP POST with urllib and classifies errors."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        err_msg = f"HTTP {exc.code}: {exc.reason}"
        try:
            err_body = exc.read().decode("utf-8")
            err_msg = f"{err_msg} - {err_body}"
        except Exception:  # noqa: BLE001
            pass
        if exc.code in (429, 500, 502, 503, 504):
            raise LLMTransientError(err_msg) from exc
        raise LLMNonRetryableError(err_msg) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMTransientError(f"Network error calling {url}: {exc}") from exc


class GroqLLM(BaseLLM):
    """Groq Cloud API provider (`llama-3.3-70b-versatile`)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = GROQ_DEFAULT_MODEL,
        settings: Settings | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._settings = settings or Settings()
        self._api_key = api_key or self._settings.groq_api_key
        self._model = model
        self._timeout = timeout
        self._endpoint = "https://api.groq.com/openai/v1/chat/completions"

    @property
    def model_name(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        if not self._api_key:
            raise LLMNonRetryableError("Groq API key is not configured")

        eff_temp = temperature if temperature is not None else self._settings.generation_temperature
        eff_max_tokens = (
            max_tokens if max_tokens is not None else self._settings.generation_max_tokens
        )

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "temperature": eff_temp,
            "max_tokens": eff_max_tokens,
        }
        if response_format == "json_object":
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        res = await asyncio.to_thread(
            _http_post_sync, self._endpoint, payload, headers, self._timeout
        )
        choices = res.get("choices")
        if not choices or not isinstance(choices, list):
            raise LLMTransientError(f"Invalid response from Groq: {res}")
        return str(choices[0].get("message", {}).get("content", ""))

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        text = await self.generate(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format="text",
        )
        for token in text.split(" "):
            yield token + " "


class GeminiLLM(BaseLLM):
    """Google Gemini API provider (e.g. `gemini-1.5-flash`)."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = GEMINI_DEFAULT_MODEL,
        settings: Settings | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._settings = settings or Settings()
        self._api_key = api_key or self._settings.gemini_api_key
        self._model = model
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        if not self._api_key:
            raise LLMNonRetryableError("Gemini API key is not configured")

        eff_temp = temperature if temperature is not None else self._settings.generation_temperature
        eff_max_tokens = (
            max_tokens if max_tokens is not None else self._settings.generation_max_tokens
        )

        gen_config: dict[str, Any] = {
            "temperature": eff_temp,
            "maxOutputTokens": eff_max_tokens,
        }
        if response_format == "json_object":
            gen_config["responseMimeType"] = "application/json"

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": gen_config,
        }
        if system_prompt:
            payload["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self._model}:generateContent?key={self._api_key}"
        headers = {"Content-Type": "application/json"}

        res = await asyncio.to_thread(_http_post_sync, url, payload, headers, self._timeout)
        candidates = res.get("candidates")
        if not candidates or not isinstance(candidates, list):
            raise LLMTransientError(f"Invalid response from Gemini: {res}")
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts or not isinstance(parts, list):
            return ""
        return str(parts[0].get("text", ""))

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        text = await self.generate(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format="text",
        )
        for token in text.split(" "):
            yield token + " "


class OllamaLLM(BaseLLM):
    """Local Ollama provider (`qwen2.5:7b-instruct`)."""

    def __init__(
        self,
        base_url: str | None = None,
        model: str = OLLAMA_DEFAULT_MODEL,
        settings: Settings | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._settings = settings or Settings()
        self._base_url = (base_url or self._settings.ollama_base_url).rstrip("/")
        self._model = model
        self._timeout = timeout

    @property
    def model_name(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        eff_temp = temperature if temperature is not None else self._settings.generation_temperature
        eff_max_tokens = (
            max_tokens if max_tokens is not None else self._settings.generation_max_tokens
        )

        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": eff_temp,
                "num_predict": eff_max_tokens,
            },
        }
        if response_format == "json_object":
            payload["format"] = "json"

        url = f"{self._base_url}/api/chat"
        headers = {"Content-Type": "application/json"}

        res = await asyncio.to_thread(_http_post_sync, url, payload, headers, self._timeout)
        msg = res.get("message")
        if not msg or not isinstance(msg, dict):
            raise LLMTransientError(f"Invalid response from Ollama: {res}")
        return str(msg.get("content", ""))

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        text = await self.generate(
            prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format="text",
        )
        for token in text.split(" "):
            yield token + " "


class MockLLM(BaseLLM):
    """Configurable mock LLM for testing and deterministic validation."""

    def __init__(
        self,
        canned_response: str = "mock answer",
        responder: Callable[[str, str | None], str] | None = None,
        responses_by_prompt_fragment: dict[str, str] | None = None,
        fail_times: int = 0,
        fail_with: Exception | None = None,
    ) -> None:
        self.canned_response = canned_response
        self.responder = responder
        self.responses_by_prompt_fragment = responses_by_prompt_fragment or {}
        self.fail_times = fail_times
        self.fail_with = fail_with or LLMTransientError("Simulated transient failure")
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: Literal["text", "json_object"] = "text",
    ) -> str:
        self.calls.append(
            {
                "prompt": prompt,
                "system_prompt": system_prompt,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "response_format": response_format,
            }
        )
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with

        if self.responder is not None:
            return self.responder(prompt, system_prompt)

        for fragment, resp in self.responses_by_prompt_fragment.items():
            if fragment in prompt or (system_prompt and fragment in system_prompt):
                return resp

        return self.canned_response

    async def stream(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        text = await self.generate(prompt, system_prompt, temperature, max_tokens)
        for part in text.split(" "):
            yield part + " "
