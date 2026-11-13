"""LLM retry logic and transient error classification (`core/architecture.md` §4, Stage F).

Provides:
- `LLMError`, `LLMTransientError`, `LLMNonRetryableError`, `AllProvidersFailedError`;
- `is_llm_transient_error` - checks if an error is HTTP 429 / 5xx / timeout;
- `retry_llm_call` - runs an async LLM call with exponential backoff (attempts=3, 1-5s).
"""

from __future__ import annotations

import asyncio
import urllib.error
from collections.abc import Awaitable, Callable
from typing import TypeVar

_T = TypeVar("_T")

# HTTP status codes considered transient (Rate limit 429 and Server errors 5xx)
TRANSIENT_HTTP_STATUS_CODES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class LLMError(Exception):
    """Base exception for LLM provider failures."""


class LLMTransientError(LLMError):
    """A transient LLM failure (e.g. HTTP 429 rate limit, 5xx server error, timeout)."""


class LLMNonRetryableError(LLMError):
    """A fatal/non-transient LLM failure (e.g. HTTP 400 Bad Request, 401 Unauthorized)."""


class AllProvidersFailedError(LLMError):
    """Raised when all providers in the fallback chain have been exhausted."""


def is_llm_transient_error(exc: BaseException) -> bool:
    """Classifies whether an exception is transient and eligible for retry."""
    if isinstance(exc, LLMTransientError):
        return True
    if isinstance(exc, LLMNonRetryableError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUS_CODES
    return isinstance(exc, urllib.error.URLError)


async def retry_llm_call(
    func: Callable[[], Awaitable[_T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 5.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retryable: Callable[[BaseException], bool] = is_llm_transient_error,
) -> _T:
    """Runs ``func`` with exponential backoff retries (3 attempts, 1s..5s backoff).

    Only errors for which ``retryable(exc)`` is True are retried. Non-transient errors
    are re-raised immediately.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - classified by `retryable`
            last_exc = exc
            if not retryable(exc) or attempt == attempts - 1:
                break
            delay = min(base_delay * (2**attempt), max_delay)
            await sleep(delay)
    assert last_exc is not None  # noqa: S101
    raise last_exc
