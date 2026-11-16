"""Public channel rate limiting (`core/eval_and_ops.md` §3.5).

Implements server-side sliding-window rate limiting per client IP and session_id
for public endpoints (Web Widget / Chat Stream).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict


class RateLimitExceeded(Exception):
    """Raised when client exceeds allowed requests per window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")
        self.retry_after = retry_after


class InMemoryRateLimiter:
    """Sliding-window rate limiter keyed by (client_ip, session_id)."""

    def __init__(self, max_requests_per_minute: int = 20) -> None:
        self.max_requests_per_minute = max_requests_per_minute
        self._history: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()

    async def check_and_record(self, key: str, now: float | None = None) -> None:
        """Checks if key is within rate limit; records timestamp or raises RateLimitExceeded."""
        current_time = now if now is not None else time.time()
        window_start = current_time - 60.0

        async with self._lock:
            timestamps = self._history[key]
            # Prune timestamps older than 60s
            valid_timestamps = [t for t in timestamps if t >= window_start]
            self._history[key] = valid_timestamps

            if len(valid_timestamps) >= self.max_requests_per_minute:
                oldest = valid_timestamps[0]
                retry_after = max(1, int(60.0 - (current_time - oldest)))
                raise RateLimitExceeded(retry_after=retry_after)

            valid_timestamps.append(current_time)

    async def is_allowed(self, key: str, now: float | None = None) -> bool:
        """Returns True if the request is allowed without raising an exception."""
        try:
            await self.check_and_record(key, now)
            return True
        except RateLimitExceeded:
            return False

    async def reset(self) -> None:
        """Clears all in-memory rate-limit tracking."""
        async with self._lock:
            self._history.clear()
