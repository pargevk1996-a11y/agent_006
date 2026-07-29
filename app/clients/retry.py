"""Retry policy: exponential backoff with jitter, for transient failures only.

Retried: transport errors (connect/read timeouts, resets), HTTP 429, HTTP 5xx.
Never retried: 4xx other than 429 — those are deterministic client errors and a
retry would only waste the rate-limit budget (and, for ``/generate``, risk a
second charge if the request actually did reach the server).
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.clients.exceptions import VibeAPIError, VibeError, VibeNetworkError

Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.3

    def should_retry(self, error: Exception, attempt: int) -> bool:
        if attempt >= self.max_attempts:
            return False
        if isinstance(error, VibeNetworkError):
            return True
        if isinstance(error, VibeAPIError):
            return error.retryable
        return False

    def delay_for(self, attempt: int, *, retry_after: float | None = None,
                  rng: random.Random | None = None) -> float:
        """Backoff for ``attempt`` (1-based). ``Retry-After`` wins when provided."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_delay)
        rng = rng or random
        backoff = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        spread = backoff * self.jitter
        return max(0.0, backoff + rng.uniform(-spread, spread))


async def with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    sleeper: Sleeper | None = None,
    rng: random.Random | None = None,
    on_retry: Callable[[int, float, Exception], None] | None = None,
) -> T:
    """Run ``operation``, retrying only transient errors."""
    sleep = sleeper or asyncio.sleep
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except VibeError as error:
            if not policy.should_retry(error, attempt):
                raise
            retry_after = getattr(error, "retry_after", None)
            delay = policy.delay_for(attempt, retry_after=retry_after, rng=rng)
            if on_retry:
                on_retry(attempt, delay, error)
            await sleep(delay)
