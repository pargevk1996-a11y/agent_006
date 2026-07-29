"""Errors raised by the Vibe API client."""

from __future__ import annotations

from typing import Any


class VibeError(Exception):
    """Base class for every upstream failure."""

    retryable: bool = False


class VibeNetworkError(VibeError):
    """Connection reset, DNS failure, read timeout — safe to retry."""

    retryable = True

    def __init__(self, message: str, *, attempts: int = 1) -> None:
        super().__init__(message)
        self.attempts = attempts


class VibeAPIError(VibeError):
    """Structured error response from the platform."""

    def __init__(
        self,
        status_code: int,
        *,
        code: str = "unknown",
        message: str = "",
        details: Any = None,
        request_id: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"[{status_code}/{code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        self.request_id = request_id
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:  # type: ignore[override]
        """429 and 5xx are transient; every other 4xx is a client bug — never retry."""
        return self.status_code == 429 or self.status_code >= 500

    @property
    def is_budget_related(self) -> bool:
        return self.code in {
            "insufficient_balance",
            "daily_spend_limit_exceeded",
        } or self.status_code == 402


class VibePollTimeoutError(VibeError):
    """A generation did not reach a terminal state within the polling budget."""

    def __init__(self, generation_id: int | str, waited: float) -> None:
        super().__init__(f"generation {generation_id} not finished after {waited:.0f}s")
        self.generation_id = generation_id
        self.waited = waited
